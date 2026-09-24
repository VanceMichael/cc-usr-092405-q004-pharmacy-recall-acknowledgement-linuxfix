"""召回定责的纯领域逻辑：不触碰数据库，便于单测与推理。

核心原则：责任归属必须按事实发生当时的关系回放——
供货当日的经营主体不能被召回生效时的新主体替换，反之亦然。
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

# 目标在一次召回版本中的定责结论。
STATUS_INCLUDED = "included"            # 纳入：供货时与生效时为同一责任主体
STATUS_TRANSFERRED = "transferred"      # 转交：主体已变更，新主体处置、旧主体留存备查
STATUS_HISTORICAL = "historical"        # 仅有历史关系：入口退网，进货当日主体兜底
STATUS_UNRESOLVED = "unresolved"        # 待定：进货当日查不到有效关系，需人工改派
STATUS_EXCLUDED = "excluded"            # 排除：不属于本次召回触达范围
STATUS_REASSIGNED = "reassigned"        # 人工改派后已定责

# 纳入/排除/转交的逐条理由（审计与门店告知共用）。
REASON_STABLE = "SUPPLY_OPERATOR_STILL_RESPONSIBLE"
REASON_TRANSFERRED = "OPERATOR_CHANGED_AFTER_SUPPLY"
REASON_NO_CURRENT = "NO_VALID_RELATION_AT_EFFECTIVE_TIME"
REASON_NO_SUPPLY_RELATION = "NO_VALID_RELATION_AT_SUPPLY_TIME"
REASON_OUT_OF_BRAND = "CURRENT_OPERATOR_OUTSIDE_BRAND"
REASON_AFTER_EFFECTIVE = "SUPPLY_HAPPENED_AFTER_RECALL_EFFECTIVE"
REASON_MANUAL = "MANUALLY_REASSIGNED"

DISPOSITION_STOP_SALE = "stop_sale"
DISPOSITION_QUARANTINED = "quarantined"
DISPOSITION_NOT_FOUND = "not_found"
DISPOSITION_DISPUTE = "dispute"
FINAL_DISPOSITIONS = {DISPOSITION_STOP_SALE, DISPOSITION_QUARANTINED, DISPOSITION_NOT_FOUND}
VALID_DISPOSITIONS = FINAL_DISPOSITIONS | {DISPOSITION_DISPUTE}

RISK_HIGH = "high"
RISK_MEDIUM = "medium"
RISK_LOW = "low"

# SLA 锚定在召回生效的绝对时刻；升级偏移也是绝对小时差，
# 因此停服恢复后只需按当前时间重放，不必依赖进程内计时器。
RISK_POLICY = {
    RISK_HIGH: {"deadline_hours": 24, "escalation_offsets_hours": (0, 12, 36)},
    RISK_MEDIUM: {"deadline_hours": 72, "escalation_offsets_hours": ()},
    RISK_LOW: {"deadline_hours": 168, "escalation_offsets_hours": ()},
}

# 升级收件人：L1 责任主体质量负责人，L2 责任主体负责人，L3 品牌总部（recipient 为 None）。
ESCALATION_RECIPIENTS = {1: "responsible", 2: "responsible", 3: "hq"}

TARGET_STORE = "store"
TARGET_ENTRY = "online_entry"


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def parse_iso(value: str) -> datetime:
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        # 查询串中 '+' 被解码为空格（"…T00:00:00 00:00"），还原为偏移写法。
        import re

        repaired = re.sub(r" (\d{2}:\d{2})(?::\d{2})?$", r"+\1", value)
        return datetime.fromisoformat(repaired)


def deadline_for(risk_level: str, effective_at: str) -> str:
    policy = RISK_POLICY[risk_level]
    return iso(parse_iso(effective_at) + timedelta(hours=policy["deadline_hours"]))


@dataclass(frozen=True)
class Relation:
    relation_id: str
    target_type: str
    target_id: str
    operator_id: str
    relation_kind: str
    valid_from: str
    valid_to: str | None
    changed_by_actor_id: str


def relation_active_at(relation: Relation, moment: str) -> bool:
    """半开区间 [valid_from, valid_to)：交接当日归属新窗口，旧窗口恰好止于前一刻。"""
    if relation.valid_from > moment:
        return False
    return relation.valid_to is None or moment < relation.valid_to


def relations_as_of(relations: list[Relation], target_type: str, target_id: str, moment: str) -> list[Relation]:
    active = [
        r
        for r in relations
        if r.target_type == target_type and r.target_id == target_id and relation_active_at(r, moment)
    ]
    # 同一时点理论上只有一条主关系；若出现重叠，生效起始最晚者优先，全部返回留痕。
    return sorted(active, key=lambda r: r.valid_from, reverse=True)


@dataclass
class SupplyFact:
    supply_id: str
    target_type: str
    target_id: str
    supplied_at: str
    supply_at_relation: Relation | None = None


@dataclass
class TargetAttribution:
    target_type: str
    target_id: str
    status: str
    reason_code: str
    reason_detail: str
    responsible_operator_id: str | None
    historical_operator_id: str | None
    relation_kind: str | None
    basis_relation_id: str | None
    supply_ids: list[str] = field(default_factory=list)


def _describe(status: str, *, current: Relation | None, historical: Relation | None) -> str:
    if status == STATUS_INCLUDED:
        return (
            f"供货当日与召回生效时的有效关系均指向 {current.operator_id}（{current.relation_kind}），"
            "责任主体未变，纳入触达并由其完成处置。"
        )
    if status == STATUS_TRANSFERRED:
        return (
            f"供货发生时由 {historical.operator_id}（{historical.relation_kind}）经手，"
            f"召回生效时有效关系已变更为 {current.operator_id}（{current.relation_kind}）；"
            "处置责任转交新主体，旧主体保留进货当日事实并接收备查告知，不得以当前关系抹除历史归属。"
        )
    if status == STATUS_HISTORICAL:
        return (
            f"供货发生时由 {historical.operator_id}（{historical.relation_kind}）经手，"
            "召回生效时该入口已无有效经营关系（关店/退网）；由进货当日责任主体兜底处置。"
        )
    if status == STATUS_UNRESOLVED and historical is None:
        return (
            "供货当日查不到任何有效经营/许可关系，无法用当前主体倒推责任；"
            "列入风险入口等待总部人工改派，暂不自动触达任何经营主体。"
        )
    if status == STATUS_UNRESOLVED:
        return (
            f"供货当日主体 {historical.operator_id} 与生效时现有记录无法连续对应，"
            "责任链断裂，列入风险入口等待人工核定。"
        )
    if status == STATUS_EXCLUDED:
        return "供货晚于召回生效时刻，不属于本批次召回范围，排除并留痕。"
    return status


def classify_target(
    target_type: str,
    target_id: str,
    supplies: list[SupplyFact],
    relations: list[Relation],
    effective_at: str,
    operator_brand: dict[str, str],
    brand_id: str,
) -> TargetAttribution:
    """按双时点对单个物理入口（实体门店或线上入口）定责。

    supplies 已按 (brand, product, batch) 过滤；relations 为该入口全部历史关系。
    """
    supply_ids = sorted(s.supply_id for s in supplies)
    latest = max(supplies, key=lambda s: s.supplied_at)

    if latest.supplied_at > effective_at:
        return TargetAttribution(
            target_type, target_id, STATUS_EXCLUDED, REASON_AFTER_EFFECTIVE,
            _describe(STATUS_EXCLUDED, current=None, historical=None),
            None, None, None, None, supply_ids,
        )

    historical = latest.supply_at_relation
    current_relations = relations_as_of(relations, target_type, target_id, effective_at)
    current = current_relations[0] if current_relations else None

    if historical is None and current is None:
        return TargetAttribution(
            target_type, target_id, STATUS_UNRESOLVED, REASON_NO_SUPPLY_RELATION,
            _describe(STATUS_UNRESOLVED, current=None, historical=None),
            None, None, None, None, supply_ids,
        )

    if historical is None:
        return TargetAttribution(
            target_type, target_id, STATUS_UNRESOLVED, REASON_NO_SUPPLY_RELATION,
            _describe(STATUS_UNRESOLVED, current=current, historical=None),
            None, None,
            current.relation_kind if current else None,
            current.relation_id if current else None, supply_ids,
        )

    if current is None:
        return TargetAttribution(
            target_type, target_id, STATUS_HISTORICAL, REASON_NO_CURRENT,
            _describe(STATUS_HISTORICAL, current=None, historical=historical),
            historical.operator_id, historical.operator_id,
            historical.relation_kind, historical.relation_id, supply_ids,
        )

    if current.operator_id == historical.operator_id:
        return TargetAttribution(
            target_type, target_id, STATUS_INCLUDED, REASON_STABLE,
            _describe(STATUS_INCLUDED, current=current, historical=historical),
            current.operator_id, None, current.relation_kind, current.relation_id, supply_ids,
        )

    if operator_brand.get(current.operator_id) != brand_id:
        # 当前主体已属其他品牌：本品牌无权触达，绝不静默丢弃——
        # 旧主体留痕、总部风险入口显式呈现，走跨品牌协查。
        detail = (
            f"供货由 {historical.operator_id} 经手；生效时入口归属品牌外主体 "
            f"{current.operator_id}，本品牌不能直接触达，排除自动派发并提交总部协查。"
        )
        return TargetAttribution(
            target_type, target_id, STATUS_EXCLUDED, REASON_OUT_OF_BRAND, detail,
            None, historical.operator_id, historical.relation_kind,
            historical.relation_id, supply_ids,
        )

    return TargetAttribution(
        target_type, target_id, STATUS_TRANSFERRED, REASON_TRANSFERRED,
        _describe(STATUS_TRANSFERRED, current=current, historical=historical),
        current.operator_id, historical.operator_id,
        current.relation_kind, current.relation_id, supply_ids,
    )


def escalation_levels_due(risk_level: str, due_at: str, now: str, current_level: int) -> list[int]:
    """返回截至 now 应达到的升级级别序列（停服恢复后可逐级补齐审计链）。"""
    policy = RISK_POLICY[risk_level]
    offsets = policy["escalation_offsets_hours"]
    if not offsets:
        return []
    due = parse_iso(due_at)
    due_levels = []
    for index, offset in enumerate(offsets, start=1):
        if now >= iso(due + timedelta(hours=offset)):
            due_levels.append(index)
    return [level for level in due_levels if level > current_level]
