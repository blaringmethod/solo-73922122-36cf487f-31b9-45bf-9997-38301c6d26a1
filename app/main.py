"""国土调查地类拆分核算 API。

功能：
- 登记/替换各图幅旧地类像元数（PUT /sheets/{id}/old-counts）；
- 登记/更正各图幅部分新地类的手工复核整幅总数（PUT /sheets/{id}/review-totals）；
- 按图幅一次性登记外业分块编号清单（PUT /sheets/{id}/blocks），分块由外业划定、
  互不重叠且覆盖整幅；
- 按块提交各新地类非负整数像元数（PUT /sheets/{id}/blocks/{bid}/counts），
  重传整份替换该块报表，填 0 算已报；
- 整表更换新旧地类对照表（PUT /legend，一个旧码可对应多个新码）；
- 按图幅 + 一对新旧地类查询重合像元数的最小/最大值（GET /overlap），
  两值相同即为确数。

分块合计规则（有效复核数的唯一推导口径，图幅查询与 /overlap 共用）：
- 某新地类在每个登记分块都报了数（含 0）才视为齐报，采用各块之和作为合计；
- 重传漏项后不再齐报的，撤下该合计；若该新地类另有手工数，则沿用手工数；
- 同地类手工数与齐报合计并存时必须相等，只计一次；不相等即冲突，
  引发冲突的整次更新回滚。

一致性约定：
- 任何更新先写入事务、再校验受影响图幅（手工/分块冲突 + 是否存在合法拆分）；
  只要有冲突或图幅无解，整次更新回滚，并在 409 响应中列出受影响图幅；
- 分块报表按块独立存储，重传只替换该块；合计由服务端依据已提交的各块报表
  重新推导，无需回读或重传其他块；
- 换表校验以提交那一刻已提交的计数为准，换表核对期间登记照常进行；
- 查询只读取已提交状态（全局锁 + 单连接，读写互斥）。
"""
from __future__ import annotations

import os
import sqlite3
import threading
from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field, NonNegativeInt

from .solver import InfeasibleStateError, SheetProblem, check_feasible, overlap_bounds

SCHEMA = """
CREATE TABLE IF NOT EXISTS legend (
    old_code TEXT NOT NULL,
    new_code TEXT NOT NULL,
    PRIMARY KEY (old_code, new_code)
);
CREATE TABLE IF NOT EXISTS old_counts (
    sheet_id TEXT NOT NULL,
    old_code TEXT NOT NULL,
    count INTEGER NOT NULL CHECK (count >= 0),
    PRIMARY KEY (sheet_id, old_code)
);
CREATE TABLE IF NOT EXISTS review_totals (
    sheet_id TEXT NOT NULL,
    new_code TEXT NOT NULL,
    total INTEGER NOT NULL CHECK (total >= 0),
    PRIMARY KEY (sheet_id, new_code)
);
CREATE TABLE IF NOT EXISTS sheet_blocks (
    sheet_id TEXT NOT NULL,
    block_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    PRIMARY KEY (sheet_id, block_id)
);
CREATE TABLE IF NOT EXISTS block_reports (
    sheet_id TEXT NOT NULL,
    block_id TEXT NOT NULL,
    new_code TEXT NOT NULL,
    count INTEGER NOT NULL CHECK (count >= 0),
    PRIMARY KEY (sheet_id, block_id, new_code)
);
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


class LegendBody(BaseModel):
    mapping: dict[str, list[str]] = Field(
        ..., description="旧地类 -> 可对应的新地类列表（一个旧码可对应多个新码），整表替换")


class OldCountsBody(BaseModel):
    counts: dict[str, NonNegativeInt] = Field(
        ..., description="旧地类 -> 像元数，整幅替换；0 等价于不登记")


class ReviewTotalsBody(BaseModel):
    totals: dict[str, NonNegativeInt] = Field(
        ..., description="新地类 -> 手工复核整幅总数，整幅替换（可更正）；不需要的键省略即可")


class BlocksBody(BaseModel):
    blocks: list[str] = Field(
        ..., description="分块编号清单，按图幅登记一次；分块由外业划定，互不重叠且覆盖整幅")


class BlockCountsBody(BaseModel):
    counts: dict[str, NonNegativeInt] = Field(
        ..., description="该分块各新地类像元数（非负整数，填 0 算已报），整份替换该块报表")


class BlocksAlreadyRegisteredError(RuntimeError):
    def __init__(self, sheet_id: str, blocks: list[str]):
        super().__init__(f"图幅 {sheet_id} 已登记分块编号清单，不能重复登记")
        self.blocks = blocks


class BlocksNotRegisteredError(RuntimeError):
    def __init__(self, sheet_id: str):
        super().__init__(f"图幅 {sheet_id} 尚未登记分块编号清单，请先调用分块登记接口")


class UnknownBlockError(RuntimeError):
    def __init__(self, sheet_id: str, block_id: str):
        super().__init__(f"分块 {block_id} 不在图幅 {sheet_id} 已登记的分块清单中")


def _normalize_codes(pairs: dict[str, list[str]]) -> dict[str, set[str]]:
    mapping: dict[str, set[str]] = {}
    for old, news in pairs.items():
        old = old.strip()
        if not old:
            raise HTTPException(422, "地类代码不能为空字符串")
        targets = {n.strip() for n in news if n.strip()}
        if targets:
            mapping.setdefault(old, set()).update(targets)
    return mapping


def _normalize_counts(pairs: dict[str, int], label: str) -> dict[str, int]:
    out: dict[str, int] = {}
    for code, value in pairs.items():
        code = code.strip()
        if not code:
            raise HTTPException(422, f"{label}的地类代码不能为空字符串")
        if value > 0:
            out[code] = out.get(code, 0) + value
    return out


def _normalize_block_ids(ids: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for raw in ids:
        block_id = raw.strip()
        if not block_id:
            raise HTTPException(422, "分块编号不能为空字符串")
        if block_id in seen:
            raise HTTPException(422, f"分块编号重复：{block_id}")
        seen.add(block_id)
        out.append(block_id)
    if not out:
        raise HTTPException(422, "分块编号清单不能为空")
    return out


def _normalize_block_counts(pairs: dict[str, int], label: str) -> dict[str, int]:
    """分块计数规范化：与手工数不同，0 必须保留（填 0 算已报）。"""
    out: dict[str, int] = {}
    for code, value in pairs.items():
        code = code.strip()
        if not code:
            raise HTTPException(422, f"{label}的地类代码不能为空字符串")
        out[code] = value
    return out


class LandService:
    """持有 SQLite 连接与写锁，所有读写都序列化，查询只见已提交状态。"""

    def __init__(self, db_path: str):
        if db_path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
        self.conn = sqlite3.connect(db_path, check_same_thread=False,
                                    isolation_level=None)  # 手动控制事务
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.execute(
            "INSERT OR IGNORE INTO meta (key, value) VALUES ('legend_version', '0')")
        self.conn.commit()
        self.lock = threading.RLock()

    # ---- 读取辅助（调用方需已持有锁） ----
    def _mapping(self) -> dict[str, set[str]]:
        rows = self.conn.execute("SELECT old_code, new_code FROM legend").fetchall()
        mapping: dict[str, set[str]] = {}
        for r in rows:
            mapping.setdefault(r["old_code"], set()).add(r["new_code"])
        return mapping

    def _legend_version(self) -> int:
        row = self.conn.execute(
            "SELECT value FROM meta WHERE key = 'legend_version'").fetchone()
        return int(row["value"])

    def _old_counts(self, sheet_id: str) -> dict[str, int]:
        return {
            r["old_code"]: r["count"]
            for r in self.conn.execute(
                "SELECT old_code, count FROM old_counts WHERE sheet_id = ?",
                (sheet_id,))
        }

    def _manual_totals(self, sheet_id: str) -> dict[str, int]:
        return {
            r["new_code"]: r["total"]
            for r in self.conn.execute(
                "SELECT new_code, total FROM review_totals WHERE sheet_id = ?",
                (sheet_id,))
        }

    def _block_ids(self, sheet_id: str) -> list[str]:
        rows = self.conn.execute(
            "SELECT block_id FROM sheet_blocks WHERE sheet_id = ? ORDER BY seq",
            (sheet_id,)).fetchall()
        return [r["block_id"] for r in rows]

    def _block_reports(self, sheet_id: str) -> dict[str, dict[str, int]]:
        reports: dict[str, dict[str, int]] = {}
        for r in self.conn.execute(
                "SELECT block_id, new_code, count FROM block_reports "
                "WHERE sheet_id = ?", (sheet_id,)):
            reports.setdefault(r["block_id"], {})[r["new_code"]] = r["count"]
        return reports

    def _effective_review(self, sheet_id: str) -> tuple[
            dict[str, int], list[str], dict[str, dict], dict[str, int],
            list[str], dict[str, dict[str, int]]]:
        """推导图幅当前已提交状态下的有效复核数。

        返回 (有效复核数, 冲突描述, 齐报状态, 手工数, 分块清单, 各块报表)。
        规则：
        - 新地类在每个分块都报了数（含 0）才齐报，采用各块之和；
        - 不齐报时该新地类不产生分块合计，有手工数则沿用手工数；
        - 手工数与齐报合计并存且不等即为冲突，有效数暂取手工数供可行性诊断，
          调用方必须据冲突列表拒绝整次更新。
        """
        manual = self._manual_totals(sheet_id)
        block_ids = self._block_ids(sheet_id)
        reports = self._block_reports(sheet_id)
        effective: dict[str, int] = dict(manual)
        conflicts: list[str] = []
        completeness: dict[str, dict] = {}
        if block_ids:
            all_codes: set[str] = set()
            for bid in block_ids:
                all_codes.update(reports.get(bid, {}))
            for code in sorted(all_codes):
                values = [reports[bid][code]
                          for bid in block_ids if code in reports.get(bid, {})]
                total = sum(values)
                is_complete = len(values) == len(block_ids)
                completeness[code] = {
                    "reported_blocks": len(values),
                    "total_blocks": len(block_ids),
                    "complete": is_complete,
                    "sum": total,
                }
                if not is_complete:
                    # 漏项：撤下分块合计；手工数已在 effective 中，沿用即可
                    continue
                if code in manual and manual[code] != total:
                    conflicts.append(
                        f"新地类[{code}]手工复核数 {manual[code]} 与分块齐报合计 "
                        f"{total} 不相等，二者并存时必须相等（只计一次）")
                else:
                    effective[code] = total  # 与手工数相等时也是同一个值，只计一次
        return effective, conflicts, completeness, manual, block_ids, reports

    def _sheet_problem(self, sheet_id: str,
                       mapping: dict[str, set[str]] | None = None) -> SheetProblem:
        if mapping is None:
            mapping = self._mapping()
        review, *_ = self._effective_review(sheet_id)
        return SheetProblem(old_counts=self._old_counts(sheet_id),
                            review_totals=review, mapping=mapping)

    def _sheet_ids_with_data(self) -> list[str]:
        rows = self.conn.execute(
            "SELECT sheet_id FROM old_counts UNION "
            "SELECT sheet_id FROM review_totals UNION "
            "SELECT sheet_id FROM sheet_blocks"
        ).fetchall()
        return sorted(r["sheet_id"] for r in rows)

    # ---- 写操作：事务内先写入再校验，失败整体回滚 ----
    def replace_legend(self, mapping: dict[str, set[str]]) -> list[dict]:
        """整表换对照表，返回受影响图幅列表（空 = 成功）。"""
        with self.lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                self.conn.execute("DELETE FROM legend")
                self.conn.executemany(
                    "INSERT INTO legend (old_code, new_code) VALUES (?, ?)",
                    [(o, n) for o, ns in mapping.items() for n in sorted(ns)])
                self.conn.execute(
                    "UPDATE meta SET value = CAST(value AS INTEGER) + 1 "
                    "WHERE key = 'legend_version'")
                self.conn.execute(
                    "INSERT OR REPLACE INTO meta (key, value) VALUES "
                    "('legend_updated_at', ?)",
                    (datetime.now(timezone.utc).isoformat(),))
                affected = self._validate_sheets(self._sheet_ids_with_data())
                if affected:
                    self.conn.rollback()
                    return affected
                self.conn.commit()
                return []
            except Exception:
                self.conn.rollback()
                raise

    def put_old_counts(self, sheet_id: str, counts: dict[str, int]) -> list[dict]:
        with self.lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                self.conn.execute("DELETE FROM old_counts WHERE sheet_id = ?",
                                  (sheet_id,))
                self.conn.executemany(
                    "INSERT INTO old_counts (sheet_id, old_code, count) VALUES (?, ?, ?)",
                    [(sheet_id, o, c) for o, c in sorted(counts.items())])
                affected = self._validate_sheets([sheet_id])
                if affected:
                    self.conn.rollback()
                    return affected
                self.conn.commit()
                return []
            except Exception:
                self.conn.rollback()
                raise

    def put_review_totals(self, sheet_id: str, totals: dict[str, int]) -> list[dict]:
        with self.lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                self.conn.execute("DELETE FROM review_totals WHERE sheet_id = ?",
                                  (sheet_id,))
                self.conn.executemany(
                    "INSERT INTO review_totals (sheet_id, new_code, total) "
                    "VALUES (?, ?, ?)",
                    [(sheet_id, n, v) for n, v in sorted(totals.items())])
                affected = self._validate_sheets([sheet_id])
                if affected:
                    self.conn.rollback()
                    return affected
                self.conn.commit()
                return []
            except Exception:
                self.conn.rollback()
                raise

    def register_blocks(self, sheet_id: str, block_ids: list[str]) -> list[dict]:
        """按图幅登记一次分块编号清单；重复登记抛 BlocksAlreadyRegisteredError。"""
        with self.lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                existing = self._block_ids(sheet_id)
                if existing:
                    self.conn.rollback()
                    raise BlocksAlreadyRegisteredError(sheet_id, existing)
                self.conn.executemany(
                    "INSERT INTO sheet_blocks (sheet_id, block_id, seq) "
                    "VALUES (?, ?, ?)",
                    [(sheet_id, bid, i) for i, bid in enumerate(block_ids)])
                affected = self._validate_sheets([sheet_id])
                if affected:
                    self.conn.rollback()
                    return affected
                self.conn.commit()
                return []
            except BlocksAlreadyRegisteredError:
                raise
            except Exception:
                self.conn.rollback()
                raise

    def put_block_counts(self, sheet_id: str, block_id: str,
                         counts: dict[str, int]) -> list[dict]:
        """整份替换某分块报表（含 0 值），随后重新推导图幅有效复核数并校验。"""
        with self.lock:
            block_ids = self._block_ids(sheet_id)
            if not block_ids:
                raise BlocksNotRegisteredError(sheet_id)
            if block_id not in block_ids:
                raise UnknownBlockError(sheet_id, block_id)
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                self.conn.execute(
                    "DELETE FROM block_reports WHERE sheet_id = ? AND block_id = ?",
                    (sheet_id, block_id))
                self.conn.executemany(
                    "INSERT INTO block_reports "
                    "(sheet_id, block_id, new_code, count) VALUES (?, ?, ?, ?)",
                    [(sheet_id, block_id, n, c)
                     for n, c in sorted(counts.items())])
                affected = self._validate_sheets([sheet_id])
                if affected:
                    self.conn.rollback()
                    return affected
                self.conn.commit()
                return []
            except Exception:
                self.conn.rollback()
                raise

    def _validate_sheets(self, sheet_ids: list[str]) -> list[dict]:
        """在事务内校验图幅（手工/分块冲突 + 可行性），返回 [{sheet_id, problems}]。"""
        mapping = self._mapping()
        affected = []
        for sid in sheet_ids:
            effective, conflicts, *_ = self._effective_review(sid)
            problems = list(conflicts)
            prob = SheetProblem(old_counts=self._old_counts(sid),
                                review_totals=effective, mapping=mapping)
            problems.extend(check_feasible(prob))
            if problems:
                affected.append({"sheet_id": sid, "problems": problems})
        return affected


def create_app(db_path: str | None = None) -> FastAPI:
    service = LandService(
        db_path or os.environ.get("LAND_DB_PATH", "data/land.db"))
    app = FastAPI(
        title="国土调查地类拆分核算 API",
        version="1.1.0",
        description="登记图幅旧地类像元数、手工/外业分块复核新地类总数与新旧地类对照表，"
                    "查询任一对新旧地类重合像元数的可能范围（最小/最大，相同即确数）。"
                    "分块齐报（每块都报、含 0）才采用合计，漏报撤下、手工数沿用；"
                    "手工数与齐报合计并存时必须相等，只计一次。",
    )

    def conflict(affected: list[dict]) -> HTTPException:
        return HTTPException(status_code=409, detail={
            "message": "该更新会造成复核数冲突或使部分图幅不存在合法的非负整数拆分，"
                       "整次更新已回滚，原报表与生效数据保持不变",
            "affected_sheets": affected,
        })

    @app.get("/", include_in_schema=False)
    def root():
        return RedirectResponse("/docs")

    @app.get("/legend", summary="查看当前对照表")
    def get_legend():
        with service.lock:
            return {
                "version": service._legend_version(),
                "mapping": {o: sorted(ns) for o, ns in sorted(service._mapping().items())},
            }

    @app.put("/legend", summary="整表更换新旧地类对照表")
    def put_legend(body: LegendBody):
        mapping = _normalize_codes(body.mapping)
        affected = service.replace_legend(mapping)
        if affected:
            raise conflict(affected)
        with service.lock:
            version = service._legend_version()
        return {"message": "对照表已整表更换", "version": version,
                "mapping": {o: sorted(ns) for o, ns in sorted(mapping.items())}}

    @app.put("/sheets/{sheet_id}/old-counts", summary="登记/替换图幅旧地类像元数")
    def put_old_counts(sheet_id: str, body: OldCountsBody):
        counts = _normalize_counts(body.counts, "旧地类计数")
        affected = service.put_old_counts(sheet_id, counts)
        if affected:
            raise conflict(affected)
        return {"message": "旧地类像元数已登记", "sheet_id": sheet_id,
                "old_counts": counts}

    @app.put("/sheets/{sheet_id}/review-totals",
             summary="手工补报/更正图幅新地类复核总数")
    def put_review_totals(sheet_id: str, body: ReviewTotalsBody):
        totals = _normalize_counts(body.totals, "复核总数")
        affected = service.put_review_totals(sheet_id, totals)
        if affected:
            raise conflict(affected)
        return {"message": "手工复核总数已登记", "sheet_id": sheet_id,
                "review_totals": totals}

    @app.put("/sheets/{sheet_id}/blocks",
             summary="按图幅登记外业分块编号清单（每幅仅登记一次）")
    def put_blocks(sheet_id: str, body: BlocksBody):
        block_ids = _normalize_block_ids(body.blocks)
        try:
            affected = service.register_blocks(sheet_id, block_ids)
        except BlocksAlreadyRegisteredError as exc:
            raise HTTPException(status_code=409, detail={
                "message": str(exc),
                "sheet_id": sheet_id,
                "existing_blocks": exc.blocks,
            })
        if affected:
            raise conflict(affected)
        return {"message": "分块编号清单已登记", "sheet_id": sheet_id,
                "blocks": block_ids, "block_count": len(block_ids)}

    @app.get("/sheets/{sheet_id}/blocks",
             summary="查看分块清单、各块报表与齐报状态")
    def get_blocks(sheet_id: str):
        with service.lock:
            block_ids = service._block_ids(sheet_id)
            if not block_ids:
                raise HTTPException(404, f"图幅 {sheet_id} 未登记分块编号清单")
            (effective, conflicts, completeness, manual, _,
             reports) = service._effective_review(sheet_id)
        return {
            "sheet_id": sheet_id,
            "blocks": block_ids,
            "block_count": len(block_ids),
            "reports": {bid: dict(sorted(reports.get(bid, {}).items()))
                        for bid in block_ids},
            "completeness": {code: completeness[code]
                             for code in sorted(completeness)},
            "manual_review_totals": dict(sorted(manual.items())),
            "effective_review_totals": dict(sorted(effective.items())),
            "conflicts": conflicts,
        }

    @app.put("/sheets/{sheet_id}/blocks/{block_id}/counts",
             summary="按块提交/重传新地类像元数（整份替换，填 0 算已报）")
    def put_block_counts(sheet_id: str, block_id: str, body: BlockCountsBody):
        counts = _normalize_block_counts(body.counts, "分块计数")
        try:
            affected = service.put_block_counts(sheet_id, block_id, counts)
        except BlocksNotRegisteredError as exc:
            raise HTTPException(status_code=409, detail={
                "message": str(exc), "sheet_id": sheet_id})
        except UnknownBlockError as exc:
            raise HTTPException(status_code=404, detail={"message": str(exc)})
        if affected:
            raise conflict(affected)
        with service.lock:
            (effective, _, completeness, _, _,
             _) = service._effective_review(sheet_id)
        return {
            "message": "分块报表已整份替换",
            "sheet_id": sheet_id,
            "block_id": block_id,
            "counts": dict(sorted(counts.items())),
            "completeness": {code: completeness[code]
                             for code in sorted(completeness)},
            "effective_review_totals": dict(sorted(effective.items())),
        }

    @app.get("/sheets", summary="列出已有数据的图幅")
    def list_sheets():
        with service.lock:
            result = []
            for sid in service._sheet_ids_with_data():
                effective, *_ = service._effective_review(sid)
                result.append({
                    "sheet_id": sid,
                    "old_pixels": sum(service._old_counts(sid).values()),
                    "review_pixels": sum(effective.values()),
                    "block_count": len(service._block_ids(sid)),
                })
            return {"sheets": result}

    @app.get("/sheets/{sheet_id}", summary="查看图幅已登记的计数与有效复核数")
    def get_sheet(sheet_id: str):
        with service.lock:
            old_counts = service._old_counts(sheet_id)
            block_ids = service._block_ids(sheet_id)
            (effective, conflicts, completeness, manual, _,
             reports) = service._effective_review(sheet_id)
            if not old_counts and not manual and not block_ids:
                raise HTTPException(404, f"图幅 {sheet_id} 没有任何已登记数据")
            return {
                "sheet_id": sheet_id,
                "old_counts": dict(sorted(old_counts.items())),
                "review_totals": dict(sorted(effective.items())),
                "manual_review_totals": dict(sorted(manual.items())),
                "total_pixels": sum(old_counts.values()),
                "reviewed_pixels": sum(effective.values()),
                "blocks": {
                    "block_ids": block_ids,
                    "reports": {bid: dict(sorted(reports.get(bid, {}).items()))
                                for bid in block_ids},
                    "completeness": {code: completeness[code]
                                     for code in sorted(completeness)},
                    "conflicts": conflicts,
                },
            }

    @app.get("/overlap", summary="查询一对新旧地类重合像元数的最小/最大值")
    def get_overlap(
        sheet_id: str = Query(..., description="图幅号"),
        old_code: str = Query(..., description="旧地类代码"),
        new_code: str = Query(..., description="新地类代码"),
    ):
        with service.lock:
            prob = service._sheet_problem(sheet_id)
            version = service._legend_version()
            block_ids = service._block_ids(sheet_id)
            manual = service._manual_totals(sheet_id)
        if not prob.old_counts and not prob.review_totals and not block_ids:
            raise HTTPException(404, f"图幅 {sheet_id} 没有任何已登记数据")
        base = {"sheet_id": sheet_id, "old_code": old_code, "new_code": new_code,
                "legend_version": version}
        if new_code not in prob.mapping.get(old_code, set()):
            return {**base, "in_legend": False, "min": 0, "max": 0,
                    "exact": True, "value": 0,
                    "note": "该新旧地类对不在对照表中，重合像元数恒为 0"}
        try:
            lo, hi = overlap_bounds(prob, old_code, new_code)
        except InfeasibleStateError as exc:  # 理论上不会发生
            raise HTTPException(500, str(exc))
        source = ("手工复核数" if new_code in manual else
                  ("分块齐报合计" if block_ids and new_code in prob.review_totals
                   else "未复核"))
        return {**base, "in_legend": True, "min": lo, "max": hi,
                "exact": lo == hi, "value": lo if lo == hi else None,
                "review_source": source,
                "note": "确数" if lo == hi else "区间估计，需进一步复核"}

    return app


app = create_app()
