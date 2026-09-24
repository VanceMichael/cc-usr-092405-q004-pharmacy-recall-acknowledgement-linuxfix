"""双时点定责：供货时与生效时分别回放有效关系，入口不得漏判或错派。"""

from tests.conftest import create_recall


def _targets(client, version_id, actor="hq_a"):
    resp = client.get(f"/versions/{version_id}/targets", headers={"X-Actor-Id": actor})
    assert resp.status_code == 200
    return {(t["target_type"], t["target_id"]): t for t in resp.get_json()["targets"]}


def test_attribution_categories_and_reasons(world):
    client = world
    recall = create_recall(client)
    targets = _targets(client, recall["version_id"])

    s1 = targets[("store", "S1")]
    assert s1["status"] == "included"
    assert s1["responsible_operator_id"] == "OP_OLD"
    assert s1["reason_code"] == "SUPPLY_OPERATOR_STILL_RESPONSIBLE"

    s2 = targets[("store", "S2")]
    assert s2["status"] == "transferred"
    assert s2["responsible_operator_id"] == "OP_NEW"          # 今天的主体负责处置
    assert s2["historical_operator_id"] == "OP_OLD"          # 进货当日主体留存备查
    assert "转交新主体" in s2["reason_detail"]

    s3 = targets[("store", "S3")]
    assert s3["status"] == "historical"
    assert s3["responsible_operator_id"] == "OP_OLD"         # 关店也不能漏掉，旧主体兜底
    assert s3["reason_code"] == "NO_VALID_RELATION_AT_EFFECTIVE_TIME"

    s4 = targets[("store", "S4")]
    assert s4["status"] == "unresolved"
    assert s4["responsible_operator_id"] is None
    assert "人工改派" in s4["reason_detail"]

    s5 = targets[("store", "S5")]
    assert s5["status"] == "excluded"
    assert s5["reason_code"] == "CURRENT_OPERATOR_OUTSIDE_BRAND"
    assert s5["historical_operator_id"] == "OP_OLD"

    e1 = targets[("online_entry", "E1")]
    assert e1["status"] == "included"
    assert e1["responsible_operator_id"] == "OP_NEW"


def test_no_silent_missing_entry(world):
    """每个物理入口（含线上入口）都必须出现，漏掉即定责失败。"""
    client = world
    recall = create_recall(client)
    targets = _targets(client, recall["version_id"])
    assert {key[1] for key in targets} == {"S1", "S2", "S3", "S4", "S5", "E1"}


def test_risk_entries_surface_breaks(world):
    client = world
    recall = create_recall(client)
    resp = client.get(
        f"/versions/{recall['version_id']}/risk-entries", headers={"X-Actor-Id": "hq_a"}
    )
    kinds = {(r["target_id"], r["risk_kind"]) for r in resp.get_json()["risk_entries"]}
    assert ("S4", "attribution_broken") in kinds
    assert ("S5", "moved_outside_brand") in kinds


def test_brand_isolation_hq_cannot_see_other_brand(world):
    client = world
    recall = create_recall(client)
    resp = client.get(
        f"/versions/{recall['version_id']}/coverage", headers={"X-Actor-Id": "hq_b"}
    )
    assert resp.status_code == 403
    assert resp.get_json()["error"] == "BRAND_SCOPE_VIOLATION"
