"""批号纠错开新版本、动作沿用；门店最小信息视图与时间线一致性。"""

from tests.conftest import create_recall, EFFECTIVE


def _supply_y(client, tid):
    client.post("/supplies", json={
        "supply_id": f"SUP_{tid}_Y", "brand_id": "A", "product_code": "P1",
        "product_name": "感冒灵颗粒", "batch_no": "LOT-Y",
        "target_type": "store", "target_id": tid,
        "supplied_at": "2026-07-15T08:00:00+00:00",
    })


def test_batch_correction_opens_version_and_preserves_actions(world):
    client = world
    created = create_recall(client)
    v1 = created["version_id"]
    # v1 中 S1 完成停售
    client.post("/receipts", json={
        "version_id": v1, "target_type": "store", "target_id": "S1",
        "disposition": "stop_sale", "device_id": "d1", "journal_no": 1,
        "submitted_at": "2026-09-10T05:00:00+00:00",
    }, headers={"X-Actor-Id": "store_s1"})

    # 批号纠错：实际批号为 LOT-Y，只供货到 S1、S2
    _supply_y(client, "S1")
    _supply_y(client, "S2")

    resp = client.post(f"/recalls/{created['recall_id']}/corrections", json={
        "product_code": "P1", "product_name": "感冒灵颗粒", "batch_no": "LOT-Y",
        "risk_level": "high", "effective_at": EFFECTIVE,
        "reason": "企业上报批号录入错误，LOT-X 更正为 LOT-Y",
    }, headers={"X-Actor-Id": "hq_a"})
    assert resp.status_code == 201, resp.get_json()
    v2 = resp.get_json()["version_id"]
    assert resp.get_json()["version_no"] == 2

    cov2 = client.get(f"/versions/{v2}/coverage", headers={"X-Actor-Id": "hq_a"}).get_json()
    assert cov2["batch_no"] == "LOT-Y"
    assert cov2["totals"]["all_entries"] == 2          # 新版本只含 LOT-Y 供货入口
    assert cov2["totals"]["final_disposed"] == 1       # S1 在 v1 的停售被沿用
    assert cov2["totals"]["coverage_rate"] == 0.5

    # v1 仍冻结可读，历史动作未被抹掉
    cov1 = client.get(f"/versions/{v1}/coverage", headers={"X-Actor-Id": "hq_a"}).get_json()
    assert cov1["batch_no"] == "LOT-X"
    assert cov1["totals"]["final_disposed"] == 1

    # 时间线记录沿用来源
    tl2 = client.get(
        f"/versions/{v2}/targets/store/S1/timeline", headers={"X-Actor-Id": "hq_a"}
    ).get_json()
    assert any(e["kind"] == "disposition_carried" for e in tl2["events"])
    # 原始回执仍只挂在 v1，新版本不复制动作
    assert tl2["receipts"] == []
    tl1 = client.get(
        f"/versions/{v1}/targets/store/S1/timeline", headers={"X-Actor-Id": "hq_a"}
    ).get_json()
    assert any(r["disposition"] == "stop_sale" for r in tl1["receipts"])


def test_carried_disposition_cancels_dup_notification(world):
    """新版本沿用处置的入口不再重复触达。"""
    client = world
    created = create_recall(client)
    client.post("/receipts", json={
        "version_id": created["version_id"], "target_type": "store", "target_id": "S1",
        "disposition": "stop_sale", "device_id": "d1", "journal_no": 1,
        "submitted_at": "2026-09-10T05:00:00+00:00",
    }, headers={"X-Actor-Id": "store_s1"})
    _supply_y(client, "S1")
    resp = client.post(f"/recalls/{created['recall_id']}/corrections", json={
        "product_code": "P1", "product_name": "感冒灵颗粒", "batch_no": "LOT-Y",
        "risk_level": "high", "effective_at": EFFECTIVE, "reason": "批号录错",
    }, headers={"X-Actor-Id": "hq_a"})
    v2 = resp.get_json()["version_id"]
    notes = client.get("/notifications", headers={"X-Actor-Id": "hq_a"}).get_json()["notifications"]
    s1_pending = [
        n for n in notes
        if n["version_id"] == v2 and n["target_id"] == "S1" and n["status"] == "pending"
    ]
    assert s1_pending == []


def test_correction_requires_reason(world):
    client = world
    created = create_recall(client)
    resp = client.post(f"/recalls/{created['recall_id']}/corrections", json={
        "product_code": "P1", "product_name": "n", "batch_no": "LOT-Y",
        "risk_level": "high", "effective_at": EFFECTIVE,
    }, headers={"X-Actor-Id": "hq_a"})
    assert resp.status_code == 400
    assert resp.get_json()["error"] == "REASON_REQUIRED"


def test_store_minimal_info_view(world):
    """门店只拿到完成处置所需信息，看不到其他门店或定责全景。"""
    client = world
    create_recall(client)
    resp = client.get("/me/tasks", headers={"X-Actor-Id": "store_s2"})
    assert resp.status_code == 200
    tasks = resp.get_json()["tasks"]
    assert len(tasks) == 1
    task = tasks[0]
    assert task["batch_no"] == "LOT-X"
    assert task["risk_level"] == "high"
    assert set(task) == {
        "version_id", "product_name", "batch_no", "risk_level",
        "effective_at", "due_at", "why", "latest_disposition", "allowed_replies",
    }

    # 无关门店 S9 无任务
    empty = client.get("/me/tasks", headers={"X-Actor-Id": "store_other"}).get_json()
    assert empty["tasks"] == []


def test_operator_tasks_show_transferred_responsibility(world):
    client = world
    create_recall(client)
    resp = client.get("/me/tasks", headers={"X-Actor-Id": "op_new_qa"})
    tasks = resp.get_json()["tasks"]
    # 新主体负责 S2（转交）与 E1（直营线上入口）
    outlets = {(t["target_type"], t["target_id"]) for t in tasks}
    assert ("store", "S2") in outlets
    assert ("online_entry", "E1") in outlets


def test_timeline_consistent_with_frozen_relations(world):
    """回执时间线：定责事实在前、处置动作在后，与版本冻结关系一致。"""
    client = world
    vid = create_recall(client)["version_id"]
    client.post("/receipts", json={
        "version_id": vid, "target_type": "store", "target_id": "S1",
        "disposition": "quarantined", "device_id": "d1", "journal_no": 1,
        "submitted_at": "2026-09-10T05:00:00+00:00", "detail": "已移入不合格品区",
    }, headers={"X-Actor-Id": "store_s1"})

    body = client.get(
        f"/versions/{vid}/targets/store/S1/timeline", headers={"X-Actor-Id": "hq_a"}
    ).get_json()
    kinds = [e["kind"] for e in body["events"]]
    assert kinds[0] == "attribution_resolved"
    assert "receipt_quarantined" in kinds
    assert body["receipts"][0]["disposition"] == "quarantined"
    assert body["receipts"][0]["detail"] == "已移入不合格品区"
