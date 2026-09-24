"""召回触达领域服务。

核心原则
========
1. 双时点归属：判定一个入口的触达对象时，分别取*供货发生时*与*召回生效时*
   有效的门店—主体关系；两者都落快照，互不顶替。
2. 只增不改：关系变更闭合旧切片而非 UPDATE；批号纠错开新版本而非覆盖；
   迟到/重复回执落流水但不回写当前处置。
3. 停机接续：所有截止时间是相对生效时刻的绝对时间，通知先落库为 queued，
   服务恢复后扫描/补发，进度不重置。
4. 最小知情：视图按版本快照与事件流拼装，总部限品牌、门店只见本入口所需信息。
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from sqlalchemy import and_, asc, desc, or_, select
from sqlalchemy.engine import Connection

from .schema import (
    actors,
    brands,
    disputes as disputes_t,
    entity_licenses,
    entity_relations,
    escalations as escalations_t,
    legal_entities,
    notifications as notifications_t,
    recall_targets,
    recall_versions,
    recalls,
    receipts as receipts_t,
    reassignments as reassignments_t,
    stores,
    supplies as supplies_t,
)

# 高风险对象的分层升级时限（自召回版本基准时刻起的绝对小时数）。
HIGH_RISK_ESCALATION_LEVELS = [
    (1, "store_manager", 4),
    (2, "entity_quality_lead", 12),
    (3, "brand_hq_emergency", 24),
]
TERMINAL_ACTIONS = {"stopped_sale", "quarantined", "not_found"}
ALL_ACTIONS = TERMINAL_ACTIONS | {"attribution_dispute"}


class DomainError(Exception):
    """可向调用方表达的领域规则冲突（映射为 HTTP 4xx）。"""

    status = 400


class AuthError(DomainError):
    status = 403


class NotFoundError(DomainError):
    status = 404


class ConflictError(DomainError):
    status = 409


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def _plus_hours(iso_ts: str, hours: int) -> str:
    return (
        datetime.fromisoformat(iso_ts) + timedelta(hours=hours)
    ).isoformat(timespec="seconds")


# ---------------------------------------------------------------- 身份登记


def register_brand(conn: Connection, name: str, *, brand_id: str | None = None, at: str | None = None) -> str:
    brand_id = brand_id or new_id("brd")
    conn.execute(
        brands.insert().values(brand_id=brand_id, name=name, created_at=at or now_iso())
    )
    return brand_id


def register_entity(
    conn: Connection, brand_id: str, name: str, *, entity_id: str | None = None, at: str | None = None
) -> str:
    entity_id = entity_id or new_id("ent")
    conn.execute(
        legal_entities.insert().values(
            entity_id=entity_id, brand_id=brand_id, name=name, created_at=at or now_iso()
        )
    )
    return entity_id


def register_store(
    conn: Connection, brand_id: str, name: str, entry_type: str, *, store_id: str | None = None,
    at: str | None = None,
) -> str:
    if entry_type not in ("physical", "online"):
        raise DomainError("entry_type 必须是 physical 或 online")
    store_id = store_id or new_id("sto")
    conn.execute(
        stores.insert().values(
            store_id=store_id, brand_id=brand_id, name=name, entry_type=entry_type,
            created_at=at or now_iso(),
        )
    )
    return store_id


def register_actor(
    conn: Connection, actor_id: str, role: str, *, brand_id: str | None = None,
    entity_id: str | None = None, store_id: str | None = None, at: str | None = None,
) -> None:
    conn.execute(
        actors.insert().values(
            actor_id=actor_id, role=role, brand_id=brand_id, entity_id=entity_id,
            store_id=store_id, created_at=at or now_iso(),
        )
    )


def upsert_license(
    conn: Connection, entity_id: str, license_no: str, valid_from: str,
    valid_until: str | None, *, recorded_at: str | None = None,
) -> str:
    license_id = new_id("lic")
    conn.execute(
        entity_licenses.insert().values(
            license_id=license_id, entity_id=entity_id, license_no=license_no,
            valid_from=valid_from, valid_until=valid_until, recorded_at=recorded_at or now_iso(),
        )
    )
    return license_id


def put_relation(
    conn: Connection, brand_id: str, store_id: str, entity_id: str, relation_type: str,
    valid_from: str, *, recorded_by: str, recorded_at: str | None = None,
) -> str:
    """登记一段门店—主体关系；同一门店未闭合的旧切片在 valid_from 处闭合。

    关系变更绝不重写历史，旧切片保留其 recorded_by——异议复核回避正是据此判断。
    """
    if relation_type not in ("direct", "franchise"):
        raise DomainError("relation_type 必须是 direct 或 franchise")
    recorded_at = recorded_at or now_iso()
    conn.execute(
        entity_relations.update()
        .where(
            and_(
                entity_relations.c.store_id == store_id,
                entity_relations.c.valid_until.is_(None),
            )
        )
        .values(valid_until=valid_from)
    )
    relation_id = new_id("rel")
    conn.execute(
        entity_relations.insert().values(
            relation_id=relation_id, brand_id=brand_id, store_id=store_id,
            entity_id=entity_id, relation_type=relation_type, valid_from=valid_from,
            valid_until=None, recorded_at=recorded_at, recorded_by=recorded_by,
        )
    )
    return relation_id


def record_supply(
    conn: Connection, *, brand_id: str, store_id: str, product_code: str, product_name: str,
    batch_no: str, supplied_at: str, supplier_entity_id: str | None = None,
    quantity: int = 0, recorded_at: str | None = None,
) -> str:
    supply_id = new_id("sup")
    conn.execute(
        supplies_t.insert().values(
            supply_id=supply_id, brand_id=brand_id, store_id=store_id, product_code=product_code,
            product_name=product_name, batch_no=batch_no, supplied_at=supplied_at,
            supplier_entity_id=supplier_entity_id, quantity=quantity,
            recorded_at=recorded_at or now_iso(),
        )
    )
    return supply_id


# ---------------------------------------------------------------- as-of 查询


def _relation_as_of(conn: Connection, store_id: str, at: str) -> dict[str, Any] | None:
    stmt = (
        select(entity_relations)
        .where(
            and_(
                entity_relations.c.store_id == store_id,
                entity_relations.c.valid_from <= at,
                or_(entity_relations.c.valid_until.is_(None), entity_relations.c.valid_until > at),
            )
        )
        .order_by(desc(entity_relations.c.valid_from))
        .limit(1)
    )
    row = conn.execute(stmt).first()
    return dict(row._mapping) if row else None


def _license_as_of(conn: Connection, entity_id: str, at: str) -> str | None:
    stmt = (
        select(entity_licenses.c.license_no)
        .where(
            and_(
                entity_licenses.c.entity_id == entity_id,
                entity_licenses.c.valid_from <= at,
                or_(entity_licenses.c.valid_until.is_(None), entity_licenses.c.valid_until > at),
            )
        )
        .order_by(desc(entity_licenses.c.valid_from))
        .limit(1)
    )
    row = conn.execute(stmt).first()
    return row[0] if row else None


def _entity_name(conn: Connection, entity_id: str | None) -> str | None:
    if entity_id is None:
        return None
    row = conn.execute(
        select(legal_entities.c.name).where(legal_entities.c.entity_id == entity_id)
    ).first()
    return row[0] if row else None


def _actor(conn: Connection, actor_id: str) -> dict[str, Any]:
    row = conn.execute(select(actors).where(actors.c.actor_id == actor_id)).first()
    if not row:
        raise AuthError("未知调用方")
    return dict(row._mapping)


# ---------------------------------------------------------------- 召回建档与目标判定


def create_recall(
    conn: Connection, *, brand_id: str, product_code: str, product_name: str, batch_no: str,
    risk_level: str, effective_at: str, created_by: str,
) -> dict[str, Any]:
    if risk_level not in ("high", "medium", "low"):
        raise DomainError("risk_level 必须是 high / medium / low")
    ts = now_iso()
    recall_id = new_id("rcl")
    conn.execute(
        recalls.insert().values(
            recall_id=recall_id, brand_id=brand_id, product_code=product_code,
            product_name=product_name, risk_level=risk_level, effective_at=effective_at,
            created_by=created_by, created_at=ts, current_version_no=1,
        )
    )
    version_id = new_id("ver")
    conn.execute(
        recall_versions.insert().values(
            version_id=version_id, recall_id=recall_id, version_no=1, batch_no=batch_no,
            reason=None, created_at=ts,
        )
    )
    targets = _build_targets(
        conn, brand_id=brand_id, version_id=version_id, product_code=product_code,
        batch_no=batch_no, base_time=effective_at, ts=ts, risk_level=risk_level,
    )
    return {"recall_id": recall_id, "version_id": version_id, "version_no": 1, "targets": targets}


def correct_batch(
    conn: Connection, *, recall_id: str, new_batch_no: str, reason: str, created_by: str
) -> dict[str, Any]:
    """批号纠错：开启新版本，按新批号重建目标，并把旧版本上已完成的处置结转。"""
    recall = conn.execute(select(recalls).where(recalls.c.recall_id == recall_id)).first()
    if not recall:
        raise NotFoundError("召回不存在")
    recall = dict(recall._mapping)
    new_no = recall["current_version_no"] + 1
    ts = now_iso()
    version_id = new_id("ver")
    conn.execute(
        recall_versions.insert().values(
            version_id=version_id, recall_id=recall_id, version_no=new_no,
            batch_no=new_batch_no, reason=reason, created_at=ts,
        )
    )
    targets = _build_targets(
        conn, brand_id=recall["brand_id"], version_id=version_id,
        product_code=recall["product_code"], batch_no=new_batch_no, base_time=ts, ts=ts,
        risk_level=recall["risk_level"],
    )
    _carry_over_actions(conn, recall_id=recall_id, new_version_id=version_id, ts=ts)
    conn.execute(
        recalls.update().where(recalls.c.recall_id == recall_id)
        .values(current_version_no=new_no)
    )
    return {"recall_id": recall_id, "version_id": version_id, "version_no": new_no, "targets": targets}


def _matching_supplies(conn: Connection, *, brand_id: str, product_code: str, batch_no: str):
    rows = conn.execute(
        select(supplies_t)
        .where(
            and_(
                supplies_t.c.brand_id == brand_id,
                supplies_t.c.product_code == product_code,
                supplies_t.c.batch_no == batch_no,
            )
        )
        .order_by(asc(supplies_t.c.supplied_at))
    ).all()
    return [dict(r._mapping) for r in rows]


def _build_targets(
    conn: Connection, *, brand_id: str, version_id: str, product_code: str, batch_no: str,
    base_time: str, ts: str, risk_level: str,
) -> list[dict[str, Any]]:
    # 同一入口可能多次收货，聚合为一个触达目标：取最早一次供货确立"供货时归属"。
    per_store: dict[str, dict[str, Any]] = {}
    for supply in _matching_supplies(conn, brand_id=brand_id, product_code=product_code, batch_no=batch_no):
        agg = per_store.setdefault(
            supply["store_id"],
            {"supply_id": supply["supply_id"], "supply_at": supply["supplied_at"],
             "fallback_entity": supply["supplier_entity_id"], "quantity": 0},
        )
        agg["quantity"] += supply["quantity"] or 0
        if supply["supplied_at"] < agg["supply_at"]:
            agg["supply_at"] = supply["supplied_at"]
            agg["supply_id"] = supply["supply_id"]
            agg["fallback_entity"] = supply["supplier_entity_id"]

    targets: list[dict[str, Any]] = []
    for store_id, info in per_store.items():
        store_row = conn.execute(select(stores).where(stores.c.store_id == store_id)).first()
        store_row = dict(store_row._mapping)
        supply_rel = _relation_as_of(conn, store_id, info["supply_at"])
        current_rel = _relation_as_of(conn, store_id, base_time)
        supply_entity = supply_rel["entity_id"] if supply_rel else info["fallback_entity"]
        current_entity = current_rel["entity_id"] if current_rel else None
        supply_license = _license_as_of(conn, supply_entity, info["supply_at"]) if supply_entity else None

        disposition, touch_entity, reason_code, detail = _decide_disposition(
            supply_entity=supply_entity, current_entity=current_entity,
            supply_rel=supply_rel, current_rel=current_rel,
            name_of=lambda e: _entity_name(conn, e),
        )

        target_id = new_id("tgt")
        conn.execute(
            recall_targets.insert().values(
                target_id=target_id, version_id=version_id, store_id=store_id,
                entry_type=store_row["entry_type"], supply_id=info["supply_id"],
                supply_at=info["supply_at"], supply_time_entity_id=supply_entity,
                supply_license_no=supply_license, current_entity_id=current_entity,
                disposition=disposition, touch_entity_id=touch_entity,
                reason_code=reason_code, reason_detail=detail, created_at=ts,
            )
        )

        # 初始通知与升级时限全部此刻落库；服务停机不影响其存在与截止时刻。
        if touch_entity is not None:
            _queue_notification(
                conn, version_id=version_id, target_id=target_id,
                recipient_entity_id=touch_entity, kind="recall_notice",
                payload={"batch_no": batch_no, "risk": risk_level,
                         "reason": reason_code}, ts=ts,
                dedupe=f"initial:{version_id}:{target_id}",
            )
            if disposition == "transferred" and supply_entity and supply_entity != touch_entity:
                # 供货时主体虽不再持证经营，仍须被告知历史批次流向，配合溯源。
                _queue_notification(
                    conn, version_id=version_id, target_id=target_id,
                    recipient_entity_id=supply_entity, kind="supply_time_inform",
                    payload={"batch_no": batch_no, "reason": reason_code}, ts=ts,
                    dedupe=f"inform:{version_id}:{target_id}:{supply_entity}",
                )
        if risk_level == "high" and touch_entity is not None:
            for level, label, hours in HIGH_RISK_ESCALATION_LEVELS:
                conn.execute(
                    escalations_t.insert().values(
                        escalation_id=new_id("esc"), target_id=target_id, version_id=version_id,
                        level=level, label=label, due_at=_plus_hours(base_time, hours),
                        triggered_at=None, notified_entity_id=touch_entity,
                    )
                )

        targets.append(
            {
                "target_id": target_id,
                "store_id": store_id,
                "entry_type": store_row["entry_type"],
                "disposition": disposition,
                "touch_entity_id": touch_entity,
                "supply_time_entity_id": supply_entity,
                "current_entity_id": current_entity,
                "supply_license_no": supply_license,
                "reason_code": reason_code,
                "reason_detail": detail,
            }
        )
    return targets


def _decide_disposition(
    *, supply_entity, current_entity, supply_rel, current_rel, name_of
) -> tuple[str, str | None, str, str]:
    """返回 (纳入/转交/排除, 触达主体, 理由码, 人读说明)。"""
    supply_name = name_of(supply_entity)
    current_name = name_of(current_entity)
    if supply_entity is None:
        return (
            "excluded", None, "no_attributable_entity",
            "供货当日查无有效门店—主体关系，供货凭证亦未登记责任主体，"
            "无法确定任何经营主体承担处置；列为排除并转人工溯源，不凭空派给当前主体。",
        )
    if current_entity is None:
        return (
            "included", supply_entity, "no_current_entity",
            f"召回生效时该入口已无有效经营主体；进货当日责任属于「{supply_name}」，"
            "不得因主体注销/关系结束而漏触达，由供货时主体承担召回处置。",
        )
    if supply_entity == current_entity:
        basis = "加盟关系" if (supply_rel and supply_rel.get("relation_type") == "franchise") else "直营关系"
        return (
            "included", current_entity, "same_entity",
            f"供货时与生效时的有效关系均指向「{current_name}」（{basis}），责任主体连续，直接纳入。",
        )
    return (
        "transferred", current_entity, "entity_changed",
        f"供货发生时（{basis_label(supply_rel)}）责任主体为「{supply_name}」，"
        f"召回生效时有效关系指向「{current_name}」（{basis_label(current_rel)}）；"
        f"处置义务随当前经营主体转交「{current_name}」，原主体「{supply_name}」保留告知与配合溯源责任。",
    )


def basis_label(rel) -> str:
    if not rel:
        return "无有效关系"
    return "加盟关系" if rel.get("relation_type") == "franchise" else "直营关系"


def _queue_notification(
    conn: Connection, *, version_id: str, target_id: str, recipient_entity_id: str | None,
    kind: str, payload: dict, ts: str, dedupe: str,
) -> None:
    existing = conn.execute(
        select(notifications_t.c.notification_id).where(notifications_t.c.dedupe_key == dedupe)
    ).first()
    if existing:
        return
    conn.execute(
        notifications_t.insert().values(
            notification_id=new_id("ntf"), version_id=version_id, target_id=target_id,
            recipient_entity_id=recipient_entity_id, kind=kind, payload=json.dumps(payload),
            status="queued", created_at=ts, sent_at=None, dedupe_key=dedupe,
        )
    )


def _carry_over_actions(conn: Connection, *, recall_id: str, new_version_id: str, ts: str) -> None:
    """把上一版本每个入口最新的已应用处置结转到新版本，保留来源版本。"""
    prior = conn.execute(
        select(recall_versions.c.version_id)
        .where(
            and_(recall_versions.c.recall_id == recall_id,
                 recall_versions.c.version_id != new_version_id)
        )
        .order_by(desc(recall_versions.c.version_no)).limit(1)
    ).first()
    if not prior:
        return
    old_version_id = prior[0]
    old_targets = conn.execute(
        select(recall_targets).where(recall_targets.c.version_id == old_version_id)
    ).all()
    new_targets = {
        r.store_id: dict(r._mapping)
        for r in conn.execute(
            select(recall_targets).where(recall_targets.c.version_id == new_version_id)
        )
    }
    for old_row in old_targets:
        old = dict(old_row._mapping)
        if not old["current_action"] or old["current_action"] not in TERMINAL_ACTIONS:
            continue
        new = new_targets.get(old["store_id"])
        if not new:
            continue
        conn.execute(
            receipts_t.insert().values(
                receipt_id=new_id("rcp"), target_id=new["target_id"], version_id=new_version_id,
                device_id=f"carry:{old_version_id}", client_seq=1,
                action=old["current_action"], action_at=old["current_action_at"],
                recorded_at=ts, applied=1, ignore_reason=None,
                carried_from_version_id=old_version_id,
            )
        )
        conn.execute(
            recall_targets.update()
            .where(recall_targets.c.target_id == new["target_id"])
            .values(
                current_action=old["current_action"], current_action_at=old["current_action_at"],
                completed_at=ts,
            )
        )
        # 新版本排期为该入口生成的升级链即刻吊销：处置在新版本建立前已完成，不应再升级。
        conn.execute(
            escalations_t.update()
            .where(and_(escalations_t.c.target_id == new["target_id"],
                        escalations_t.c.revoked_at.is_(None)))
            .values(revoked_at=ts)
        )


# ---------------------------------------------------------------- 回执（含离线上传去重/乱序保护）


def submit_receipt(
    conn: Connection, *, actor_id: str, target_id: str, device_id: str, client_seq: int,
    action: str, action_at: str,
) -> dict[str, Any]:
    actor = _actor(conn, actor_id)
    target = conn.execute(
        select(recall_targets).where(recall_targets.c.target_id == target_id)
    ).first()
    if not target:
        raise NotFoundError("触达目标不存在")
    target = dict(target._mapping)
    if actor["role"] == "store" and actor["store_id"] != target["store_id"]:
        raise AuthError("门店只能回报本入口的处置")
    if actor["role"] == "entity" and actor["entity_id"] != target["touch_entity_id"]:
        raise AuthError("主体只能回报本主体被触达的目标")
    if action not in ALL_ACTIONS:
        raise DomainError("action 必须是 stopped_sale / quarantined / not_found / attribution_dispute")
    if target["disposition"] == "excluded":
        raise DomainError("该入口已排除，不接受处置回执；如有异议请走归属异议")

    ts = now_iso()
    # 设备离线上传可能重放：先按 (device_id, client_seq) 查重，命中即原样回显，不落第二条。
    existing = conn.execute(
        select(receipts_t).where(
            and_(receipts_t.c.device_id == device_id, receipts_t.c.client_seq == client_seq)
        )
    ).first()
    if existing:
        stored = dict(existing._mapping)
        return {"receipt_id": stored["receipt_id"], "applied": bool(stored["applied"]),
                "deduplicated": True, "ignore_reason": stored["ignore_reason"]}

    receipt_id = new_id("rcp")
    conn.execute(
        receipts_t.insert().values(
            receipt_id=receipt_id, target_id=target_id, version_id=target["version_id"],
            device_id=device_id, client_seq=client_seq, action=action, action_at=action_at,
            recorded_at=ts, applied=1, ignore_reason=None,
        )
    )

    # 时序保护：以回执声明的处置发生时刻 action_at 为准，迟到者不得压过较新处置。
    latest = target["current_action_at"]
    applied = True
    ignore_reason = None
    if latest is not None and action_at <= latest:
        applied = False
        ignore_reason = "late_superseded"
        conn.execute(
            receipts_t.update().where(receipts_t.c.receipt_id == receipt_id)
            .values(applied=0, ignore_reason=ignore_reason)
        )
    else:
        completed_at = ts if action in TERMINAL_ACTIONS else None
        conn.execute(
            recall_targets.update().where(recall_targets.c.target_id == target_id)
            .values(current_action=action, current_action_at=action_at, completed_at=completed_at)
        )
        if action == "attribution_dispute":
            already_pending = conn.execute(
                select(disputes_t.c.dispute_id).where(and_(
                    disputes_t.c.target_id == target_id, disputes_t.c.status == "pending"))
            ).first()
            if not already_pending:
                conn.execute(
                    disputes_t.insert().values(
                        dispute_id=new_id("dsp"), target_id=target_id,
                        version_id=target["version_id"], raised_by=actor_id,
                        reason="门店通过回执提出归属异议", status="pending",
                        reviewed_by=None, reviewed_at=None, review_note=None, created_at=ts,
                    )
                )

    return {"receipt_id": receipt_id, "applied": applied, "deduplicated": False,
            "ignore_reason": ignore_reason}


# ---------------------------------------------------------------- 异议复核与人工改派


def review_dispute(
    conn: Connection, *, dispute_id: str, reviewer_actor_id: str, verdict: str,
    note: str = "", to_entity_id: str | None = None,
) -> dict[str, Any]:
    if verdict not in ("upheld", "rejected"):
        raise DomainError("verdict 必须是 upheld 或 rejected")
    actor = _actor(conn, reviewer_actor_id)
    if actor["role"] not in ("reviewer", "hq"):
        raise AuthError("只有复核岗或总部可以复核异议")
    dispute = conn.execute(
        select(disputes_t).where(disputes_t.c.dispute_id == dispute_id)
    ).first()
    if not dispute:
        raise NotFoundError("异议不存在")
    dispute = dict(dispute._mapping)
    if dispute["status"] != "pending":
        raise ConflictError("该异议已复核")

    target = dict(conn.execute(
        select(recall_targets).where(recall_targets.c.target_id == dispute["target_id"])
    ).first()._mapping)

    # 回避：该入口关系切片的任何登记/变更发起人，都不能复核同一异议。
    changers = {
        r[0]
        for r in conn.execute(
            select(entity_relations.c.recorded_by).where(
                entity_relations.c.store_id == target["store_id"]
            )
        ).all()
    }
    if reviewer_actor_id in changers:
        raise AuthError("关系变更的发起人不能参与同一异议的复核")

    # 品牌隔离：总部复核人只能处理所属品牌。
    recall = _recall_of_version(conn, target["version_id"])
    if actor["role"] == "hq" and actor["brand_id"] != recall["brand_id"]:
        raise AuthError("总部只能处理所属品牌的异议")

    ts = now_iso()
    kind = "transfer" if to_entity_id else "exclude"
    if verdict == "upheld":
        reassignment_id = new_id("rsg")
        conn.execute(
            reassignments_t.insert().values(
                reassignment_id=reassignment_id, target_id=target["target_id"],
                version_id=target["version_id"], dispute_id=dispute_id,
                from_entity_id=target["touch_entity_id"], to_entity_id=to_entity_id,
                kind=kind, note=note, created_at=ts,
            )
        )
        if kind == "transfer":
            conn.execute(
                recall_targets.update().where(recall_targets.c.target_id == target["target_id"])
                .values(
                    disposition="transferred", touch_entity_id=to_entity_id,
                    reason_code="dispute_upheld_transfer",
                    reason_detail=f"归属异议成立，人工改派至新主体。复核意见：{note}",
                    current_action=None, current_action_at=None, completed_at=None,
                )
            )
            _reset_escalations(
                conn, target_id=target["target_id"], version_id=target["version_id"],
                new_entity_id=to_entity_id, base_time=ts,
            )
            _queue_notification(
                conn, version_id=target["version_id"], target_id=target["target_id"],
                recipient_entity_id=to_entity_id, kind="reassigned_notice",
                payload={"from": target["touch_entity_id"], "note": note}, ts=ts,
                dedupe=f"reassign:{target['target_id']}:{to_entity_id}",
            )
        else:
            conn.execute(
                recall_targets.update().where(recall_targets.c.target_id == target["target_id"])
                .values(
                    disposition="excluded", touch_entity_id=None,
                    reason_code="dispute_upheld_exclude",
                    reason_detail=f"归属异议成立，该入口摘除并转人工溯源。复核意见：{note}",
                    completed_at=ts,
                )
            )

    conn.execute(
        disputes_t.update().where(disputes_t.c.dispute_id == dispute_id)
        .values(status=verdict, reviewed_by=reviewer_actor_id, reviewed_at=ts, review_note=note)
    )
    return {"dispute_id": dispute_id, "verdict": verdict, "kind": kind if verdict == "upheld" else None}


# ---------------------------------------------------------------- 逾期升级 / 通知补发（停机接续）


def _reset_escalations(
    conn: Connection, *, target_id: str, version_id: str, new_entity_id: str, base_time: str
) -> None:
    """改派后旧升级链（对原主体排期）吊销留痕，以改派时刻为新主体重排时限。"""
    conn.execute(
        escalations_t.update()
        .where(and_(escalations_t.c.target_id == target_id,
                    escalations_t.c.revoked_at.is_(None)))
        .values(revoked_at=base_time)
    )
    for level, label, hours in HIGH_RISK_ESCALATION_LEVELS:
        conn.execute(
            escalations_t.insert().values(
                escalation_id=new_id("esc"), target_id=target_id, version_id=version_id,
                level=level, label=label, due_at=_plus_hours(base_time, hours),
                triggered_at=None, revoked_at=None, notified_entity_id=new_entity_id,
            )
        )


def run_due_escalations(conn: Connection, *, as_of: str | None = None) -> list[dict[str, Any]]:
    """触发所有已到绝对截止时刻且目标尚未完成的升级；可重复执行，不重复触发。"""
    as_of = as_of or now_iso()
    due = conn.execute(
        select(escalations_t)
        .where(
            and_(
                escalations_t.c.triggered_at.is_(None),
                escalations_t.c.revoked_at.is_(None),
                escalations_t.c.due_at <= as_of,
            )
        )
        .order_by(asc(escalations_t.c.due_at))
    ).all()
    triggered = []
    for row in due:
        esc = dict(row._mapping)
        target = conn.execute(
            select(recall_targets).where(recall_targets.c.target_id == esc["target_id"])
        ).first()
        target = dict(target._mapping)
        # 已完成（含异议成立摘除）的目标不再升级；处置在截止前完成即关闭整链。
        if target["completed_at"] is not None:
            continue
        # 归属异议待裁期间暂停逾期计时：门店已回应、责任归属未定，不能向原主体升级。
        pending = conn.execute(
            select(disputes_t.c.dispute_id).where(and_(
                disputes_t.c.target_id == esc["target_id"],
                disputes_t.c.status == "pending",
            ))
        ).first()
        if pending:
            continue
        ts = now_iso()
        conn.execute(
            escalations_t.update().where(escalations_t.c.escalation_id == esc["escalation_id"])
            .values(triggered_at=ts)
        )
        _queue_notification(
            conn, version_id=esc["version_id"], target_id=esc["target_id"],
            recipient_entity_id=esc["notified_entity_id"], kind="escalation",
            payload={"level": esc["level"], "label": esc["label"], "due_at": esc["due_at"]},
            ts=ts, dedupe=f"esc:{esc['target_id']}:{esc['level']}",
        )
        triggered.append({"escalation_id": esc["escalation_id"], "target_id": esc["target_id"],
                          "level": esc["level"], "label": esc["label"], "due_at": esc["due_at"]})
    return triggered


def dispatch_pending_notifications(
    conn: Connection, transport: Callable[[dict[str, Any]], None], *, limit: int = 100,
) -> list[dict[str, Any]]:
    """按入队顺序发送 queued 通知；发送成功才标记 sent。

    停服期间未发出的通知以 queued 留在库中，恢复后从最早一条继续——不重排、不丢失。
    """
    rows = conn.execute(
        select(notifications_t)
        .where(notifications_t.c.status == "queued")
        .order_by(asc(notifications_t.c.created_at), asc(notifications_t.c.notification_id))
        .limit(limit)
    ).all()
    sent = []
    for row in rows:
        item = dict(row._mapping)
        message = {"notification_id": item["notification_id"], "kind": item["kind"],
                   "recipient_entity_id": item["recipient_entity_id"],
                   "payload": json.loads(item["payload"])}
        transport(message)  # 抛异常则该条不标记，下次重试
        ts = now_iso()
        conn.execute(
            notifications_t.update()
            .where(notifications_t.c.notification_id == item["notification_id"])
            .values(status="sent", sent_at=ts)
        )
        sent.append(message)
    return sent


# ---------------------------------------------------------------- 版本化视图


def _recall_of_version(conn: Connection, version_id: str) -> dict[str, Any]:
    row = conn.execute(
        select(recalls)
        .select_from(recall_versions.join(recalls, recall_versions.c.recall_id == recalls.c.recall_id))
        .where(recall_versions.c.version_id == version_id)
    ).first()
    if not row:
        raise NotFoundError("版本不存在")
    return dict(row._mapping)


def _target_timeline(conn: Connection, target_id: str) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for r in conn.execute(
        select(receipts_t).where(receipts_t.c.target_id == target_id)
    ).all():
        d = dict(r._mapping)
        events.append({
            "at": d["recorded_at"], "type": "receipt", "action": d["action"],
            "action_at": d["action_at"], "applied": bool(d["applied"]),
            "ignore_reason": d["ignore_reason"], "device_id": d["device_id"],
            "carried_from_version_id": d["carried_from_version_id"],
        })
    for r in conn.execute(
        select(disputes_t).where(disputes_t.c.target_id == target_id)
    ).all():
        d = dict(r._mapping)
        events.append({"at": d["created_at"], "type": "dispute_opened",
                       "dispute_id": d["dispute_id"], "raised_by": d["raised_by"]})
        if d["reviewed_at"]:
            events.append({"at": d["reviewed_at"], "type": "dispute_reviewed",
                           "dispute_id": d["dispute_id"], "status": d["status"],
                           "reviewed_by": d["reviewed_by"], "note": d["review_note"]})
    for r in conn.execute(
        select(reassignments_t).where(reassignments_t.c.target_id == target_id)
    ).all():
        d = dict(r._mapping)
        events.append({"at": d["created_at"], "type": "reassignment", "kind": d["kind"],
                       "from_entity_id": d["from_entity_id"], "to_entity_id": d["to_entity_id"]})
    for r in conn.execute(
        select(escalations_t).where(
            and_(escalations_t.c.target_id == target_id,
                 escalations_t.c.revoked_at.is_(None))
        )
    ).all():
        d = dict(r._mapping)
        if d["triggered_at"]:
            events.append({"at": d["triggered_at"], "type": "escalation",
                           "level": d["level"], "label": d["label"], "due_at": d["due_at"]})
    events.sort(key=lambda e: (e["at"], e["type"]))
    return events


def hq_dashboard(conn: Connection, *, actor_id: str, recall_id: str, as_of: str | None = None) -> dict[str, Any]:
    """总部按召回版本查看覆盖率/风险入口/改派/时间线，且仅限所属品牌。"""
    actor = _actor(conn, actor_id)
    if actor["role"] != "hq":
        raise AuthError("仅总部账户可查看品牌看板")
    recall = conn.execute(select(recalls).where(recalls.c.recall_id == recall_id)).first()
    if not recall:
        raise NotFoundError("召回不存在")
    recall = dict(recall._mapping)
    if actor["brand_id"] != recall["brand_id"]:
        raise AuthError("总部只能查看所属品牌的召回")

    as_of = as_of or now_iso()
    versions_out = []
    for vrow in conn.execute(
        select(recall_versions).where(recall_versions.c.recall_id == recall_id)
        .order_by(asc(recall_versions.c.version_no))
    ).all():
        version = dict(vrow._mapping)
        targets = [
            dict(r._mapping) for r in conn.execute(
                select(recall_targets).where(recall_targets.c.version_id == version["version_id"])
            )
        ]
        actionable = [t for t in targets if t["disposition"] in ("included", "transferred")]
        completed = [t for t in actionable if t["completed_at"] is not None]
        risk_entries = [
            {"target_id": t["target_id"], "store_id": t["store_id"], "entry_type": t["entry_type"],
             "reason": "线上入口风险扩散面大" if t["entry_type"] == "online" else "高风险实体入口"}
            for t in actionable
            if t["completed_at"] is None and (t["entry_type"] == "online" or recall["risk_level"] == "high")
        ]
        overdue = []
        for t in actionable:
            if t["completed_at"] is not None:
                continue
            pending = conn.execute(
                select(disputes_t.c.dispute_id).where(and_(
                    disputes_t.c.target_id == t["target_id"],
                    disputes_t.c.status == "pending",
                ))
            ).first()
            if pending:
                continue
            esc = conn.execute(
                select(escalations_t)
                .where(and_(
                    escalations_t.c.target_id == t["target_id"],
                    escalations_t.c.triggered_at.is_(None),
                    escalations_t.c.revoked_at.is_(None),
                    escalations_t.c.due_at <= as_of,
                ))
                .order_by(asc(escalations_t.c.level)).limit(1)
            ).first()
            if esc:
                overdue.append({"target_id": t["target_id"], "next_level": esc.level,
                                "label": esc.label, "due_at": esc.due_at})
        reassign_rows = [
            dict(r._mapping) for r in conn.execute(
                select(reassignments_t)
                .select_from(reassignments_t.join(
                    recall_targets, reassignments_t.c.target_id == recall_targets.c.target_id))
                .where(recall_targets.c.version_id == version["version_id"])
            )
        ]
        targets_out = []
        for t in targets:
            targets_out.append({
                "target_id": t["target_id"], "store_id": t["store_id"],
                "entry_type": t["entry_type"], "disposition": t["disposition"],
                "supply_time_entity_id": t["supply_time_entity_id"],
                "current_entity_id": t["current_entity_id"],
                "touch_entity_id": t["touch_entity_id"],
                "supply_license_no": t["supply_license_no"],
                "reason_code": t["reason_code"], "reason_detail": t["reason_detail"],
                "current_action": t["current_action"], "completed_at": t["completed_at"],
                "timeline": _target_timeline(conn, t["target_id"]),
            })
        versions_out.append({
            "version_no": version["version_no"], "version_id": version["version_id"],
            "batch_no": version["batch_no"], "reason": version["reason"],
            "coverage": {
                "actionable": len(actionable), "completed": len(completed),
                "rate": round(len(completed) / len(actionable), 4) if actionable else None,
            },
            "risk_entries": risk_entries,
            "overdue_escalations": overdue,
            "reassignments": [
                {"target_id": r["target_id"], "kind": r["kind"],
                 "from_entity_id": r["from_entity_id"], "to_entity_id": r["to_entity_id"],
                 "note": r["note"], "created_at": r["created_at"]}
                for r in reassign_rows
            ],
            "targets": targets_out,
        })

    return {
        "recall_id": recall_id, "product_code": recall["product_code"],
        "product_name": recall["product_name"], "risk_level": recall["risk_level"],
        "effective_at": recall["effective_at"], "as_of": as_of, "versions": versions_out,
    }


def store_briefing(conn: Connection, *, actor_id: str, target_id: str) -> dict[str, Any]:
    """门店/设备视角：只获得完成处置所需的信息，不含其他主体与品牌全局数据。"""
    actor = _actor(conn, actor_id)
    target = conn.execute(
        select(recall_targets).where(recall_targets.c.target_id == target_id)
    ).first()
    if not target:
        raise NotFoundError("触达目标不存在")
    target = dict(target._mapping)
    if actor["role"] != "store" or actor["store_id"] != target["store_id"]:
        raise AuthError("门店只能查看本入口的处置任务")
    if target["disposition"] == "excluded":
        return {"target_id": target_id, "status": "excluded",
                "message": "该入口经判定不属于本次召回处置范围，如有疑问可联系总部。"}
    recall = _recall_of_version(conn, target["version_id"])
    version = conn.execute(
        select(recall_versions).where(recall_versions.c.version_id == target["version_id"])
    ).first()
    version = dict(version._mapping)
    open_dispute = conn.execute(
        select(disputes_t.c.dispute_id).where(and_(
            disputes_t.c.target_id == target_id, disputes_t.c.status == "pending"))
    ).first()
    return {
        "target_id": target_id,
        "product_name": recall["product_name"],
        "batch_no": version["batch_no"],
        "risk_level": recall["risk_level"],
        "recall_effective_at": recall["effective_at"],
        "required_actions": sorted(TERMINAL_ACTIONS) + ["attribution_dispute"],
        "current_action": target["current_action"],
        "current_action_at": target["current_action_at"],
        "completed_at": target["completed_at"],
        "dispute_id": open_dispute[0] if open_dispute else None,
        # 有意不返回任何其他主体 id/名称、许可证号、品牌覆盖率等信息。
    }
