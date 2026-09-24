"""身份与召回领域的表结构元数据。

时间轴约定：
- ``valid_from`` / ``valid_until``：业务生效期（关系或许可证在真实世界何时有效），
  用于"供货发生时""召回生效时"两个时点的 as-of 查询；
- ``recorded_at`` / ``recorded_by``：系统记录期（这条事实何时、由谁登记），
  用于异议复核回避——关系变更的发起人不能复核同一异议。
所有时刻均为 UTC ISO-8601 字符串，按字典序即与时间序一致。
"""

from sqlalchemy import (
    CheckConstraint,
    Column,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    text,
)

metadata = MetaData()

brands = Table(
    "brands",
    metadata,
    Column("brand_id", String, primary_key=True),
    Column("name", String, nullable=False),
    Column("created_at", String, nullable=False),
)

legal_entities = Table(
    "legal_entities",
    metadata,
    Column("entity_id", String, primary_key=True),
    Column("brand_id", String, ForeignKey("brands.brand_id"), nullable=False),
    Column("name", String, nullable=False),
    Column("created_at", String, nullable=False),
)

# 经营许可证本身也按版本管理：判定供货当日责任时取当时有效的许可证，
# 不能用今天换发的许可证倒推。
entity_licenses = Table(
    "entity_licenses",
    metadata,
    Column("license_id", String, primary_key=True),
    Column("entity_id", String, ForeignKey("legal_entities.entity_id"), nullable=False),
    Column("license_no", String, nullable=False),
    Column("valid_from", String, nullable=False),
    Column("valid_until", String, nullable=True),
    Column("recorded_at", String, nullable=False),
)

stores = Table(
    "stores",
    metadata,
    Column("store_id", String, primary_key=True),
    Column("brand_id", String, ForeignKey("brands.brand_id"), nullable=False),
    Column("name", String, nullable=False),
    Column("entry_type", String, nullable=False),  # physical | online
    Column("created_at", String, nullable=False),
    CheckConstraint("entry_type in ('physical', 'online')", name="ck_stores_entry_type"),
)

# 门店—经营主体关系的切片表。关系变更不改写旧行，而是闭合旧切片、插入新切片，
# 因此任意历史时点都能取到当时的有效关系。
entity_relations = Table(
    "entity_relations",
    metadata,
    Column("relation_id", String, primary_key=True),
    Column("brand_id", String, ForeignKey("brands.brand_id"), nullable=False),
    Column("store_id", String, ForeignKey("stores.store_id"), nullable=False),
    Column("entity_id", String, ForeignKey("legal_entities.entity_id"), nullable=False),
    Column("relation_type", String, nullable=False),  # direct | franchise
    Column("valid_from", String, nullable=False),
    Column("valid_until", String, nullable=True),
    Column("recorded_at", String, nullable=False),
    Column("recorded_by", String, nullable=False),
)

# 供货流水：批号经由哪个主体、到达哪个入口（含线上入口）的事实证据。
supplies = Table(
    "supplies",
    metadata,
    Column("supply_id", String, primary_key=True),
    Column("brand_id", String, ForeignKey("brands.brand_id"), nullable=False),
    Column("store_id", String, ForeignKey("stores.store_id"), nullable=False),
    Column("product_code", String, nullable=False),
    Column("product_name", String, nullable=False),
    Column("batch_no", String, nullable=False),
    Column("supplied_at", String, nullable=False),
    Column("supplier_entity_id", String, ForeignKey("legal_entities.entity_id"), nullable=True),
    Column("quantity", Integer, nullable=False, server_default="0"),
    Column("recorded_at", String, nullable=False),
)

# 调用方身份。role: hq（总部，按 brand_id 限定）、reviewer（异议复核）、
# entity（经营主体账户）、store（门店/设备账户）、system。
actors = Table(
    "actors",
    metadata,
    Column("actor_id", String, primary_key=True),
    Column("role", String, nullable=False),
    Column("brand_id", String, ForeignKey("brands.brand_id"), nullable=True),
    Column("entity_id", String, ForeignKey("legal_entities.entity_id"), nullable=True),
    Column("store_id", String, ForeignKey("stores.store_id"), nullable=True),
    Column("created_at", String, nullable=False),
    CheckConstraint(
        "role in ('hq', 'reviewer', 'entity', 'store', 'system')",
        name="ck_actors_role",
    ),
)

recalls = Table(
    "recalls",
    metadata,
    Column("recall_id", String, primary_key=True),
    Column("brand_id", String, ForeignKey("brands.brand_id"), nullable=False),
    Column("product_code", String, nullable=False),
    Column("product_name", String, nullable=False),
    Column("risk_level", String, nullable=False),  # high | medium | low
    Column("effective_at", String, nullable=False),
    Column("created_by", String, nullable=False),
    Column("created_at", String, nullable=False),
    Column("current_version_no", Integer, nullable=False, server_default="1"),
    CheckConstraint("risk_level in ('high', 'medium', 'low')", name="ck_recalls_risk"),
)

# 批号纠错不开新召回单，而是开新版本；目标、回执、时间线都挂在版本上。
recall_versions = Table(
    "recall_versions",
    metadata,
    Column("version_id", String, primary_key=True),
    Column("recall_id", String, ForeignKey("recalls.recall_id"), nullable=False),
    Column("version_no", Integer, nullable=False),
    Column("batch_no", String, nullable=False),
    Column("reason", String, nullable=True),
    Column("created_at", String, nullable=False),
    Index("ix_recall_versions_recall", "recall_id", "version_no", unique=True),
)

# 每个入口在一个版本下的判定快照：供货时主体、生效时主体、纳入/转交/排除及理由。
recall_targets = Table(
    "recall_targets",
    metadata,
    Column("target_id", String, primary_key=True),
    Column("version_id", String, ForeignKey("recall_versions.version_id"), nullable=False),
    Column("store_id", String, ForeignKey("stores.store_id"), nullable=False),
    Column("entry_type", String, nullable=False),
    Column("supply_id", String, ForeignKey("supplies.supply_id"), nullable=True),
    Column("supply_at", String, nullable=True),
    Column("supply_time_entity_id", String, nullable=True),
    Column("supply_license_no", String, nullable=True),
    Column("current_entity_id", String, nullable=True),
    Column("disposition", String, nullable=False),  # included | transferred | excluded
    Column("touch_entity_id", String, nullable=True),
    Column("reason_code", String, nullable=False),
    Column("reason_detail", Text, nullable=False),
    Column("current_action", String, nullable=True),
    Column("current_action_at", String, nullable=True),
    Column("completed_at", String, nullable=True),
    Column("created_at", String, nullable=False),
    Index("ix_targets_version_store", "version_id", "store_id", unique=True),
)

# 回执事件流水。(device_id, client_seq) 唯一：设备离线上传以自身流水去重。
# applied=0 的迟到/重复事件仍保留，供时间线审计，但不改变目标当前处置。
receipts = Table(
    "receipts",
    metadata,
    Column("receipt_id", String, primary_key=True),
    Column("target_id", String, ForeignKey("recall_targets.target_id"), nullable=False),
    Column("version_id", String, nullable=False),
    Column("device_id", String, nullable=False),
    Column("client_seq", Integer, nullable=False),
    Column("action", String, nullable=False),
    # stopped_sale | quarantined | not_found | attribution_dispute
    Column("action_at", String, nullable=False),
    Column("recorded_at", String, nullable=False),
    Column("applied", Integer, nullable=False, server_default="1"),
    Column("ignore_reason", String, nullable=True),  # duplicate | late_superseded
    Column("carried_from_version_id", String, nullable=True),
    Index("ix_receipts_device_seq", "device_id", "client_seq", unique=True),
)

disputes = Table(
    "disputes",
    metadata,
    Column("dispute_id", String, primary_key=True),
    Column("target_id", String, ForeignKey("recall_targets.target_id"), nullable=False),
    Column("version_id", String, nullable=False),
    Column("raised_by", String, nullable=False),
    Column("reason", Text, nullable=False),
    Column("status", String, nullable=False),  # pending | upheld | rejected
    Column("reviewed_by", String, nullable=True),
    Column("reviewed_at", String, nullable=True),
    Column("review_note", Text, nullable=True),
    Column("created_at", String, nullable=False),
)

# 异议成立后的人工改派（转交）或摘除（排除）。
reassignments = Table(
    "reassignments",
    metadata,
    Column("reassignment_id", String, primary_key=True),
    Column("target_id", String, ForeignKey("recall_targets.target_id"), nullable=False),
    Column("version_id", String, nullable=False),
    Column("dispute_id", String, ForeignKey("disputes.dispute_id"), nullable=False),
    Column("from_entity_id", String, nullable=True),
    Column("to_entity_id", String, nullable=True),
    Column("kind", String, nullable=False),  # transfer | exclude
    Column("note", Text, nullable=False),
    Column("created_at", String, nullable=False),
)

# 逾期升级记录。due_at 在建标时按召回生效时刻绝对计算，停机多久都不会重置。
escalations = Table(
    "escalations",
    metadata,
    Column("escalation_id", String, primary_key=True),
    Column("target_id", String, ForeignKey("recall_targets.target_id"), nullable=False),
    Column("version_id", String, nullable=False),
    Column("level", Integer, nullable=False),
    Column("label", String, nullable=False),
    Column("due_at", String, nullable=False),
    Column("triggered_at", String, nullable=True),
    Column("revoked_at", String, nullable=True),
    Column("notified_entity_id", String, nullable=True),
    # 部分唯一：同一目标每个层级只允许一条"未吊销"排期；改派后吊销旧链、重排新链。
    Index(
        "ix_escalations_target_level", "target_id", "level",
        unique=True, sqlite_where=text("revoked_at is null"),
    ),
)

# 持久化通知箱：尚未发出的通知落库，服务恢复后按 created_at 顺序补发。
notifications = Table(
    "notifications",
    metadata,
    Column("notification_id", String, primary_key=True),
    Column("version_id", String, nullable=False),
    Column("target_id", String, ForeignKey("recall_targets.target_id"), nullable=False),
    Column("recipient_entity_id", String, nullable=True),
    Column("kind", String, nullable=False),
    Column("payload", Text, nullable=False),
    Column("status", String, nullable=False),  # queued | sent
    Column("created_at", String, nullable=False),
    Column("sent_at", String, nullable=True),
    Column("dedupe_key", String, nullable=False),
    Index("ix_notifications_dedupe", "dedupe_key", unique=True),
)
