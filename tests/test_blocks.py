"""外业分块复核 API 端到端测试。

覆盖需求：
- 分块清单按图幅登记一次，重传替换该块整份报表；
- 每块都报数（含 0）才采用合计；重传漏项且不再齐报则撤下合计，手工数沿用；
- 手工数与齐报合计并存必须相等、只计一次，不等则整次拒绝；
- 图幅查询与 /overlap 使用同一已提交状态下的有效复核数；
- 补报、更正手工数、改旧计数、换图例导致冲突/无解时整次拒绝，原数据不变；
- 汇总在服务端推导，重传一块无需回读其他块。
"""
import pytest
from fastapi.testclient import TestClient

from app.main import create_app

LEGEND = {
    "01": ["0101", "0103"],
    "02": ["0201", "0202"],
    "03": ["0301", "0302"],
}
# 简化图：仅旧码 01/02，所有旧码在图例中均有去向，保证基线可行。
# 注意 01 只能进入 0101/0103，故两列复核之和不得超过 01 的像元数，
# 否则该图幅本就无解（由 409 路径专门覆盖）。
SMALL_COUNTS = {"01": 1000, "02": 500}


@pytest.fixture()
def client(tmp_path):
    app = create_app(str(tmp_path / "test.db"))
    with TestClient(app) as c:
        yield c


def seed(client, sheet="S1", counts=None):
    assert client.put("/legend", json={"mapping": LEGEND}).status_code == 200
    assert client.put(
        f"/sheets/{sheet}/old-counts",
        json={"counts": SMALL_COUNTS if counts is None else counts}
    ).status_code == 200


def put_block(client, sheet, block, counts):
    return client.put(f"/sheets/{sheet}/blocks/{block}/counts",
                      json={"counts": counts})


# ---------- 分块清单登记 ----------

def test_register_blocks_once(client):
    seed(client)
    r = client.put("/sheets/S1/blocks", json={"blocks": ["A", "B", "C"]})
    assert r.status_code == 200
    assert r.json()["blocks"] == ["A", "B", "C"]
    # 每幅只能登记一次
    r = client.put("/sheets/S1/blocks", json={"blocks": ["X"]})
    assert r.status_code == 409
    assert r.json()["detail"]["existing_blocks"] == ["A", "B", "C"]
    info = client.get("/sheets/S1/blocks").json()
    assert info["blocks"] == ["A", "B", "C"]


def test_register_blocks_validation(client):
    seed(client)
    assert client.put("/sheets/S1/blocks", json={"blocks": []}).status_code == 422
    assert client.put("/sheets/S1/blocks",
                      json={"blocks": ["A", "A"]}).status_code == 422
    assert client.put("/sheets/S1/blocks",
                      json={"blocks": ["", "B"]}).status_code == 422


def test_block_counts_require_registration_and_known_block(client):
    seed(client)
    # 未登记分块清单 -> 409
    r = put_block(client, "S1", "A", {"0101": 10})
    assert r.status_code == 409
    client.put("/sheets/S1/blocks", json={"blocks": ["A", "B"]})
    # 未知分块 -> 404
    r = put_block(client, "S1", "Z", {"0101": 10})
    assert r.status_code == 404
    # 负数 -> 422
    r = put_block(client, "S1", "A", {"0101": -1})
    assert r.status_code == 422


# ---------- 齐报生效 ----------

def test_complete_reporting_adopts_sum(client):
    seed(client)
    client.put("/sheets/S1/blocks", json={"blocks": ["A", "B"]})
    r = put_block(client, "S1", "A", {"0101": 250, "0201": 100})
    assert r.status_code == 200
    # 只报一块，尚未齐报：有效复核数为空
    assert r.json()["effective_review_totals"] == {}
    assert r.json()["completeness"]["0101"]["complete"] is False
    r = put_block(client, "S1", "B", {"0101": 350, "0201": 0})  # 0 算已报
    assert r.status_code == 200
    assert r.json()["effective_review_totals"] == {"0101": 600, "0201": 100}
    comp = r.json()["completeness"]
    assert comp["0101"] == {"reported_blocks": 2, "total_blocks": 2,
                            "complete": True, "sum": 600}
    assert comp["0201"]["complete"] is True and comp["0201"]["sum"] == 100
    # 图幅查询与 /overlap 使用同一有效复核数
    sheet = client.get("/sheets/S1").json()
    assert sheet["review_totals"] == {"0101": 600, "0201": 100}
    assert sheet["reviewed_pixels"] == 700
    ov = client.get("/overlap", params={"sheet_id": "S1", "old_code": "01",
                                        "new_code": "0101"}).json()
    assert ov["exact"] is True and ov["value"] == 600
    assert ov["review_source"] == "分块齐报合计"


def test_zero_is_a_reported_value(client):
    seed(client, counts={"01": 100, "02": 50})
    client.put("/sheets/S1/blocks", json={"blocks": ["A", "B"]})
    put_block(client, "S1", "A", {"0101": 0})
    r = put_block(client, "S1", "B", {"0101": 0})
    # 两块都报 0：齐报，合计 0，列和锁定为 0，(01,0101) 确数 0
    assert r.json()["effective_review_totals"] == {"0101": 0}
    ov = client.get("/overlap", params={"sheet_id": "S1", "old_code": "01",
                                        "new_code": "0101"}).json()
    assert ov["exact"] is True and ov["value"] == 0


def test_retransmit_replaces_whole_block_report(client):
    seed(client)
    client.put("/sheets/S1/blocks", json={"blocks": ["A", "B"]})
    put_block(client, "S1", "A", {"0101": 250, "0201": 100})
    put_block(client, "S1", "B", {"0101": 350, "0201": 50})
    # 重传 A：整份替换，0201 从 A 的报表中移除 -> 0201 不齐
    r = put_block(client, "S1", "A", {"0101": 260})
    assert r.status_code == 200
    assert r.json()["effective_review_totals"] == {"0101": 610}
    assert r.json()["completeness"]["0201"]["complete"] is False
    # 原始分块报表里 A 已不含 0201
    info = client.get("/sheets/S1/blocks").json()
    assert info["reports"]["A"] == {"0101": 260}
    assert info["reports"]["B"] == {"0101": 350, "0201": 50}


def test_retransmit_missing_item_withdraws_total_keeps_manual(client):
    seed(client)
    client.put("/sheets/S1/blocks", json={"blocks": ["A", "B"]})
    put_block(client, "S1", "A", {"0101": 250})
    put_block(client, "S1", "B", {"0101": 350})
    assert client.get("/sheets/S1").json()["review_totals"] == {"0101": 600}
    # 手工补报同地类，与合计相等：并存但只计一次
    r = client.put("/sheets/S1/review-totals", json={"totals": {"0101": 600}})
    assert r.status_code == 200
    sheet = client.get("/sheets/S1").json()
    assert sheet["review_totals"] == {"0101": 600}
    assert sheet["manual_review_totals"] == {"0101": 600}
    assert sheet["reviewed_pixels"] == 600  # 只计一次
    # 重传 A 漏掉 0101：齐报合计撤下，手工数 600 沿用
    r = put_block(client, "S1", "A", {})
    assert r.status_code == 200
    assert r.json()["effective_review_totals"] == {"0101": 600}
    sheet = client.get("/sheets/S1").json()
    assert sheet["review_totals"] == {"0101": 600}
    ov = client.get("/overlap", params={"sheet_id": "S1", "old_code": "01",
                                        "new_code": "0101"}).json()
    assert ov["value"] == 600 and ov["review_source"] == "手工复核数"
    # 重新补齐后合计再次生效（与手工数相等）
    r = put_block(client, "S1", "A", {"0101": 250})
    assert r.json()["effective_review_totals"] == {"0101": 600}


# ---------- 手工数与合计的一致性 ----------

def test_manual_equal_sum_counted_once(client):
    seed(client)
    client.put("/sheets/S1/blocks", json={"blocks": ["A", "B"]})
    client.put("/sheets/S1/review-totals", json={"totals": {"0101": 600}})
    put_block(client, "S1", "A", {"0101": 250})
    r = put_block(client, "S1", "B", {"0101": 350})
    assert r.status_code == 200
    assert r.json()["effective_review_totals"] == {"0101": 600}
    assert client.get("/sheets/S1").json()["reviewed_pixels"] == 600


def test_manual_mismatch_sum_rejects_block_write_and_keeps_state(client):
    seed(client)
    client.put("/sheets/S1/blocks", json={"blocks": ["A", "B"]})
    client.put("/sheets/S1/review-totals", json={"totals": {"0101": 590}})
    put_block(client, "S1", "A", {"0101": 250})
    # 齐报合计 600 != 手工 590 -> 409，整次拒绝
    r = put_block(client, "S1", "B", {"0101": 350})
    assert r.status_code == 409
    problems = r.json()["detail"]["affected_sheets"][0]["problems"]
    assert any("590" in p and "600" in p for p in problems)
    # B 的报表未写入，有效复核数仍是手工 590；A 为此前已提交的独立事务，保持不变
    sheet = client.get("/sheets/S1").json()
    assert sheet["review_totals"] == {"0101": 590}
    info = client.get("/sheets/S1/blocks").json()
    assert info["reports"].get("B", {}) == {}
    assert info["reports"]["A"] == {"0101": 250}


def test_manual_correction_mismatch_rejected(client):
    seed(client)
    client.put("/sheets/S1/blocks", json={"blocks": ["A", "B"]})
    put_block(client, "S1", "A", {"0101": 250})
    put_block(client, "S1", "B", {"0101": 350})  # 齐报合计 600 生效
    # 手工补报与合计不等 -> 拒绝，原合计保留
    r = client.put("/sheets/S1/review-totals", json={"totals": {"0101": 601}})
    assert r.status_code == 409
    sheet = client.get("/sheets/S1").json()
    assert sheet["manual_review_totals"] == {}
    assert sheet["review_totals"] == {"0101": 600}
    # 改成相等值可正常并存
    r = client.put("/sheets/S1/review-totals", json={"totals": {"0101": 600}})
    assert r.status_code == 200
    assert client.get("/sheets/S1").json()["review_totals"] == {"0101": 600}


def test_infeasible_block_total_rejected_and_unchanged(client):
    seed(client, counts={"01": 100, "02": 50})
    client.put("/sheets/S1/blocks", json={"blocks": ["A", "B"]})
    put_block(client, "S1", "A", {"0101": 40})  # 此时未齐报，不产生合计，可提交
    # B 重传后合计 120 > 图幅像元 150 中 01 的 100（0101 只能来自 01）-> 409
    r = put_block(client, "S1", "B", {"0101": 80})
    assert r.status_code == 409
    problems = r.json()["detail"]["affected_sheets"][0]["problems"]
    assert any("无法同时满足" in p or "超过" in p for p in problems)
    # B 的报表随本次事务回滚；A 为此前已提交数据，保持不变；有效复核数仍为空
    info = client.get("/sheets/S1/blocks").json()
    assert info["reports"]["A"] == {"0101": 40}
    assert info["reports"].get("B", {}) == {}
    assert client.get("/sheets/S1").json()["review_totals"] == {}


def test_old_count_change_revalidates_block_totals(client):
    seed(client)
    client.put("/sheets/S1/blocks", json={"blocks": ["A", "B"]})
    put_block(client, "S1", "A", {"0101": 250})
    put_block(client, "S1", "B", {"0101": 350})  # 合计 600，0101 只能来自 01
    # 把 01 的旧计数改小到容不下 600 -> 409
    r = client.put("/sheets/S1/old-counts",
                   json={"counts": {"01": 500, "02": 500}})
    assert r.status_code == 409
    # 原旧计数与有效复核数均保留
    sheet = client.get("/sheets/S1").json()
    assert sheet["old_counts"] == {"01": 1000, "02": 500}
    assert sheet["review_totals"] == {"0101": 600}


def test_legend_swap_revalidates_block_totals(client):
    seed(client, counts={"01": 100, "02": 50})
    client.put("/sheets/S1/blocks", json={"blocks": ["A", "B"]})
    put_block(client, "S1", "A", {"0101": 60})
    put_block(client, "S1", "B", {"0101": 40})  # 0101=100 锁定全部 01 像元
    # 换表后 01 只能入 0103：0101 无来源 -> 无解，整次拒绝
    r = client.put("/legend", json={"mapping": {"01": ["0103"],
                                                "02": ["0201"]}})
    assert r.status_code == 409
    assert "S1" in [s["sheet_id"] for s in r.json()["detail"]["affected_sheets"]]
    assert client.get("/legend").json()["version"] == 1
    ov = client.get("/overlap", params={"sheet_id": "S1", "old_code": "01",
                                        "new_code": "0101"}).json()
    assert ov["value"] == 100  # 原有效数据不变


# ---------- 查询口径与多图幅 ----------

def test_overlap_and_sheet_share_committed_effective_totals(client):
    seed(client)
    client.put("/sheets/S1/blocks", json={"blocks": ["A", "B", "C"]})
    put_block(client, "S1", "A", {"0101": 100})
    put_block(client, "S1", "B", {"0101": 200})
    # C 未报：查询口径里 0101 未复核 -> 区间
    ov = client.get("/overlap", params={"sheet_id": "S1", "old_code": "01",
                                        "new_code": "0101"}).json()
    assert (ov["min"], ov["max"], ov["exact"]) == (0, 1000, False)
    assert ov["review_source"] == "未复核"
    sheet = client.get("/sheets/S1").json()
    assert sheet["review_totals"] == {}
    # 补齐后两个接口同时变确数
    put_block(client, "S1", "C", {"0101": 300})
    ov = client.get("/overlap", params={"sheet_id": "S1", "old_code": "01",
                                        "new_code": "0101"}).json()
    assert ov["value"] == 600
    assert client.get("/sheets/S1").json()["review_totals"] == {"0101": 600}
    # /sheets 汇总也使用同一口径
    listed = [s for s in client.get("/sheets").json()["sheets"]
              if s["sheet_id"] == "S1"][0]
    assert listed["review_pixels"] == 600 and listed["block_count"] == 3


def test_blocks_only_sheet_is_listed_and_404_unknown(client):
    seed(client)
    # 只登记分块、暂无任何计数：图幅仍可列出
    client.put("/sheets/S9/blocks", json={"blocks": ["A"]})
    ids = {s["sheet_id"] for s in client.get("/sheets").json()["sheets"]}
    assert "S9" in ids
    assert client.get("/sheets/S9/blocks").status_code == 200
    assert client.get("/sheets/NOPE/blocks").status_code == 404


def test_empty_retransmit_withdraws_all(client):
    seed(client)
    client.put("/sheets/S1/blocks", json={"blocks": ["A", "B"]})
    put_block(client, "S1", "A", {"0101": 250})
    put_block(client, "S1", "B", {"0101": 350})
    # 空表重传 A：A 的报表清空，所有地类都不再齐报
    r = put_block(client, "S1", "A", {})
    assert r.status_code == 200
    assert r.json()["effective_review_totals"] == {}
    info = client.get("/sheets/S1/blocks").json()
    assert info["reports"]["A"] == {}
    ov = client.get("/overlap", params={"sheet_id": "S1", "old_code": "01",
                                        "new_code": "0101"}).json()
    assert ov["exact"] is False
