"""测试夹具：在内存 SQLite 上建表并构造双品牌、多主体、多入口的身份世界。"""

import pytest
from sqlalchemy import create_engine

from pharmacy_identity import create_app
from pharmacy_identity.schema import metadata

T_BEFORE = "2026-01-01T00:00:00+00:00"
HANDOVER = "2026-08-01T00:00:00+00:00"
SUPPLY_AT = "2026-07-15T08:00:00+00:00"
EFFECTIVE = "2026-09-10T00:00:00+00:00"
# 高风险截止 = 生效 + 24h。
DUE = "2026-09-11T00:00:00+00:00"
L2_AT = "2026-09-11T12:00:00+00:00"
L3_AT = "2026-09-12T12:00:00+00:00"


@pytest.fixture()
def client():
    engine = create_engine("sqlite:///:memory:")
    metadata.create_all(engine)
    app = create_app(engine)
    app.testing = True
    return app.test_client()


def _put(client, path, payload, actor=None):
    headers = {"X-Actor-Id": actor} if actor else {}
    return client.put(path, json=payload, headers=headers)


def _post(client, path, payload, actor=None):
    headers = {"X-Actor-Id": actor} if actor else {}
    return client.post(path, json=payload, headers=headers)


def _get(client, path, actor=None):
    headers = {"X-Actor-Id": actor} if actor else {}
    return client.get(path, headers=headers)


@pytest.fixture()
def world(client):
    """两个品牌；A 品牌有老主体/新主体，B 品牌为跨品牌对照。"""
    _put(client, "/admin/facts/brand", {"brand_id": "A", "name": "安心连锁"})
    _put(client, "/admin/facts/brand", {"brand_id": "B", "name": "别家药房"})
    _put(client, "/admin/facts/operator", {"operator_id": "OP_OLD", "brand_id": "A", "name": "老经营主体"})
    _put(client, "/admin/facts/operator", {"operator_id": "OP_NEW", "brand_id": "A", "name": "新经营主体"})
    _put(client, "/admin/facts/operator", {"operator_id": "OP_B", "brand_id": "B", "name": "别家主体"})

    actors = {
        "hq_a": {"actor_id": "hq_a", "brand_id": "A", "name": "A品牌质量负责人", "role": "hq"},
        "hq_a2": {"actor_id": "hq_a2", "brand_id": "A", "name": "A品牌合规专员", "role": "hq"},
        "hq_b": {"actor_id": "hq_b", "brand_id": "B", "name": "B品牌负责人", "role": "hq"},
        "op_new_qa": {
            "actor_id": "op_new_qa", "name": "新主体质量员", "role": "operator",
            "operator_id": "OP_NEW", "brand_id": "A",
        },
        "store_s1": {
            "actor_id": "store_s1", "name": "一店店长", "role": "store",
            "store_id": "S1", "brand_id": "A",
        },
        "store_s2": {
            "actor_id": "store_s2", "name": "二店店长", "role": "store",
            "store_id": "S2", "brand_id": "A",
        },
        "store_other": {
            "actor_id": "store_other", "name": "无关门店店长", "role": "store",
            "store_id": "S9", "brand_id": "A",
        },
    }
    for actor in actors.values():
        _put(client, "/admin/facts/actor", actor)

    for sid, name in (("S1", "一店"), ("S2", "二店"), ("S3", "关店门店"),
                      ("S4", "责任断裂店"), ("S5", "转手外品牌店"), ("S9", "无关门店")):
        _put(client, "/admin/facts/store", {"store_id": sid, "brand_id": "A", "name": name})
    _put(client, "/admin/facts/online_entry", {
        "entry_id": "E1", "brand_id": "A", "host_store_id": "S1", "name": "一店线上旗舰店"
    })

    def relation(ttype, tid, operator, valid_from, valid_to=None, actor="hq_a", kind="franchise"):
        _post(client, "/relations", {
            "target_type": ttype, "target_id": tid, "operator_id": operator,
            "relation_kind": kind, "valid_from": valid_from, "valid_to": valid_to,
        }, actor=actor)

    # S1：供货时与生效时关系稳定 → included
    relation("store", "S1", "OP_OLD", T_BEFORE)
    # S2：8 月转手新主体 → transferred
    relation("store", "S2", "OP_OLD", T_BEFORE, HANDOVER)
    relation("store", "S2", "OP_NEW", HANDOVER)
    # S3：8 月中关店/退网，生效时无关系 → historical，老主体兜底
    relation("store", "S3", "OP_OLD", T_BEFORE, "2026-08-15T00:00:00+00:00")
    # S4：无任何关系 → unresolved
    # S5：转手到品牌外主体 → excluded + 跨品牌协查风险
    relation("store", "S5", "OP_OLD", T_BEFORE, HANDOVER)
    relation("store", "S5", "OP_B", HANDOVER)
    # E1：线上入口稳定挂在新主体名下
    relation("online_entry", "E1", "OP_NEW", T_BEFORE, kind="direct")

    # 同一批号药品在召回生效前供货到五个入口。
    for ttype, tid in (("store", "S1"), ("store", "S2"), ("store", "S3"),
                       ("store", "S4"), ("store", "S5"), ("online_entry", "E1")):
        _post(client, "/supplies", {
            "supply_id": f"SUP_{tid}",
            "brand_id": "A",
            "product_code": "P1",
            "product_name": "感冒灵颗粒",
            "batch_no": "LOT-X",
            "target_type": ttype,
            "target_id": tid,
            "supplied_at": SUPPLY_AT,
        })
    # S5 另有一笔生效后供货，验证逐笔排除而非整店一刀切。
    _post(client, "/supplies", {
        "supply_id": "SUP_S5_LATE",
        "brand_id": "A", "product_code": "P1", "product_name": "感冒灵颗粒",
        "batch_no": "LOT-X", "target_type": "store", "target_id": "S5",
        "supplied_at": "2026-09-11T08:00:00+00:00",
    })

    return client


def create_recall(client, actor="hq_a", batch="LOT-X", effective=EFFECTIVE, risk="high"):
    resp = _post(client, "/recalls", {
        "brand_id": "A",
        "product_code": "P1",
        "product_name": "感冒灵颗粒",
        "batch_no": batch,
        "risk_level": risk,
        "effective_at": effective,
    }, actor=actor)
    assert resp.status_code == 201, resp.get_json()
    return resp.get_json()
