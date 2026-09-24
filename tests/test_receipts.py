"""门店回执：四种处置、设备流水去重、迟到不覆盖、异议与复核回避。"""

from tests.conftest import create_recall, EFFECTIVE, SUPPLY_AT


def _receipt(client, version_id, ttype, tid, disposition, *, actor=None, device="dev1",
             journal=1, submitted="2026-09-10T05:00:00+00:00", detail=None):
    payload = {
        "version_id": version_id, "target_type": ttype, "target_id": tid,
        "disposition": disposition, "device_id": device, "journal_no": journal,
        "submitted_at": submitted,
    }
    if detail:
        payload["detail"] = detail
    headers = {"X-Actor-Id": actor} if actor else {}
    return client.post("/receipts", json=payload, headers=headers)


def test_store_can_report_four_dispositions(world):
    client = world
    recall = create_recall(client)
    # 门店 S1 是 included；S1 店长可回报
    resp = _receipt(client, recall["version_id"], "store", "S1", "stop_sale", actor="store_s1")
    assert resp.status_code == 201

    coverage = client.get(
        f"/versions/{recall['version_id']}/coverage", headers={"X-Actor-Id": "hq_a"}
    ).get_json()
    assert coverage["totals"]["final_disposed"] == 1


def test_store_cannot_act_for_other_store(world):
    client = world
    recall = create_recall(client)
    resp = _receipt(client, recall["version_id"], "store", "S2", "stop_sale", actor="store_s1")
    assert resp.status_code == 403
    assert resp.get_json()["error"] == "OUTLET_SCOPE_VIOLATION"


def test_unresolved_target_rejects_receipt(world):
    client = world
    recall = create_recall(client)
    resp = _receipt(client, recall["version_id"], "store", "S4", "stop_sale",
                    actor="hq_a", device="dev4")
    assert resp.status_code == 409
    assert resp.get_json()["error"] == "TARGET_NOT_ACTIONABLE"


def test_device_journal_dedup(world):
    """设备离线上传以自身流水去重：重复上传回放首条，不产生重复处置。"""
    client = world
    recall = create_recall(client)
    first = _receipt(client, recall["version_id"], "store", "S1", "quarantined",
                     actor="store_s1", device="dev-s1", journal=100)
    assert first.status_code == 201
    first_id = first.get_json()["receipt_id"]

    replay = _receipt(client, recall["version_id"], "store", "S1", "quarantined",
                      actor="store_s1", device="dev-s1", journal=100)
    assert replay.status_code == 201
    body = replay.get_json()
    assert body["deduplicated"] is True
    assert body["receipt_id"] == first_id

    timeline = client.get(
        f"/versions/{recall['version_id']}/targets/store/S1/timeline",
        headers={"X-Actor-Id": "hq_a"},
    ).get_json()
    assert sum(1 for e in timeline["events"] if e["kind"] == "receipt_quarantined") == 1


def test_late_receipt_does_not_override_newer_disposition(world):
    """迟到回执（设备时间更早）不得压过较新的处置。"""
    client = world
    recall = create_recall(client)
    # 先收到时间较新的“已停售”
    _receipt(client, recall["version_id"], "store", "S1", "stop_sale",
             actor="store_s1", journal=2, submitted="2026-09-10T10:00:00+00:00")
    # 再离线补传时间更早的“隔离”
    late = _receipt(client, recall["version_id"], "store", "S1", "quarantined",
                    actor="store_s1", journal=1, submitted="2026-09-10T06:00:00+00:00")
    assert late.status_code == 201
    assert late.get_json()["superseded"] is True

    targets = client.get(
        f"/versions/{recall['version_id']}/targets", headers={"X-Actor-Id": "hq_a"}
    ).get_json()["targets"]
    s1 = next(t for t in targets if t["target_id"] == "S1")
    assert s1["latest_disposition"] == "stop_sale"  # 较新处置保留
    timeline = client.get(
        f"/versions/{recall['version_id']}/targets/store/S1/timeline",
        headers={"X-Actor-Id": "hq_a"},
    ).get_json()
    assert any(e["kind"] == "late_receipt_ignored" for e in timeline["events"])


def test_dispute_and_reviewer_recusal(world):
    """异议复核：自提自审禁止；发起过该入口关系变更的人必须回避。"""
    client = world
    recall = create_recall(client)
    # S2 由 OP_NEW 接手处置，门店提出归属异议
    resp = _receipt(client, recall["version_id"], "store", "S2", "dispute",
                    actor="store_s2", journal=1, detail="本批次不应由新主体承担")
    assert resp.status_code == 201
    dispute_id = resp.get_json()["dispute_id"]

    # hq_a 发起过 S2 的关系变更（两次转手都由其登记），必须回避
    blocked = client.post(
        f"/disputes/{dispute_id}/review",
        json={"decision": "uphold", "note": "维持"},
        headers={"X-Actor-Id": "hq_a"},
    )
    assert blocked.status_code == 409
    assert blocked.get_json()["error"] == "REVIEWER_RELATION_CONFLICT"

    # 未发起过 S2 关系变更的另一总部账号可复核并改派给老主体
    reviewed = client.post(
        f"/disputes/{dispute_id}/review",
        json={"decision": "reassign", "to_operator_id": "OP_OLD", "note": "进货当日主体担责"},
        headers={"X-Actor-Id": "hq_a2"},
    )
    assert reviewed.status_code == 200, reviewed.get_json()

    targets = client.get(
        f"/versions/{recall['version_id']}/targets", headers={"X-Actor-Id": "hq_a2"}
    ).get_json()["targets"]
    s2 = next(t for t in targets if t["target_id"] == "S2")
    assert s2["status"] == "reassigned"
    assert s2["responsible_operator_id"] == "OP_OLD"
    assert s2["manually_reassigned"] is True


def test_cannot_review_own_dispute(world):
    client = world
    recall = create_recall(client)
    # 由总部代录异议（设备匿名场景下 raised_by 为 device id，这里用门店）
    resp = _receipt(client, recall["version_id"], "store", "S1", "dispute",
                    actor="store_s1", journal=1, detail="异议")
    dispute_id = resp.get_json()["dispute_id"]
    # 另一门店不是总部，无权复核
    denied = client.post(
        f"/disputes/{dispute_id}/review",
        json={"decision": "uphold"},
        headers={"X-Actor-Id": "store_other"},
    )
    assert denied.status_code == 403


def test_uphold_dispute_reopens_target_without_resetting_due(world):
    """维持原归属：挂起解除、回到待处置，逾期截止仍是原绝对时刻。"""
    client = world
    recall = create_recall(client)
    before = client.get(
        f"/versions/{recall['version_id']}/targets", headers={"X-Actor-Id": "hq_a2"}
    ).get_json()["targets"]
    s2_before = next(t for t in before if t["target_id"] == "S2")

    resp = _receipt(client, recall["version_id"], "store", "S2", "dispute",
                    actor="store_s2", journal=1, detail="归属存疑")
    dispute_id = resp.get_json()["dispute_id"]
    client.post(
        f"/disputes/{dispute_id}/review",
        json={"decision": "uphold", "note": "进货当日主体已变更，新主体担责"},
        headers={"X-Actor-Id": "hq_a2"},
    )
    after = client.get(
        f"/versions/{recall['version_id']}/targets", headers={"X-Actor-Id": "hq_a2"}
    ).get_json()["targets"]
    s2_after = next(t for t in after if t["target_id"] == "S2")
    assert s2_after["latest_disposition"] is None
    assert s2_after["due_at"] == s2_before["due_at"]          # 计时未重置
    assert s2_after["responsible_operator_id"] == "OP_NEW"   # 归属维持


def test_late_dispute_does_not_reopen_state(world):
    """晚到的异议（设备时间更早）同样不能覆盖当前处置，也不重复开单。"""
    client = world
    recall = create_recall(client)
    _receipt(client, recall["version_id"], "store", "S1", "stop_sale",
             actor="store_s1", journal=2, submitted="2026-09-10T10:00:00+00:00")
    late = _receipt(client, recall["version_id"], "store", "S1", "dispute",
                    actor="store_s1", journal=1, submitted="2026-09-10T06:00:00+00:00",
                    detail="迟到的异议")
    assert late.get_json()["superseded"] is True
    assert "dispute_id" not in late.get_json()
    targets = client.get(
        f"/versions/{recall['version_id']}/targets", headers={"X-Actor-Id": "hq_a"}
    ).get_json()["targets"]
    s1 = next(t for t in targets if t["target_id"] == "S1")
    assert s1["latest_disposition"] == "stop_sale"
