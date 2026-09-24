"""HTTP 边界冒烟：鉴权头、事务、错误码与停机补发 tick。"""

from pharmacy_identity import create_app


def test_http_recall_lifecycle(engine):
    sent = []
    app = create_app(engine, transport=sent.append)
    c = app.test_client()

    assert c.post("/v1/admin/brands", json={"brand_id": "B", "name": "安心药房"}).status_code == 201
    c.post("/v1/admin/entities", json={"entity_id": "EA", "brand_id": "B", "name": "甲主体"})
    c.post("/v1/admin/stores", json={"store_id": "S1", "brand_id": "B",
                                     "name": "中山北路店", "entry_type": "physical"})
    c.post("/v1/admin/actors", json={"actor_id": "hqB", "role": "hq", "brand_id": "B"})
    c.post("/v1/admin/actors", json={"actor_id": "dev1", "role": "store", "store_id": "S1"})
    c.post("/v1/admin/relations", json={
        "brand_id": "B", "store_id": "S1", "entity_id": "EA",
        "relation_type": "direct", "valid_from": "2025-01-01T00:00:00+00:00",
        "recorded_by": "sys"})
    c.post("/v1/admin/supplies", json={
        "brand_id": "B", "store_id": "S1", "product_code": "P1", "product_name": "某药",
        "batch_no": "X1", "supplied_at": "2026-01-01T00:00:00+00:00"})

    # 缺鉴权头 → 403
    assert c.post("/v1/recalls", json={}).status_code == 403

    r = c.post("/v1/recalls", headers={"X-Actor-Id": "hqB"}, json={
        "brand_id": "B", "product_code": "P1", "product_name": "某药",
        "batch_no": "X1", "risk_level": "high",
        "effective_at": "2026-09-20T00:00:00+00:00"})
    assert r.status_code == 201
    body = r.get_json()
    target_id = body["targets"][0]["target_id"]

    # 门店处置回报
    rr = c.post(f"/v1/targets/{target_id}/receipts", headers={"X-Actor-Id": "dev1"}, json={
        "device_id": "d1", "client_seq": 1, "action": "quarantined",
        "action_at": "2026-09-20T02:00:00+00:00"})
    assert rr.status_code == 200 and rr.get_json()["applied"] is True

    # 同流水重放 → 幂等
    rr2 = c.post(f"/v1/targets/{target_id}/receipts", headers={"X-Actor-Id": "dev1"}, json={
        "device_id": "d1", "client_seq": 1, "action": "quarantined",
        "action_at": "2026-09-20T02:00:00+00:00"})
    assert rr2.get_json()["deduplicated"] is True

    # 门店最小知情视图
    brief = c.get(f"/v1/targets/{target_id}/briefing", headers={"X-Actor-Id": "dev1"})
    assert brief.status_code == 200
    assert "coverage" not in brief.get_json()

    # 总部看板限品牌
    dash = c.get(f"/v1/recalls/{body['recall_id']}/dashboard", headers={"X-Actor-Id": "hqB"})
    assert dash.status_code == 200
    assert dash.get_json()["versions"][0]["coverage"]["completed"] == 1


def test_health_still_ok(engine):
    app = create_app(engine)
    assert app.test_client().get("/health").get_json() == {"status": "ok", "storage": "sqlite"}
