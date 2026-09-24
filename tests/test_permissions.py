"""权限边界：品牌可见域、总部专属操作、待定入口人工改派闭环。"""

from tests.conftest import create_recall


def test_hq_reassigns_unresolved_target(world):
    """责任断裂入口由总部人工改派后进入可处置，覆盖率含人工改派计数。"""
    client = world
    vid = create_recall(client)["version_id"]

    denied = client.post(
        f"/versions/{vid}/targets/store/S4/reassign",
        json={"to_operator_id": "OP_OLD", "reason": "进货台账显示老主体收货"},
        headers={"X-Actor-Id": "op_new_qa"},
    )
    assert denied.status_code == 403

    resp = client.post(
        f"/versions/{vid}/targets/store/S4/reassign",
        json={"to_operator_id": "OP_OLD", "reason": "进货台账显示老主体收货"},
        headers={"X-Actor-Id": "hq_a"},
    )
    assert resp.status_code == 200, resp.get_json()

    targets = client.get(f"/versions/{vid}/targets", headers={"X-Actor-Id": "hq_a"}).get_json()["targets"]
    s4 = next(t for t in targets if t["target_id"] == "S4")
    assert s4["status"] == "reassigned"
    assert s4["responsible_operator_id"] == "OP_OLD"

    # 改派后 S4 可由新责任主体回报
    receipt = client.post("/receipts", json={
        "version_id": vid, "target_type": "store", "target_id": "S4",
        "disposition": "not_found", "device_id": "d4", "journal_no": 1,
        "submitted_at": "2026-09-10T09:00:00+00:00",
    }, headers={"X-Actor-Id": "hq_a"})
    assert receipt.status_code == 201

    cov = client.get(f"/versions/{vid}/coverage", headers={"X-Actor-Id": "hq_a"}).get_json()
    assert cov["totals"]["manually_reassigned"] == 1
    assert cov["by_disposition"] == {"not_found": 1}


def test_cannot_reassign_outside_brand(world):
    client = world
    vid = create_recall(client)["version_id"]
    resp = client.post(
        f"/versions/{vid}/targets/store/S4/reassign",
        json={"to_operator_id": "OP_B", "reason": "x"},
        headers={"X-Actor-Id": "hq_a"},
    )
    assert resp.status_code == 403
    assert resp.get_json()["error"] == "BRAND_SCOPE_VIOLATION"


def test_notification_outbox_hq_only(world):
    client = world
    create_recall(client)
    as_store = client.get("/notifications", headers={"X-Actor-Id": "store_s1"})
    assert as_store.status_code == 403
    as_hq = client.get("/notifications", headers={"X-Actor-Id": "hq_a"})
    assert as_hq.status_code == 200


def test_relation_change_hq_only_and_brand_scoped(world):
    client = world
    resp = client.post("/relations", json={
        "target_type": "store", "target_id": "S1", "operator_id": "OP_NEW",
        "relation_kind": "direct", "valid_from": "2026-09-01T00:00:00+00:00",
    }, headers={"X-Actor-Id": "store_s1"})
    assert resp.status_code == 403

    # B 品牌总部不能改 A 品牌门店的关系
    denied = client.post("/relations", json={
        "target_type": "store", "target_id": "S1", "operator_id": "OP_B",
        "relation_kind": "franchise", "valid_from": "2026-09-01T00:00:00+00:00",
    }, headers={"X-Actor-Id": "hq_b"})
    assert denied.status_code == 403


def test_unauthenticated_business_call_rejected(world):
    client = world
    resp = client.get("/me/tasks")
    assert resp.status_code == 401
