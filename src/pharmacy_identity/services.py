"""召回服务层：身份事实、双时点定责、回执、升级、版本纠错与按版本视图。

所有时间戳为 UTC ISO 字符串；逾期判定锚定绝对时刻，服务停启不影响计时。
"""

import contextvars
import json
import uuid
from datetime import datetime

from sqlalchemy import select

from . import domain as d
from .schema import (
    actors,
    brands,
    online_entries,
    operators,
    outlet_relations,
    receipts,
    recall_target_supplies,
    recall_targets,
    recall_versions,
    recalls,
    reassignments,
    disputes as disputes_table,
    stores,
    supply_events,
    target_timeline,
    notifications as notifications_table,
    escalations as escalations_table,
)

# 系统时钟可被请求头 X-Current-Time 覆盖（测试与灰度演练用）；
# 业务时间一律走 _now_iso()，逾期因此锚定单一可重放时钟。
_current_time: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "current_time", default=None
)


def set_current_time(value: str | None):
    if value is not None:
        value = d.iso(d.parse_iso(value))
    return _current_time.set(value)


def reset_current_time(token) -> None:
    _current_time.reset(token)


class ServiceError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status = status
        self.code = code


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


def _now_iso() -> str:
    return _current_time.get() or d.iso(d.utc_now())


def _timeline(conn, version_id, ttype, tid, kind, detail, actor_id=None, at=None):
    # 单调序号：时间线按“发生先后（写入顺序）”而非设备自报时刻排序，
    # 迟到回执因此不会排到定责事件之前。
    next_seq = conn.execute(
        select(target_timeline.c.seq).order_by(target_timeline.c.seq.desc()).limit(1)
    ).first()
    seq = (next_seq[0] + 1) if next_seq else 1
    conn.execute(
        target_timeline.insert().values(
            event_id=_new_id("evt"),
            seq=seq,
            version_id=version_id,
            target_type=ttype,
            target_id=tid,
            kind=kind,
            actor_id=actor_id,
            created_at=at or _now_iso(),
            detail=detail,
        )
    )


def _notify(conn, version_id, ttype, tid, level, operator_id, payload, due_at=None,
            notice_kind="action"):
    """外发箱写入；(版本,入口,层级,类型,收件人) 唯一约束即幂等键，冲突时不重复触达。"""
    insert = notifications_table.insert().values(
        notification_id=_new_id("ntf"),
        version_id=version_id,
        target_type=ttype,
        target_id=tid,
        level=level,
        notice_kind=notice_kind,
        recipient_operator_id=operator_id,
        payload=json.dumps(payload, ensure_ascii=False),
        status="pending",
        created_at=_now_iso(),
        due_at=due_at or _now_iso(),
    )
    conn.execute(insert.prefix_with("OR IGNORE"))


# ---------------------------------------------------------------- 身份事实


def require_actor(conn, actor_id: str | None):
    if not actor_id:
        raise ServiceError(401, "UNAUTHENTICATED", "缺少 X-Actor-Id。")
    row = conn.execute(select(actors).where(actors.c.actor_id == actor_id)).mappings().first()
    if row is None:
        raise ServiceError(401, "UNKNOWN_ACTOR", "操作者不存在。")
    return dict(row)


def actor_brand_id(conn, actor: dict) -> str:
    if actor["brand_id"]:
        return actor["brand_id"]
    if actor["operator_id"]:
        row = conn.execute(
            select(operators.c.brand_id).where(operators.c.operator_id == actor["operator_id"])
        ).first()
        if row:
            return row[0]
    for column, table, key in (
        ("store_id", stores, "store_id"),
        ("entry_id", online_entries, "entry_id"),
    ):
        if actor[column]:
            row = conn.execute(
                select(table.c.brand_id).where(table.c[key] == actor[column])
            ).first()
            if row:
                return row[0]
    raise ServiceError(403, "NO_BRAND_SCOPE", "操作者没有品牌可见域。")


def require_hq(conn, actor: dict, brand_id: str) -> str:
    if actor["role"] != "hq":
        raise ServiceError(403, "HQ_ONLY", "仅品牌总部可执行该操作。")
    scope = actor_brand_id(conn, actor)
    if scope != brand_id:
        raise ServiceError(403, "BRAND_SCOPE_VIOLATION", "总部只能查看与操作所属品牌。")
    return scope


def put_identity_fact(conn, kind: str, payload: dict) -> dict:
    if kind == "brand":
        conn.execute(
            brands.delete().where(brands.c.brand_id == payload["brand_id"])
        )
        conn.execute(brands.insert().values(brand_id=payload["brand_id"], name=payload["name"]))
    elif kind == "operator":
        conn.execute(operators.delete().where(operators.c.operator_id == payload["operator_id"]))
        conn.execute(
            operators.insert().values(
                operator_id=payload["operator_id"],
                brand_id=payload["brand_id"],
                name=payload["name"],
            )
        )
    elif kind == "actor":
        conn.execute(actors.delete().where(actors.c.actor_id == payload["actor_id"]))
        conn.execute(actors.insert().values(**payload))
    elif kind == "store":
        conn.execute(stores.delete().where(stores.c.store_id == payload["store_id"]))
        conn.execute(stores.insert().values(**payload))
    elif kind == "online_entry":
        conn.execute(online_entries.delete().where(online_entries.c.entry_id == payload["entry_id"]))
        conn.execute(online_entries.insert().values(**payload))
    else:
        raise ServiceError(400, "UNKNOWN_FACT", f"未知身份事实类型：{kind}")
    return {"ok": True}


def add_relation(conn, actor: dict, payload: dict) -> dict:
    """登记一条带时间段的经营关系；新窗口生效时，旧窗口自动收口（半开区间）。"""
    if actor["role"] != "hq":
        raise ServiceError(403, "HQ_ONLY", "经营/许可关系变更仅品牌总部可登记。")
    ttype, tid = payload["target_type"], payload["target_id"]
    if ttype == d.TARGET_STORE:
        row = conn.execute(select(stores.c.brand_id).where(stores.c.store_id == tid)).first()
    else:
        row = conn.execute(
            select(online_entries.c.brand_id).where(online_entries.c.entry_id == tid)
        ).first()
    if row is None:
        raise ServiceError(404, "OUTLET_NOT_FOUND", "入口不存在。")
    if actor_brand_id(conn, actor) != row[0]:
        raise ServiceError(403, "BRAND_SCOPE_VIOLATION", "不能变更其他品牌的关系。")

    valid_from = payload["valid_from"]
    # 同一入口重叠的开放窗口在新窗口起点收口，保证任一时点可回放出唯一责任链。
    open_windows = conn.execute(
        select(outlet_relations).where(
            outlet_relations.c.target_type == ttype,
            outlet_relations.c.target_id == tid,
            outlet_relations.c.valid_to.is_(None),
        )
    ).mappings().all()
    for window in open_windows:
        if window["valid_from"] < valid_from:
            conn.execute(
                outlet_relations.update()
                .where(outlet_relations.c.relation_id == window["relation_id"])
                .values(valid_to=valid_from)
            )

    relation_id = _new_id("rel")
    conn.execute(
        outlet_relations.insert().values(
            relation_id=relation_id,
            target_type=ttype,
            target_id=tid,
            operator_id=payload["operator_id"],
            relation_kind=payload["relation_kind"],
            valid_from=valid_from,
            valid_to=payload.get("valid_to"),
            changed_by_actor_id=actor["actor_id"],
            created_at=_now_iso(),
        )
    )
    return {"relation_id": relation_id}


def add_supply(conn, payload: dict) -> dict:
    supply_id = payload.get("supply_id") or _new_id("sup")
    conn.execute(
        supply_events.insert().values(
            supply_id=supply_id,
            brand_id=payload["brand_id"],
            product_code=payload["product_code"],
            product_name=payload["product_name"],
            batch_no=payload["batch_no"],
            target_type=payload["target_type"],
            target_id=payload["target_id"],
            supplied_at=payload["supplied_at"],
            created_at=_now_iso(),
        )
    )
    return {"supply_id": supply_id}


# ---------------------------------------------------------------- 召回定责


def _load_relations(conn, ttype: str, tid: str) -> list[d.Relation]:
    rows = conn.execute(
        select(outlet_relations).where(
            outlet_relations.c.target_type == ttype,
            outlet_relations.c.target_id == tid,
        )
    ).mappings().all()
    return [
        d.Relation(
            relation_id=r["relation_id"],
            target_type=r["target_type"],
            target_id=r["target_id"],
            operator_id=r["operator_id"],
            relation_kind=r["relation_kind"],
            valid_from=r["valid_from"],
            valid_to=r["valid_to"],
            changed_by_actor_id=r["changed_by_actor_id"],
        )
        for r in rows
    ]


def _resolve_version(conn, version: dict, brand_id: str):
    """按 产品+批号 找供货，按供货时/生效时双时点冻结每个入口的定责结论。"""
    supply_rows = conn.execute(
        select(supply_events).where(
            supply_events.c.brand_id == brand_id,
            supply_events.c.product_code == version["product_code"],
            supply_events.c.batch_no == version["batch_no"],
        )
    ).mappings().all()

    grouped: dict[tuple[str, str], list[dict]] = {}
    for row in supply_rows:
        grouped.setdefault((row["target_type"], row["target_id"]), []).append(dict(row))

    operator_rows = conn.execute(select(operators)).mappings().all()
    operator_brand = {r["operator_id"]: r["brand_id"] for r in operator_rows}

    effective_at = version["effective_at"]
    for (ttype, tid), outlet_supplies in grouped.items():
        relations = _load_relations(conn, ttype, tid)
        facts = []
        per_supply = []
        for sup in outlet_supplies:
            active = d.relations_as_of(relations, ttype, tid, sup["supplied_at"])
            rel = active[0] if active else None
            per_supply.append((sup["supply_id"], rel))
            # 召回生效之后发生的供货不属于本批次范围；逐笔判定而非整店一刀切。
            if sup["supplied_at"] <= effective_at:
                facts.append(
                    d.SupplyFact(
                        supply_id=sup["supply_id"],
                        target_type=ttype,
                        target_id=tid,
                        supplied_at=sup["supplied_at"],
                        supply_at_relation=rel,
                    )
                )

        if not facts:
            attribution = d.TargetAttribution(
                ttype, tid, d.STATUS_EXCLUDED, d.REASON_AFTER_EFFECTIVE,
                "该入口对本批号的全部供货均晚于召回生效时刻，不属于本次召回范围，排除并留痕。",
                None, None, None, None,
                supply_ids=sorted(s["supply_id"] for s in outlet_supplies),
            )
        else:
            attribution = d.classify_target(
                ttype, tid, facts, relations, effective_at, operator_brand, brand_id
            )
        target_uid = _new_id("tgt")
        actionable = attribution.status in (
            d.STATUS_INCLUDED,
            d.STATUS_TRANSFERRED,
            d.STATUS_HISTORICAL,
        )
        due_at = d.deadline_for(version["risk_level"], effective_at) if actionable else None
        conn.execute(
            recall_targets.insert().values(
                target_uid=target_uid,
                version_id=version["version_id"],
                target_type=ttype,
                target_id=tid,
                status=attribution.status,
                reason_code=attribution.reason_code,
                reason_detail=attribution.reason_detail,
                responsible_operator_id=attribution.responsible_operator_id,
                historical_operator_id=attribution.historical_operator_id,
                relation_kind=attribution.relation_kind,
                basis_relation_id=attribution.basis_relation_id,
                due_at=due_at,
                created_at=_now_iso(),
            )
        )
        for supply_id, rel in per_supply:
            conn.execute(
                recall_target_supplies.insert().values(
                    target_uid=target_uid,
                    supply_id=supply_id,
                    supply_at_operator_id=rel.operator_id if rel else None,
                    supply_at_relation_kind=rel.relation_kind if rel else None,
                    supply_at_relation_id=rel.relation_id if rel else None,
                )
            )
        _timeline(
            conn, version["version_id"], ttype, tid, "attribution_resolved",
            f"定责结论 {attribution.status}：{attribution.reason_detail}",
        )
        if actionable:
            _notify(
                conn, version["version_id"], ttype, tid, 0,
                attribution.responsible_operator_id,
                {
                    "kind": "action_required",
                    "product": version["product_name"],
                    "batch_no": version["batch_no"],
                    "risk_level": version["risk_level"],
                    "reason": attribution.reason_detail,
                    "deadline": due_at,
                },
                due_at=effective_at,
            )
        if attribution.status == d.STATUS_TRANSFERRED and attribution.historical_operator_id:
            _notify(
                conn, version["version_id"], ttype, tid, 0,
                attribution.historical_operator_id,
                {
                    "kind": "historical_fyi",
                    "product": version["product_name"],
                    "batch_no": version["batch_no"],
                    "reason": attribution.reason_detail,
                },
                notice_kind="historical_fyi",
                due_at=effective_at,
            )


def create_recall(conn, actor: dict, payload: dict) -> dict:
    brand_id = payload["brand_id"]
    require_hq(conn, actor, brand_id)
    risk = payload["risk_level"]
    if risk not in d.RISK_POLICY:
        raise ServiceError(400, "BAD_RISK", "风险等级须为 low/medium/high。")
    effective_at = payload["effective_at"]
    datetime.fromisoformat(effective_at)

    recall_id = _new_id("rec")
    version_id = _new_id("ver")
    now = _now_iso()
    conn.execute(
        recalls.insert().values(
            recall_id=recall_id,
            brand_id=brand_id,
            created_by_actor_id=actor["actor_id"],
            created_at=now,
            current_version_no=1,
        )
    )
    version = {
        "version_id": version_id,
        "recall_id": recall_id,
        "version_no": 1,
        "product_code": payload["product_code"],
        "product_name": payload["product_name"],
        "batch_no": payload["batch_no"],
        "risk_level": risk,
        "effective_at": effective_at,
        "correction_reason": None,
        "created_by_actor_id": actor["actor_id"],
        "created_at": now,
        "frozen_at": now,
    }
    conn.execute(recall_versions.insert().values(**version))
    _resolve_version(conn, version, brand_id)
    return {"recall_id": recall_id, "version_id": version_id, "version_no": 1}


def correct_batch(conn, actor: dict, recall_id: str, payload: dict) -> dict:
    """批号/产品纠错：开启新版本并冻结；此前处置按物理入口沿用并留痕。"""
    recall = conn.execute(
        select(recalls).where(recalls.c.recall_id == recall_id)
    ).mappings().first()
    if recall is None:
        raise ServiceError(404, "RECALL_NOT_FOUND", "召回不存在。")
    require_hq(conn, actor, recall["brand_id"])
    if not payload.get("reason"):
        raise ServiceError(400, "REASON_REQUIRED", "批号纠错必须填写原因。")

    new_no = recall["current_version_no"] + 1
    version_id = _new_id("ver")
    now = _now_iso()
    version = {
        "version_id": version_id,
        "recall_id": recall_id,
        "version_no": new_no,
        "product_code": payload["product_code"],
        "product_name": payload["product_name"],
        "batch_no": payload["batch_no"],
        "risk_level": payload.get("risk_level", "high"),
        "effective_at": payload["effective_at"],
        "correction_reason": payload["reason"],
        "created_by_actor_id": actor["actor_id"],
        "created_at": now,
        "frozen_at": now,
    }
    if version["risk_level"] not in d.RISK_POLICY:
        raise ServiceError(400, "BAD_RISK", "风险等级须为 low/medium/high。")
    datetime.fromisoformat(version["effective_at"])

    conn.execute(recall_versions.insert().values(**version))
    _resolve_version(conn, version, recall["brand_id"])
    _carry_forward(conn, recall_id, version_id)

    conn.execute(
        recalls.update()
        .where(recalls.c.recall_id == recall_id)
        .values(current_version_no=new_no)
    )
    _timeline(
        conn, version_id, "-", "-", "version_opened",
        f"批号/产品纠错开启版本 v{new_no}：{payload['reason']}", actor["actor_id"],
    )
    return {"recall_id": recall_id, "version_id": version_id, "version_no": new_no}


def _carry_forward(conn, recall_id: str, new_version_id: str):
    """把上一版本对同一物理入口的终态处置带到新版本；原始回执保留在旧版本可溯。"""
    previous = conn.execute(
        select(recall_versions)
        .where(recall_versions.c.recall_id == recall_id)
        .where(recall_versions.c.version_id != new_version_id)
        .order_by(recall_versions.c.version_no.desc())
    ).mappings().first()
    if previous is None:
        return

    prior_targets = conn.execute(
        select(recall_targets).where(recall_targets.c.version_id == previous["version_id"])
    ).mappings().all()
    prior_by_outlet = {(t["target_type"], t["target_id"]): dict(t) for t in prior_targets}

    new_targets = conn.execute(
        select(recall_targets).where(recall_targets.c.version_id == new_version_id)
    ).mappings().all()
    for target in new_targets:
        prior = prior_by_outlet.get((target["target_type"], target["target_id"]))
        if prior is None or prior["latest_disposition"] not in d.FINAL_DISPOSITIONS:
            continue
        latest_receipt = conn.execute(
            select(receipts)
            .where(
                receipts.c.version_id == previous["version_id"],
                receipts.c.target_type == target["target_type"],
                receipts.c.target_id == target["target_id"],
                receipts.c.superseded == 0,
            )
            .order_by(receipts.c.submitted_at.desc())
        ).mappings().first()
        if latest_receipt is None:
            continue
        conn.execute(
            recall_targets.update()
            .where(recall_targets.c.target_uid == target["target_uid"])
            .values(
                latest_disposition=prior["latest_disposition"],
                latest_disposition_at=prior["latest_disposition_at"],
            )
        )
        # 新版本中该入口已有沿用的终态处置：不再重复触达，作废待发通知。
        conn.execute(
            notifications_table.update()
            .where(
                notifications_table.c.version_id == new_version_id,
                notifications_table.c.target_type == target["target_type"],
                notifications_table.c.target_id == target["target_id"],
                notifications_table.c.status == "pending",
            )
            .values(status="cancelled")
        )
        _timeline(
            conn, new_version_id, target["target_type"], target["target_id"],
            "disposition_carried",
            f"沿用版本 v{previous['version_no']} 的处置 {prior['latest_disposition']}"
            f"（原回执 {latest_receipt['receipt_id']}，原始动作不重放、不覆盖）。",
        )


# ---------------------------------------------------------------- 回执与异议


def _load_target_for_version(conn, version_id: str, ttype: str, tid: str):
    row = conn.execute(
        select(recall_targets).where(
            recall_targets.c.version_id == version_id,
            recall_targets.c.target_type == ttype,
            recall_targets.c.target_id == tid,
        )
    ).mappings().first()
    if row is None:
        raise ServiceError(404, "TARGET_NOT_FOUND", "该入口不在此召回版本的定责范围内。")
    return dict(row)


def submit_receipt(conn, actor: dict | None, payload: dict) -> dict:
    """门店/设备回报。设备以 (device_id, journal_no) 幂等；迟到者不覆盖较新处置。"""
    disposition = payload["disposition"]
    if disposition not in d.VALID_DISPOSITIONS:
        raise ServiceError(400, "BAD_DISPOSITION", "处置类型不合法。")

    duplicate = conn.execute(
        select(receipts).where(
            receipts.c.device_id == payload["device_id"],
            receipts.c.device_journal_no == str(payload["journal_no"]),
        )
    ).mappings().first()
    if duplicate is not None:
        # 离线上传重放：以设备自身流水去重，直接回放首次结果，不产生第二条动作。
        return {
            "receipt_id": duplicate["receipt_id"],
            "deduplicated": True,
            "superseded": bool(duplicate["superseded"]),
        }

    version = conn.execute(
        select(recall_versions).where(recall_versions.c.version_id == payload["version_id"])
    ).mappings().first()
    if version is None:
        raise ServiceError(404, "VERSION_NOT_FOUND", "召回版本不存在。")
    version = dict(version)
    ttype, tid = payload["target_type"], payload["target_id"]
    target = _load_target_for_version(conn, version["version_id"], ttype, tid)

    if target["status"] not in (
        d.STATUS_INCLUDED,
        d.STATUS_TRANSFERRED,
        d.STATUS_HISTORICAL,
        d.STATUS_REASSIGNED,
    ):
        raise ServiceError(409, "TARGET_NOT_ACTIONABLE", "该入口为待定/排除状态，不能回报处置。")

    if actor is not None:
        _authorize_outlet_action(conn, actor, version, target)

    submitted_at = payload["submitted_at"]
    datetime.fromisoformat(submitted_at)
    receipt_id = _new_id("rcp")
    # 迟到判定以设备自报时刻为准：任何不晚于当前最新处置的回报都是迟到者，
    # 包括晚到服务器的异议——一律留存，不压过较新的处置。
    is_late = (
        target["latest_disposition_at"] is not None
        and submitted_at <= target["latest_disposition_at"]
    )

    conn.execute(
        receipts.insert().values(
            receipt_id=receipt_id,
            version_id=version["version_id"],
            target_type=ttype,
            target_id=tid,
            disposition=disposition,
            detail=payload.get("detail"),
            device_id=payload["device_id"],
            device_journal_no=str(payload["journal_no"]),
            actor_id=actor["actor_id"] if actor else None,
            submitted_at=submitted_at,
            received_at=_now_iso(),
            superseded=1 if is_late else 0,
        )
    )

    if is_late:
        _timeline(
            conn, version["version_id"], ttype, tid, "late_receipt_ignored",
            f"迟到回执 {receipt_id}（{disposition}，设备时间 {submitted_at}）"
            f"不晚于当前处置 {target['latest_disposition']}（{target['latest_disposition_at']}），留存但不覆盖。",
            actor["actor_id"] if actor else None,
        )
        return {"receipt_id": receipt_id, "deduplicated": False, "superseded": True}

    if disposition == d.DISPOSITION_DISPUTE:
        dispute_id = _open_dispute(
            conn, version, ttype, tid,
            actor["actor_id"] if actor else payload["device_id"],
            submitted_at, payload.get("detail") or "门店提出归属异议。",
        )
        conn.execute(
            recall_targets.update()
            .where(recall_targets.c.target_uid == target["target_uid"])
            .values(latest_disposition=d.DISPOSITION_DISPUTE, latest_disposition_at=submitted_at)
        )
        _timeline(
            conn, version["version_id"], ttype, tid, "dispute_raised",
            f"归属异议 {dispute_id}：{payload.get('detail') or ''}",
            actor["actor_id"] if actor else None, at=submitted_at,
        )
        return {"receipt_id": receipt_id, "dispute_id": dispute_id, "superseded": False}

    conn.execute(
        recall_targets.update()
        .where(recall_targets.c.target_uid == target["target_uid"])
        .values(latest_disposition=disposition, latest_disposition_at=submitted_at)
    )
    _timeline(
        conn, version["version_id"], ttype, tid, f"receipt_{disposition}",
        f"回执 {receipt_id}：{payload.get('detail') or disposition}",
        actor["actor_id"] if actor else None, at=submitted_at,
    )
    return {"receipt_id": receipt_id, "deduplicated": False, "superseded": False}


def _authorize_outlet_action(conn, actor: dict, version: dict, target: dict):
    """门店只接触完成处置所需：本门店/本入口或当前责任主体成员。"""
    if actor["role"] in ("store", "device") and actor.get("store_id") == target["target_id"]:
        return
    if actor["role"] == "entry" and actor.get("entry_id") == target["target_id"]:
        return
    if actor.get("operator_id") and actor["operator_id"] == target["responsible_operator_id"]:
        return
    if actor["role"] == "hq" and actor_brand_id(conn, actor) == _version_brand(conn, version):
        return
    raise ServiceError(403, "OUTLET_SCOPE_VIOLATION", "不能替其他入口回报处置。")


def _version_brand(conn, version: dict) -> str:
    row = conn.execute(
        select(recalls.c.brand_id).where(recalls.c.recall_id == version["recall_id"])
    ).first()
    return row[0]


def _open_dispute(conn, version, ttype, tid, raiser_id, raised_at, reason) -> str:
    existing = conn.execute(
        select(disputes_table.c.dispute_id).where(
            disputes_table.c.version_id == version["version_id"],
            disputes_table.c.target_type == ttype,
            disputes_table.c.target_id == tid,
            disputes_table.c.status == "open",
        )
    ).first()
    if existing:
        return existing[0]
    dispute_id = _new_id("dsp")
    conn.execute(
        disputes_table.insert().values(
            dispute_id=dispute_id,
            version_id=version["version_id"],
            target_type=ttype,
            target_id=tid,
            raised_by_actor_id=raiser_id,
            raised_at=raised_at,
            reason=reason,
            status="open",
        )
    )
    return dispute_id


def review_dispute(conn, actor: dict, dispute_id: str, payload: dict) -> dict:
    dispute = conn.execute(
        select(disputes_table).where(disputes_table.c.dispute_id == dispute_id)
    ).mappings().first()
    if dispute is None:
        raise ServiceError(404, "DISPUTE_NOT_FOUND", "异议不存在。")
    dispute = dict(dispute)
    version = conn.execute(
        select(recall_versions).where(recall_versions.c.version_id == dispute["version_id"])
    ).mappings().first()
    recall = conn.execute(
        select(recalls).where(recalls.c.recall_id == version["recall_id"])
    ).mappings().first()
    require_hq(conn, actor, recall["brand_id"])

    if dispute["status"] != "open":
        raise ServiceError(409, "DISPUTE_CLOSED", "异议已复核。")
    if dispute["raised_by_actor_id"] == actor["actor_id"]:
        raise ServiceError(409, "REVIEWER_CONFLICT", "不能复核自己发起的异议。")

    # 关系变更的发起人不能参与同一异议的复核：
    # 该入口任何一段关系（含定责依据窗口）由其变更过，即视为利益相关。
    initiated = conn.execute(
        select(outlet_relations.c.relation_id).where(
            outlet_relations.c.target_type == dispute["target_type"],
            outlet_relations.c.target_id == dispute["target_id"],
            outlet_relations.c.changed_by_actor_id == actor["actor_id"],
        )
    ).first()
    if initiated is not None:
        raise ServiceError(
            409, "REVIEWER_RELATION_CONFLICT",
            "复核人发起过该入口的关系变更，须回避本异议的复核。",
        )

    decision = payload["decision"]
    if decision not in ("uphold", "reassign"):
        raise ServiceError(400, "BAD_DECISION", "决定须为 uphold 或 reassign。")
    now = _now_iso()
    target = _load_target_for_version(
        conn, dispute["version_id"], dispute["target_type"], dispute["target_id"]
    )
    if decision == "reassign":
        to_operator = payload.get("to_operator_id")
        if not to_operator:
            raise ServiceError(400, "OPERATOR_REQUIRED", "改派决定须指定新主体。")
        _reassign_target(
            conn, dict(version), dispute["target_type"], dispute["target_id"],
            to_operator, actor["actor_id"], f"异议 {dispute_id} 复核改派：{payload.get('note', '')}",
        )
    elif target["latest_disposition"] == d.DISPOSITION_DISPUTE:
        # 维持原归属：异议挂起态解除，入口回到待处置；逾期计时从未暂停（due_at 不变）。
        conn.execute(
            recall_targets.update()
            .where(recall_targets.c.target_uid == target["target_uid"])
            .values(latest_disposition=None, latest_disposition_at=None)
        )
    conn.execute(
        disputes_table.update()
        .where(disputes_table.c.dispute_id == dispute_id)
        .values(
            status="reviewed",
            reviewer_actor_id=actor["actor_id"],
            reviewed_at=now,
            decision=decision,
            review_note=payload.get("note"),
        )
    )
    _timeline(
        conn, dispute["version_id"], dispute["target_type"], dispute["target_id"],
        "dispute_reviewed",
        f"异议 {dispute_id} 复核结论 {decision}，复核人 {actor['actor_id']}。",
        actor["actor_id"],
    )
    return {"dispute_id": dispute_id, "decision": decision}


def reassign_target(conn, actor: dict, version_id: str, ttype: str, tid: str, payload: dict) -> dict:
    version = conn.execute(
        select(recall_versions).where(recall_versions.c.version_id == version_id)
    ).mappings().first()
    if version is None:
        raise ServiceError(404, "VERSION_NOT_FOUND", "版本不存在。")
    recall = conn.execute(
        select(recalls).where(recalls.c.recall_id == version["recall_id"])
    ).mappings().first()
    require_hq(conn, actor, recall["brand_id"])
    target = _load_target_for_version(conn, version_id, ttype, tid)
    result = _reassign_target(
        conn, dict(version), ttype, tid,
        payload["to_operator_id"], actor["actor_id"],
        f"总部人工改派：{payload.get('reason', '')}",
    )
    return result


def _reassign_target(conn, version: dict, ttype: str, tid: str, to_operator_id: str,
                     by_actor_id: str, reason: str) -> dict:
    target = _load_target_for_version(conn, version["version_id"], ttype, tid)
    op = conn.execute(
        select(operators).where(operators.c.operator_id == to_operator_id)
    ).mappings().first()
    if op is None:
        raise ServiceError(404, "OPERATOR_NOT_FOUND", "新责任主体不存在。")
    brand_id = _version_brand(conn, version)
    if op["brand_id"] != brand_id:
        raise ServiceError(403, "BRAND_SCOPE_VIOLATION", "不能改派给品牌外主体，请走跨品牌协查。")

    reassignment_id = _new_id("ras")
    conn.execute(
        reassignments.insert().values(
            reassignment_id=reassignment_id,
            version_id=version["version_id"],
            target_type=ttype,
            target_id=tid,
            from_operator_id=target["responsible_operator_id"],
            to_operator_id=to_operator_id,
            by_actor_id=by_actor_id,
            reason=reason,
            created_at=_now_iso(),
        )
    )
    due_at = target["due_at"] or d.deadline_for(version["risk_level"], version["effective_at"])
    conn.execute(
        recall_targets.update()
        .where(recall_targets.c.target_uid == target["target_uid"])
        .values(
            status=d.STATUS_REASSIGNED,
            reason_code=d.REASON_MANUAL,
            reason_detail=f"人工改派：{reason}",
            responsible_operator_id=to_operator_id,
            relation_kind="manual_assignment",
            manually_reassigned=1,
            due_at=due_at,
        )
    )
    _timeline(
        conn, version["version_id"], ttype, tid, "manually_reassigned",
        f"责任主体 {target['responsible_operator_id']} → {to_operator_id}；{reason}",
        by_actor_id,
    )
    # 旧主体的待发通知作废，避免改派后仍向其催办；已发出的保留在时间线上可溯。
    conn.execute(
        notifications_table.update()
        .where(
            notifications_table.c.version_id == version["version_id"],
            notifications_table.c.target_type == ttype,
            notifications_table.c.target_id == tid,
            notifications_table.c.status == "pending",
        )
        .values(status="cancelled")
    )
    _notify(
        conn, version["version_id"], ttype, tid, 0, to_operator_id,
        {"kind": "action_required_after_reassignment", "reason": reason, "deadline": due_at},
        notice_kind="reassignment",
    )
    return {"reassignment_id": reassignment_id, "to_operator_id": to_operator_id}


# ---------------------------------------------------------------- 升级与补发


def run_sweep(conn, now: str | None = None) -> dict:
    """逾期升级 + 到期外发。按绝对时间重放，停服多久都能一次补齐、再扫为空。"""
    # 统一规范化为带偏移的 ISO，避免 "2026-.. 00:00" 与 "2026-..+00:00" 等写法做字符串比较错位。
    now = d.iso(d.parse_iso(now)) if now else _now_iso()

    actionables = conn.execute(
        select(recall_targets, recall_versions.c.risk_level)
        .select_from(
            recall_targets.join(
                recall_versions,
                recall_targets.c.version_id == recall_versions.c.version_id,
            )
        )
        .where(recall_targets.c.due_at.is_not(None))
    ).mappings().all()

    escalated = 0
    for row in actionables:
        if row["latest_disposition"] in d.FINAL_DISPOSITIONS:
            continue
        due_levels = d.escalation_levels_due(
            row["risk_level"], row["due_at"], now, row["escalated_level"]
        )
        max_level = len(d.RISK_POLICY[row["risk_level"]]["escalation_offsets_hours"])
        for level in due_levels:
            conn.execute(
                escalations_table.insert().values(
                    escalation_id=_new_id("esc"),
                    version_id=row["version_id"],
                    target_type=row["target_type"],
                    target_id=row["target_id"],
                    from_level=row["escalated_level"],
                    to_level=level,
                    created_at=now,
                    reason=f"高风险对象逾期未完成处置，升级至 L{level}。",
                )
            )
            recipient = None
            if d.ESCALATION_RECIPIENTS[level] == "responsible":
                recipient = row["responsible_operator_id"]
            _notify(
                conn, row["version_id"], row["target_type"], row["target_id"],
                level, recipient,
                {"kind": "escalation", "level": level},
                due_at=now,
                notice_kind="escalation",
            )
            conn.execute(
                recall_targets.update()
                .where(recall_targets.c.target_uid == row["target_uid"])
                .values(
                    escalated_level=level,
                    final_escalated=1 if level == max_level else 0,
                )
            )
            escalated += 1

    # 升级补齐后统一外发：初始触达、备查、改派、各级催办凡到期者一次发出。
    pending = conn.execute(
        select(notifications_table)
        .where(notifications_table.c.status == "pending")
        .where(notifications_table.c.due_at <= now)
    ).mappings().all()
    dispatched = 0
    for row in pending:
        conn.execute(
            notifications_table.update()
            .where(notifications_table.c.notification_id == row["notification_id"])
            .values(status="sent", sent_at=now)
        )
        dispatched += 1

    return {"at": now, "dispatched": dispatched, "escalations": escalated}


# ---------------------------------------------------------------- 视图


def _version_and_scope(conn, actor: dict, version_id: str):
    version = conn.execute(
        select(recall_versions).where(recall_versions.c.version_id == version_id)
    ).mappings().first()
    if version is None:
        raise ServiceError(404, "VERSION_NOT_FOUND", "版本不存在。")
    version = dict(version)
    recall = conn.execute(
        select(recalls).where(recalls.c.recall_id == version["recall_id"])
    ).mappings().first()
    require_hq(conn, actor, recall["brand_id"])
    return version, dict(recall)


def coverage_view(conn, actor: dict, version_id: str) -> dict:
    """负责人按召回版本看到的覆盖率；所有数字来自该版本冻结的关系事实。"""
    version, recall = _version_and_scope(conn, actor, version_id)
    rows = conn.execute(
        select(recall_targets).where(recall_targets.c.version_id == version_id)
    ).mappings().all()

    actionable_status = {
        d.STATUS_INCLUDED, d.STATUS_TRANSFERRED, d.STATUS_HISTORICAL, d.STATUS_REASSIGNED
    }
    now = _now_iso()
    by_status: dict[str, int] = {}
    by_disposition: dict[str, int] = {}
    overdue = 0
    final_done = 0
    manually_reassigned = 0
    for row in rows:
        by_status[row["status"]] = by_status.get(row["status"], 0) + 1
        if row["manually_reassigned"]:
            manually_reassigned += 1
        if row["status"] in actionable_status:
            if row["latest_disposition"] in d.FINAL_DISPOSITIONS:
                final_done += 1
                key = row["latest_disposition"]
                by_disposition[key] = by_disposition.get(key, 0) + 1
            elif row["due_at"] and row["due_at"] < now:
                overdue += 1

    # 各级升级计数取审计表（同一入口逐级升级时在 L1、L2、L3 各计一次）。
    level_rows = conn.execute(
        select(escalations_table.c.to_level)
        .where(escalations_table.c.version_id == version_id)
    ).all()
    escalated_by_level: dict[str, int] = {}
    for (level,) in level_rows:
        key = f"L{level}"
        escalated_by_level[key] = escalated_by_level.get(key, 0) + 1

    actionable_total = sum(c for s, c in by_status.items() if s in actionable_status)
    return {
        "recall_id": version["recall_id"],
        "version_id": version_id,
        "version_no": version["version_no"],
        "product": {"code": version["product_code"], "name": version["product_name"]},
        "batch_no": version["batch_no"],
        "risk_level": version["risk_level"],
        "effective_at": version["effective_at"],
        "frozen_at": version["frozen_at"],
        "brand_id": recall["brand_id"],
        "totals": {
            "all_entries": len(rows),
            "action_required": actionable_total,
            "final_disposed": final_done,
            "overdue_open": overdue,
            "manually_reassigned": manually_reassigned,
            "coverage_rate": round(final_done / actionable_total, 4) if actionable_total else None,
        },
        "by_status": by_status,
        "by_disposition": by_disposition,
        "escalated_by_level": escalated_by_level,
    }


def risk_entries_view(conn, actor: dict, version_id: str) -> dict:
    """风险入口：定责断裂/品牌外移/高等级升级未闭环的入口。"""
    version, _ = _version_and_scope(conn, actor, version_id)
    rows = conn.execute(
        select(recall_targets).where(recall_targets.c.version_id == version_id)
    ).mappings().all()
    result = []
    for row in rows:
        risk_kind = None
        open_case = row["latest_disposition"] not in d.FINAL_DISPOSITIONS
        if row["status"] == d.STATUS_UNRESOLVED:
            risk_kind = "attribution_broken"
        elif row["status"] == d.STATUS_EXCLUDED and row["reason_code"] == d.REASON_OUT_OF_BRAND:
            risk_kind = "moved_outside_brand"
        elif open_case and row["escalated_level"] >= 2:
            risk_kind = "escalation_unresolved"
        if risk_kind:
            result.append(
                {
                    "target_type": row["target_type"],
                    "target_id": row["target_id"],
                    "status": row["status"],
                    "risk_kind": risk_kind,
                    "reason_code": row["reason_code"],
                    "reason_detail": row["reason_detail"],
                    "historical_operator_id": row["historical_operator_id"],
                    "escalated_level": row["escalated_level"],
                    "manually_reassigned": bool(row["manually_reassigned"]),
                }
            )
    return {"version_id": version_id, "risk_entries": result}


def targets_view(conn, actor: dict, version_id: str) -> dict:
    """总部目标清单：含逐条纳入/排除/转交理由（仅本品牌）。"""
    version, _ = _version_and_scope(conn, actor, version_id)
    rows = conn.execute(
        select(recall_targets).where(recall_targets.c.version_id == version_id)
    ).mappings().all()
    return {
        "version_id": version_id,
        "targets": [
            {
                "target_type": r["target_type"],
                "target_id": r["target_id"],
                "status": r["status"],
                "reason_code": r["reason_code"],
                "reason_detail": r["reason_detail"],
                "responsible_operator_id": r["responsible_operator_id"],
                "historical_operator_id": r["historical_operator_id"],
                "relation_kind": r["relation_kind"],
                "latest_disposition": r["latest_disposition"],
                "latest_disposition_at": r["latest_disposition_at"],
                "due_at": r["due_at"],
                "escalated_level": r["escalated_level"],
                "manually_reassigned": bool(r["manually_reassigned"]),
            }
            for r in rows
        ],
    }


def timeline_view(conn, actor: dict, version_id: str, ttype: str, tid: str) -> dict:
    version, _ = _version_and_scope(conn, actor, version_id)
    _load_target_for_version(conn, version_id, ttype, tid)
    rows = conn.execute(
        select(target_timeline)
        .where(
            target_timeline.c.version_id == version_id,
            target_timeline.c.target_type == ttype,
            target_timeline.c.target_id == tid,
        )
        .order_by(target_timeline.c.seq)
    ).mappings().all()
    receipts_rows = conn.execute(
        select(receipts)
        .where(
            receipts.c.version_id == version_id,
            receipts.c.target_type == ttype,
            receipts.c.target_id == tid,
        )
        .order_by(receipts.c.submitted_at)
    ).mappings().all()
    return {
        "version_id": version_id,
        "target": {"target_type": ttype, "target_id": tid},
        "events": [
            {
                "kind": r["kind"],
                "actor_id": r["actor_id"],
                "created_at": r["created_at"],
                "detail": r["detail"],
            }
            for r in rows
        ],
        "receipts": [
            {
                "receipt_id": r["receipt_id"],
                "disposition": r["disposition"],
                "detail": r["detail"],
                "submitted_at": r["submitted_at"],
                "received_at": r["received_at"],
                "superseded": bool(r["superseded"]),
                "carried_from_version_id": r["carried_from_version_id"],
                "device_id": r["device_id"],
            }
            for r in receipts_rows
        ],
    }


def my_tasks_view(conn, actor: dict) -> dict:
    """门店/线上入口只拿到完成处置所需的信息，看不到其他入口与品牌内全景。"""
    if actor["role"] in ("store", "device") and actor.get("store_id"):
        ttype, tid = d.TARGET_STORE, actor["store_id"]
    elif actor["role"] == "entry" and actor.get("entry_id"):
        ttype, tid = d.TARGET_ENTRY, actor["entry_id"]
    elif actor.get("operator_id"):
        return _operator_tasks(conn, actor)
    else:
        raise ServiceError(403, "NO_OUTLET_SCOPE", "该操作者没有可处置的入口。")

    rows = conn.execute(
        select(recall_targets, recall_versions, recalls.c.brand_id)
        .select_from(
            recall_targets.join(
                recall_versions,
                recall_targets.c.version_id == recall_versions.c.version_id,
            ).join(recalls, recall_versions.c.recall_id == recalls.c.recall_id)
        )
        .where(
            recall_targets.c.target_type == ttype,
            recall_targets.c.target_id == tid,
            recall_targets.c.status.in_(
                [d.STATUS_INCLUDED, d.STATUS_TRANSFERRED, d.STATUS_HISTORICAL, d.STATUS_REASSIGNED]
            ),
        )
    ).mappings().all()
    return {
        "tasks": [
            {
                "version_id": r["version_id"],
                "product_name": r["product_name"],
                "batch_no": r["batch_no"],
                "risk_level": r["risk_level"],
                "effective_at": r["effective_at"],
                "due_at": r["due_at"],
                "why": r["reason_detail"],
                "latest_disposition": r["latest_disposition"],
                "allowed_replies": ["stop_sale", "quarantined", "not_found", "dispute"],
            }
            for r in rows
        ]
    }


def _operator_tasks(conn, actor: dict) -> dict:
    rows = conn.execute(
        select(recall_targets, recall_versions)
        .select_from(
            recall_targets.join(
                recall_versions,
                recall_targets.c.version_id == recall_versions.c.version_id,
            )
        )
        .where(
            recall_targets.c.responsible_operator_id == actor["operator_id"],
            recall_targets.c.status.in_(
                [d.STATUS_INCLUDED, d.STATUS_TRANSFERRED, d.STATUS_HISTORICAL, d.STATUS_REASSIGNED]
            ),
        )
    ).mappings().all()
    return {
        "operator_id": actor["operator_id"],
        "tasks": [
            {
                "version_id": r["version_id"],
                "target_type": r["target_type"],
                "target_id": r["target_id"],
                "product_name": r["product_name"],
                "batch_no": r["batch_no"],
                "risk_level": r["risk_level"],
                "due_at": r["due_at"],
                "latest_disposition": r["latest_disposition"],
                "escalated_level": r["escalated_level"],
                "why": r["reason_detail"],
            }
            for r in rows
        ],
    }


def pending_notifications_view(conn, actor: dict) -> dict:
    """外发箱视图（总部）：停服期间未发出的通知保持 pending，恢复后续发。"""
    if actor["role"] != "hq":
        raise ServiceError(403, "HQ_ONLY", "外发箱仅品牌总部可见。")
    brand_id = actor_brand_id(conn, actor)
    rows = conn.execute(
        select(notifications_table)
        .select_from(
            notifications_table.join(
                recall_versions,
                notifications_table.c.version_id == recall_versions.c.version_id,
            ).join(recalls, recall_versions.c.recall_id == recalls.c.recall_id)
        )
        .where(recalls.c.brand_id == brand_id)
        .order_by(notifications_table.c.due_at)
    ).mappings().all()
    return {
        "notifications": [
            {
                "notification_id": r["notification_id"],
                "version_id": r["version_id"],
                "target_type": r["target_type"],
                "target_id": r["target_id"],
                "level": r["level"],
                "notice_kind": r["notice_kind"],
                "status": r["status"],
                "due_at": r["due_at"],
                "sent_at": r["sent_at"],
            }
            for r in rows
        ]
    }
