"""召回触达主场景：双时点归属、去重乱序、异议回避改派、升级、停机接续、批号纠错。"""

import pytest
from sqlalchemy import and_, select

from pharmacy_identity import services as svc
from pharmacy_identity.schema import (
    disputes as dsp_t, entity_relations, escalations as esc_t,
    notifications as ntf_t, recall_targets as tgt_t, receipts as rcp_t,
)

# 固定时间轴（UTC）
T_SUPPLY = "2026-01-10T08:00:00+00:00"
T_EFFECTIVE = "2026-09-20T00:00:00+00:00"
T_RECALL_CREATED = "2026-09-20T00:05:00+00:00"


def _build_world(conn):
    """品牌 B：EA 为供货时加盟主体（后退出 S1），EB 为 S1 现主体，EC 用于改派。"""
    bid = svc.register_brand(conn, "安心药房", brand_id="B", at="2025-01-01T00:00:00+00:00")
    ea = svc.register_entity(conn, bid, "甲加盟医药", entity_id="EA")
    eb = svc.register_entity(conn, bid, "乙直营医药", entity_id="EB")
    ec = svc.register_entity(conn, bid, "丙承接医药", entity_id="EC")
    other = svc.register_brand(conn, "别家品牌", brand_id="B2")
    svc.register_entity(conn, other, "别家主体", entity_id="EX")

    s1 = svc.register_store(conn, bid, "中山北路店", "physical", store_id="S1")
    s2 = svc.register_store(conn, bid, "安心到家旗舰店", "online", store_id="S2")
    s3 = svc.register_store(conn, bid, "老码头店", "physical", store_id="S3")
    s4 = svc.register_store(conn, bid, "无主自助柜", "physical", store_id="S4")

    svc.upsert_license(conn, ea, "LIC-A-OLD", "2024-01-01T00:00:00+00:00",
                       "2026-06-01T00:00:00+00:00")
    svc.upsert_license(conn, eb, "LIC-B-NEW", "2026-06-01T00:00:00+00:00", None)
    svc.upsert_license(conn, ec, "LIC-C", "2025-01-01T00:00:00+00:00", None)

    # S1：供货时加盟于 EA，2026-06-01 起改为 EB 直营（关系变更只闭合旧切片）。
    svc.put_relation(conn, bid, s1, ea, "franchise", "2025-01-01T00:00:00+00:00",
                     recorded_by="mgrA", recorded_at="2025-01-01T00:00:00+00:00")
    svc.put_relation(conn, bid, s1, eb, "direct", "2026-06-01T00:00:00+00:00",
                     recorded_by="mgrB", recorded_at="2026-05-20T00:00:00+00:00")
    # S2：主体始终是 EA。
    svc.put_relation(conn, bid, s2, ea, "direct", "2025-01-01T00:00:00+00:00",
                     recorded_by="mgrA")
    # S3：关系在召回前已结束且无新主体（在夹具中闭合）。
    svc.put_relation(conn, bid, s3, ea, "franchise", "2025-01-01T00:00:00+00:00",
                     recorded_by="mgrA")
    # S4：从未建立关系。

    for store_id in (s1, s2, s3, s4):
        svc.record_supply(
            conn, brand_id=bid, store_id=store_id, product_code="P01",
            product_name="头孢克肟胶囊", batch_no="X202601", supplied_at=T_SUPPLY,
            supplier_entity_id=None, quantity=10,
        )

    # 账户
    svc.register_actor(conn, "hqB", "hq", brand_id=bid)
    svc.register_actor(conn, "hqOther", "hq", brand_id=other)
    svc.register_actor(conn, "rev1", "reviewer")
    svc.register_actor(conn, "mgrA", "reviewer")  # 同时是 S1/S2/S3 关系发起人
    svc.register_actor(conn, "devS1", "store", store_id=s1)
    svc.register_actor(conn, "devS2", "store", store_id=s2)
    return {"brand": bid, "stores": {"s1": s1, "s2": s2, "s3": s3, "s4": s4}}


def _close_s3_relation(conn):
    """S3 的加盟关系在召回生效前结束，之后没有新主体承接。"""
    from sqlalchemy import update
    conn.execute(
        update(entity_relations)
        .where(entity_relations.c.store_id == "S3")
        .values(valid_until="2026-05-01T00:00:00+00:00")
    )


@pytest.fixture()
def world(engine):
    with engine.begin() as conn:
        w = _build_world(conn)
        _close_s3_relation(conn)
        result = svc.create_recall(
            conn, brand_id=w["brand"], product_code="P01",
            product_name="头孢克肟胶囊", batch_no="X202601", risk_level="high",
            effective_at=T_EFFECTIVE, created_by="hqB",
        )
    w["recall"] = result
    w["targets"] = {t["store_id"]: t for t in result["targets"]}
    return w


def test_dual_timepoint_targeting(world):
    t = world["targets"]
    # S1：主体变更 → 转交当前主体 EB，供货时主体 EA 留痕，许可证取供货当日的旧证。
    s1 = t["S1"]
    assert s1["disposition"] == "transferred"
    assert s1["touch_entity_id"] == "EB"
    assert s1["supply_time_entity_id"] == "EA"
    assert s1["current_entity_id"] == "EB"
    assert s1["supply_license_no"] == "LIC-A-OLD"
    assert s1["reason_code"] == "entity_changed"

    # S2：两个时点主体一致 → 纳入。
    assert t["S2"]["disposition"] == "included"
    assert t["S2"]["touch_entity_id"] == "EA"
    assert t["S2"]["reason_code"] == "same_entity"

    # S3：当前无主体 → 仍纳入给供货时主体，避免漏掉实体门店。
    assert t["S3"]["disposition"] == "included"
    assert t["S3"]["touch_entity_id"] == "EA"
    assert t["S3"]["reason_code"] == "no_current_entity"

    # S4：供货当日也查无责任主体 → 排除并说明，不许凭空派给今天的任何主体。
    assert t["S4"]["disposition"] == "excluded"
    assert t["S4"]["touch_entity_id"] is None
    assert t["S4"]["reason_code"] == "no_attributable_entity"
    # 线上入口同样在目标集合中，不会因只盯实体店而漏掉。
    assert t["S2"]["entry_type"] == "online"


def test_receipt_dedup_and_ordering(engine, world):
    s1 = world["targets"]["S1"]["target_id"]
    with engine.begin() as conn:
        r1 = svc.submit_receipt(
            conn, actor_id="devS1", target_id=s1, device_id="dev-1", client_seq=1,
            action="quarantined", action_at="2026-09-20T02:00:00+00:00")
        # 迟到回执（更早的处置时刻）不得压过较新处置。
        r2 = svc.submit_receipt(
            conn, actor_id="devS1", target_id=s1, device_id="dev-1", client_seq=2,
            action="stopped_sale", action_at="2026-09-20T01:00:00+00:00")
        # 设备离线上传重放 seq=1 → 幂等去重。
        r1_replay = svc.submit_receipt(
            conn, actor_id="devS1", target_id=s1, device_id="dev-1", client_seq=1,
            action="quarantined", action_at="2026-09-20T02:00:00+00:00")
        cur = conn.execute(
            select(tgt_t).where(tgt_t.c.target_id == s1)
        ).first()
    assert r1["applied"] is True
    assert r2["applied"] is False and r2["ignore_reason"] == "late_superseded"
    assert r1_replay["deduplicated"] is True
    # 当前处置保持隔离，不被迟到的"停售"覆盖。
    assert cur.current_action == "quarantined"
    assert cur.completed_at is not None


def test_store_cannot_report_other_store(engine, world):
    s2 = world["targets"]["S2"]["target_id"]
    with engine.begin() as conn, pytest.raises(svc.AuthError):
        svc.submit_receipt(
            conn, actor_id="devS1", target_id=s2, device_id="x", client_seq=1,
            action="quarantined", action_at=T_EFFECTIVE)


def test_excluded_target_rejects_receipt(engine, world):
    s4 = world["targets"]["S4"]["target_id"]
    with engine.begin() as conn, pytest.raises(svc.DomainError):
        svc.submit_receipt(
            conn, actor_id="devS1", target_id=s4, device_id="x", client_seq=1,
            action="quarantined", action_at=T_EFFECTIVE)


def test_escalation_levels_and_outage_resume(engine, world):
    s2 = world["targets"]["S2"]["target_id"]
    # 初始通知已落库但未发出（模拟停服）。
    with engine.begin() as conn:
        queued = conn.execute(
            select(ntf_t).where(ntf_t.c.status == "queued")
        ).all()
    assert len(queued) >= 4  # 三个未完成高风险目标各一条初始通知 + S1 告知 EA 一条

    # 传输器故障：发送抛错时该批不标记 sent，进度保留。
    def boom(_):
        raise RuntimeError("短信网关不可用")

    with engine.begin() as conn, pytest.raises(RuntimeError):
        svc.dispatch_pending_notifications(conn, boom)
    with engine.begin() as conn:
        still_queued = conn.execute(
            select(ntf_t).where(ntf_t.c.status == "queued")
        ).all()
    assert len(still_queued) == len(queued)

    # t+5h：第一层升级（店长 4h）到期；S2 无回执 → 触发。
    with engine.begin() as conn:
        fired = svc.run_due_escalations(conn, as_of="2026-09-20T05:00:00+00:00")
    levels = {(f["target_id"], f["level"]) for f in fired}
    assert (s2, 1) in levels
    assert (s2, 2) not in levels
    # 重复执行不重复触发。
    with engine.begin() as conn:
        fired_again = svc.run_due_escalations(conn, as_of="2026-09-20T05:30:00+00:00")
    assert fired_again == []

    # t+13h：第二、三层在 +12h、+24h，故只有 level2。
    with engine.begin() as conn:
        fired2 = svc.run_due_escalations(conn, as_of="2026-09-20T13:00:00+00:00")
    assert (s2, 2) in {(f["target_id"], f["level"]) for f in fired2}
    assert (s2, 3) not in {(f["target_id"], f["level"]) for f in fired2}

    # 网关恢复：queued 通知按入队顺序全部补发，升级通知也在其中。
    sent = []
    with engine.begin() as conn:
        dispatched = svc.dispatch_pending_notifications(conn, sent.append, limit=1000)
    assert len(dispatched) == len(sent)
    kinds = [m["kind"] for m in sent]
    assert "escalation" in kinds and "recall_notice" in kinds
    with engine.begin() as conn:
        left = conn.execute(
            select(ntf_t).where(ntf_t.c.status == "queued")
        ).all()
    assert left == []


def test_dispute_pauses_escalation(engine, world):
    s2 = world["targets"]["S2"]["target_id"]
    with engine.begin() as conn:
        svc.submit_receipt(
            conn, actor_id="devS2", target_id=s2, device_id="dev-2", client_seq=1,
            action="attribution_dispute", action_at="2026-09-20T03:00:00+00:00")
        fired = svc.run_due_escalations(conn, as_of="2026-09-21T00:00:00+00:00")
    # 即使过了 +24h，异议待裁期间不得升级。
    assert all(f["target_id"] != s2 for f in fired)


def test_reviewer_conflict_and_reassignment(engine, world):
    s1 = world["targets"]["S1"]["target_id"]
    with engine.begin() as conn:
        svc.submit_receipt(
            conn, actor_id="devS1", target_id=s1, device_id="dev-1", client_seq=9,
            action="attribution_dispute", action_at="2026-09-20T03:00:00+00:00")
        dispute_id = conn.execute(
            select(dsp_t.c.dispute_id).where(dsp_t.c.target_id == s1)
        ).scalar()

        # mgrA 登记过 S1 的关系切片（旧加盟关系），不能复核该入口的异议。
        with pytest.raises(svc.AuthError):
            svc.review_dispute(conn, dispute_id=dispute_id, reviewer_actor_id="mgrA",
                               verdict="upheld", note="我改的我回避", to_entity_id="EC")
        # 无利害关系的复核人可裁；成立并转交给 EC。
        out = svc.review_dispute(conn, dispute_id=dispute_id, reviewer_actor_id="rev1",
                                 verdict="upheld", note="查进货凭证确属承接范围",
                                 to_entity_id="EC")
        row = conn.execute(
            select(tgt_t.c.disposition, tgt_t.c.touch_entity_id, tgt_t.c.current_action)
            .where(tgt_t.c.target_id == s1)
        ).first()
        open_esc = conn.execute(
            select(esc_t.c.level, esc_t.c.notified_entity_id)
            .where(and_(esc_t.c.target_id == s1, esc_t.c.revoked_at.is_(None)))
            .order_by(esc_t.c.level)
        ).all()
        revoked = conn.execute(
            select(esc_t.c.escalation_id)
            .where(and_(esc_t.c.target_id == s1, esc_t.c.revoked_at.is_not(None)))
        ).all()
        queued_reassign = conn.execute(
            select(ntf_t.c.notification_id).where(and_(
                ntf_t.c.kind == "reassigned_notice", ntf_t.c.target_id == s1))
        ).all()

    assert out["verdict"] == "upheld" and out["kind"] == "transfer"
    assert row.disposition == "transferred" and row.touch_entity_id == "EC"
    assert row.current_action is None  # 旧处置随改派清零，等新主体处置
    assert [e.level for e in open_esc] == [1, 2, 3]
    assert all(e.notified_entity_id == "EC" for e in open_esc)
    assert len(revoked) == 3  # 旧链吊销留痕
    assert len(queued_reassign) == 1


def test_hq_brand_isolation_and_store_minimum_disclosure(engine, world):
    recall_id = world["recall"]["recall_id"]
    s1 = world["targets"]["S1"]["target_id"]

    with engine.begin() as conn, pytest.raises(svc.AuthError):
        svc.hq_dashboard(conn, actor_id="hqOther", recall_id=recall_id)
    with engine.begin() as conn:
        dash = svc.hq_dashboard(conn, actor_id="hqB", recall_id=recall_id,
                                as_of="2026-09-20T05:00:00+00:00")
    v1 = dash["versions"][0]
    # 3 个可处置入口（S1 转交、S2/S3 纳入），S4 排除不计入覆盖率分母。
    assert v1["coverage"] == {"actionable": 3, "completed": 0, "rate": 0.0}
    # 高风险 + 线上未完成入口出现在风险入口列表。
    assert any(e["entry_type"] == "online" for e in v1["risk_entries"])

    with engine.begin() as conn, pytest.raises(svc.AuthError):
        svc.store_briefing(conn, actor_id="devS2", target_id=s1)
    with engine.begin() as conn:
        brief = svc.store_briefing(conn, actor_id="devS1", target_id=s1)
    # 最小知情：只有处置所需字段，不含其他主体/许可证/覆盖率。
    assert set(brief) == {
        "target_id", "product_name", "batch_no", "risk_level", "recall_effective_at",
        "required_actions", "current_action", "current_action_at", "completed_at",
        "dispute_id",
    }
    assert brief["batch_no"] == "X202601"


def test_batch_correction_opins_version_and_carries_actions(engine, world):
    recall_id = world["recall"]["recall_id"]
    s1_old = world["targets"]["S1"]["target_id"]
    with engine.begin() as conn:
        svc.submit_receipt(
            conn, actor_id="devS1", target_id=s1_old, device_id="dev-1", client_seq=1,
            action="quarantined", action_at="2026-09-20T02:00:00+00:00")
        # 批号纠错：新批号只发给 S1/S2（S3/S4 没有 Y 批供货）。
        for store_id in ("S1", "S2"):
            svc.record_supply(
                conn, brand_id="B", store_id=store_id, product_code="P01",
                product_name="头孢克肟胶囊", batch_no="Y202602",
                supplied_at="2026-02-15T08:00:00+00:00", quantity=5)
        out = svc.correct_batch(
            conn, recall_id=recall_id, new_batch_no="Y202602",
            reason="厂家通报批号印刷错误", created_by="hqB")
        new_targets = {t["store_id"]: t for t in out["targets"]}
        s1_new = new_targets["S1"]["target_id"]
        carried = conn.execute(
            select(rcp_t.c.action, rcp_t.c.carried_from_version_id)
            .where(rcp_t.c.target_id == s1_new)
        ).all()
        s1_state = conn.execute(
            select(tgt_t.c.current_action, tgt_t.c.completed_at)
            .where(tgt_t.c.target_id == s1_new)
        ).first()
        # 旧版本仍可查且保留此前动作。
        old_timeline = svc.hq_dashboard(
            conn, actor_id="hqB", recall_id=recall_id)["versions"]

    assert out["version_no"] == 2
    assert set(new_targets) == {"S1", "S2"}
    assert any(r.carried_from_version_id == world["recall"]["version_id"]
               and r.action == "quarantined" for r in carried)
    # 新版本上 S1 直接是已隔离状态，门店无需重复处置。
    assert s1_state.current_action == "quarantined"
    assert s1_state.completed_at is not None
    assert [v["version_no"] for v in old_timeline] == [1, 2]
    v1, v2 = old_timeline
    assert v1["batch_no"] == "X202601" and v2["batch_no"] == "Y202602"
    # 每个版本的覆盖率按当时关系事实独立呈现。
    assert v1["coverage"]["actionable"] == 3
    assert v2["coverage"]["completed"] == 1 and v2["coverage"]["actionable"] == 2
