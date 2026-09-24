"""高风险逾期层级升级与停服恢复接续。

截止/升级时刻全部锚定召回生效的绝对时间：
高风险 SLA 24h；逾期当时 L1，逾期+12h L2，逾期+36h L3。
"""

from tests.conftest import create_recall, L2_AT, L3_AT


def test_no_escalation_before_due(world):
    client = world
    recall = create_recall(client)
    # 截止前任意时刻扫描：无升级，只有初始待发通知
    resp = client.post("/sweep?at=2026-09-10T12:00:00+00:00")
    assert resp.status_code == 200
    assert resp.get_json()["escalations"] == 0
    assert resp.get_json()["dispatched"] >= 1


def test_level1_then_level2_then_level3_by_absolute_time(world):
    client = world
    recall = create_recall(client)
    vid = recall["version_id"]

    at_l1 = client.post("/sweep?at=2026-09-11T00:00:00+00:00").get_json()
    assert at_l1["escalations"] == 4  # S1,S2,S3,E1 四个可处置对象（S4 待定、S5 排除）

    cov1 = client.get(f"/versions/{vid}/coverage", headers={"X-Actor-Id": "hq_a"}).get_json()
    assert cov1["escalated_by_level"] == {"L1": 4}

    client.post(f"/sweep?at={L2_AT}")
    cov2 = client.get(f"/versions/{vid}/coverage", headers={"X-Actor-Id": "hq_a"}).get_json()
    assert cov2["escalated_by_level"] == {"L1": 4, "L2": 4}

    client.post(f"/sweep?at={L3_AT}")
    risk = client.get(f"/versions/{vid}/risk-entries", headers={"X-Actor-Id": "hq_a"}).get_json()
    escalated = [r for r in risk["risk_entries"] if r["risk_kind"] == "escalation_unresolved"]
    assert len(escalated) == 4
    assert all(r["escalated_level"] == 3 for r in escalated)


def test_sweep_is_idempotent(world):
    """同一时刻反复扫描不产生重复升级/重复通知。"""
    client = world
    recall = create_recall(client)
    first = client.post("/sweep?at=2026-09-11T01:00:00+00:00").get_json()
    second = client.post("/sweep?at=2026-09-11T01:00:00+00:00").get_json()
    assert first["escalations"] == 4
    assert second["escalations"] == 0
    assert second["dispatched"] == 0


def test_outage_resume_picks_up_at_original_progress(world):
    """停服跨越两个升级点：恢复后一次扫描补齐 L1→L2→L3，逾期不重新计时。"""
    client = world
    recall = create_recall(client)
    vid = recall["version_id"]
    # 召回刚生效时服务停摆：初始通知仍 pending
    pending = client.get("/notifications", headers={"X-Actor-Id": "hq_a"}).get_json()["notifications"]
    assert all(n["status"] == "pending" for n in pending)
    initial_count = len(pending)

    # 直到 L3 时刻才恢复：一次 sweep 既要补发初始待发，也要连补两级升级并随即发出
    result = client.post(f"/sweep?at={L3_AT}").get_json()
    # 4 条处置触达 + 1 条旧主体备查 + 每对象 3 条升级（4×3）
    assert result["dispatched"] == initial_count + 12
    # 每个可处置对象补 L1、L2、L3 三条升级
    assert result["escalations"] == 12

    cov = client.get(f"/versions/{vid}/coverage", headers={"X-Actor-Id": "hq_a"}).get_json()
    assert cov["escalated_by_level"] == {"L1": 4, "L2": 4, "L3": 4}
    assert cov["totals"]["overdue_open"] == 4

    # 再次扫描为空：进度已经接续，无重复
    again = client.post(f"/sweep?at={L3_AT}").get_json()
    assert again == {"at": L3_AT, "dispatched": 0, "escalations": 0}


def test_final_disposition_stops_escalation(world):
    """完成处置的对象不再升级；其他对象继续。"""
    client = world
    recall = create_recall(client)
    vid = recall["version_id"]
    # S1 在截止前回报停售
    client.post("/receipts", json={
        "version_id": vid, "target_type": "store", "target_id": "S1",
        "disposition": "stop_sale", "device_id": "dev-s1", "journal_no": 1,
        "submitted_at": "2026-09-10T20:00:00+00:00",
    }, headers={"X-Actor-Id": "store_s1"})

    client.post(f"/sweep?at={L3_AT}")
    targets = client.get(f"/versions/{vid}/targets", headers={"X-Actor-Id": "hq_a"}).get_json()["targets"]
    s1 = next(t for t in targets if t["target_id"] == "S1")
    assert s1["escalated_level"] == 0
    s2 = next(t for t in targets if t["target_id"] == "S2")
    assert s2["escalated_level"] == 3

    cov = client.get(f"/versions/{vid}/coverage", headers={"X-Actor-Id": "hq_a"}).get_json()
    assert cov["totals"]["coverage_rate"] == 0.25
    assert cov["totals"]["final_disposed"] == 1


def test_medium_and_low_risk_no_escalation(world):
    client = world
    recall = create_recall(client, risk="medium")
    result = client.post("/sweep?at=2026-12-31T00:00:00+00:00").get_json()
    assert result["escalations"] == 0
