"""与迁移 002 对齐的 SQLAlchemy Core 表定义。

DDL 归 Alembic 管理；此处仅用于服务层构造 SQL，避免在业务代码中散落裸表名。
"""

from sqlalchemy import (
    CheckConstraint,
    Column,
    ForeignKey,
    Integer,
    MetaData,
    String,
    Table,
    UniqueConstraint,
)

metadata = MetaData()

brands = Table(
    "brands",
    metadata,
    Column("brand_id", String, primary_key=True),
    Column("name", String, nullable=False),
)

operators = Table(
    "operators",
    metadata,
    Column("operator_id", String, primary_key=True),
    Column("brand_id", String, ForeignKey("brands.brand_id"), nullable=False),
    Column("name", String, nullable=False),
)

actors = Table(
    "actors",
    metadata,
    Column("actor_id", String, primary_key=True),
    Column("brand_id", String, ForeignKey("brands.brand_id"), nullable=True),
    Column("name", String, nullable=False),
    Column("role", String, nullable=False),
    Column("operator_id", String, ForeignKey("operators.operator_id"), nullable=True),
    Column("store_id", String, nullable=True),
    Column("entry_id", String, nullable=True),
)

stores = Table(
    "stores",
    metadata,
    Column("store_id", String, primary_key=True),
    Column("brand_id", String, ForeignKey("brands.brand_id"), nullable=False),
    Column("name", String, nullable=False),
)

online_entries = Table(
    "online_entries",
    metadata,
    Column("entry_id", String, primary_key=True),
    Column("brand_id", String, ForeignKey("brands.brand_id"), nullable=False),
    Column("host_store_id", String, ForeignKey("stores.store_id"), nullable=True),
    Column("name", String, nullable=False),
)

outlet_relations = Table(
    "outlet_relations",
    metadata,
    Column("relation_id", String, primary_key=True),
    Column("target_type", String, nullable=False),
    Column("target_id", String, nullable=False),
    Column("operator_id", String, ForeignKey("operators.operator_id"), nullable=False),
    Column("relation_kind", String, nullable=False),
    Column("valid_from", String, nullable=False),
    Column("valid_to", String, nullable=True),
    Column("changed_by_actor_id", String, nullable=False),
    Column("created_at", String, nullable=False),
    CheckConstraint("valid_to IS NULL OR valid_to > valid_from", name="relation_window_order"),
)

supply_events = Table(
    "supply_events",
    metadata,
    Column("supply_id", String, primary_key=True),
    Column("brand_id", String, ForeignKey("brands.brand_id"), nullable=False),
    Column("product_code", String, nullable=False),
    Column("product_name", String, nullable=False),
    Column("batch_no", String, nullable=False),
    Column("target_type", String, nullable=False),
    Column("target_id", String, nullable=False),
    Column("supplied_at", String, nullable=False),
    Column("created_at", String, nullable=False),
)

recalls = Table(
    "recalls",
    metadata,
    Column("recall_id", String, primary_key=True),
    Column("brand_id", String, ForeignKey("brands.brand_id"), nullable=False),
    Column("created_by_actor_id", String, nullable=False),
    Column("created_at", String, nullable=False),
    Column("current_version_no", Integer, nullable=False, server_default="1"),
)

recall_versions = Table(
    "recall_versions",
    metadata,
    Column("version_id", String, primary_key=True),
    Column("recall_id", String, ForeignKey("recalls.recall_id"), nullable=False),
    Column("version_no", Integer, nullable=False),
    Column("product_code", String, nullable=False),
    Column("product_name", String, nullable=False),
    Column("batch_no", String, nullable=False),
    Column("risk_level", String, nullable=False),
    Column("effective_at", String, nullable=False),
    Column("correction_reason", String, nullable=True),
    Column("created_by_actor_id", String, nullable=False),
    Column("created_at", String, nullable=False),
    Column("frozen_at", String, nullable=False),
    UniqueConstraint("recall_id", "version_no", name="uq_version_no"),
)

recall_targets = Table(
    "recall_targets",
    metadata,
    Column("target_uid", String, primary_key=True),
    Column("version_id", String, ForeignKey("recall_versions.version_id"), nullable=False),
    Column("target_type", String, nullable=False),
    Column("target_id", String, nullable=False),
    Column("status", String, nullable=False),
    Column("reason_code", String, nullable=False),
    Column("reason_detail", String, nullable=False),
    Column("responsible_operator_id", String, nullable=True),
    Column("historical_operator_id", String, nullable=True),
    Column("relation_kind", String, nullable=True),
    Column("basis_relation_id", String, nullable=True),
    Column("attribution_by_actor_id", String, nullable=True),
    Column("latest_disposition", String, nullable=True),
    Column("latest_disposition_at", String, nullable=True),
    Column("escalated_level", Integer, nullable=False, server_default="0"),
    Column("final_escalated", Integer, nullable=False, server_default="0"),
    Column("manually_reassigned", Integer, nullable=False, server_default="0"),
    Column("due_at", String, nullable=True),
    Column("created_at", String, nullable=False),
    UniqueConstraint("version_id", "target_type", "target_id", name="uq_version_target"),
)

recall_target_supplies = Table(
    "recall_target_supplies",
    metadata,
    Column("target_uid", String, ForeignKey("recall_targets.target_uid"), primary_key=True),
    Column("supply_id", String, ForeignKey("supply_events.supply_id"), primary_key=True),
    Column("supply_at_operator_id", String, nullable=True),
    Column("supply_at_relation_kind", String, nullable=True),
    Column("supply_at_relation_id", String, nullable=True),
)

receipts = Table(
    "receipts",
    metadata,
    Column("receipt_id", String, primary_key=True),
    Column("version_id", String, ForeignKey("recall_versions.version_id"), nullable=False),
    Column("target_type", String, nullable=False),
    Column("target_id", String, nullable=False),
    Column("disposition", String, nullable=False),
    Column("detail", String, nullable=True),
    Column("device_id", String, nullable=False),
    Column("device_journal_no", String, nullable=False),
    Column("actor_id", String, nullable=True),
    Column("submitted_at", String, nullable=False),
    Column("received_at", String, nullable=False),
    Column("superseded", Integer, nullable=False, server_default="0"),
    Column("carried_from_version_id", String, nullable=True),
    UniqueConstraint("device_id", "device_journal_no", name="uq_device_journal"),
)

disputes = Table(
    "disputes",
    metadata,
    Column("dispute_id", String, primary_key=True),
    Column("version_id", String, ForeignKey("recall_versions.version_id"), nullable=False),
    Column("target_type", String, nullable=False),
    Column("target_id", String, nullable=False),
    Column("raised_by_actor_id", String, nullable=False),
    Column("raised_at", String, nullable=False),
    Column("reason", String, nullable=False),
    Column("status", String, nullable=False, server_default="open"),
    Column("reviewer_actor_id", String, nullable=True),
    Column("reviewed_at", String, nullable=True),
    Column("decision", String, nullable=True),
    Column("review_note", String, nullable=True),
)

reassignments = Table(
    "reassignments",
    metadata,
    Column("reassignment_id", String, primary_key=True),
    Column("version_id", String, ForeignKey("recall_versions.version_id"), nullable=False),
    Column("target_type", String, nullable=False),
    Column("target_id", String, nullable=False),
    Column("from_operator_id", String, nullable=True),
    Column("to_operator_id", String, ForeignKey("operators.operator_id"), nullable=False),
    Column("by_actor_id", String, nullable=False),
    Column("reason", String, nullable=False),
    Column("created_at", String, nullable=False),
)

notifications = Table(
    "notifications",
    metadata,
    Column("notification_id", String, primary_key=True),
    Column("version_id", String, ForeignKey("recall_versions.version_id"), nullable=False),
    Column("target_type", String, nullable=False),
    Column("target_id", String, nullable=False),
    Column("level", Integer, nullable=False),
    Column("notice_kind", String, nullable=False),
    Column("recipient_operator_id", String, nullable=True),
    Column("payload", String, nullable=False),
    Column("status", String, nullable=False, server_default="pending"),
    Column("created_at", String, nullable=False),
    Column("due_at", String, nullable=False),
    Column("sent_at", String, nullable=True),
    UniqueConstraint(
        "version_id", "target_type", "target_id", "level", "notice_kind",
        "recipient_operator_id",
        name="uq_notification_level",
    ),
)

escalations = Table(
    "escalations",
    metadata,
    Column("escalation_id", String, primary_key=True),
    Column("version_id", String, nullable=False),
    Column("target_type", String, nullable=False),
    Column("target_id", String, nullable=False),
    Column("from_level", Integer, nullable=False),
    Column("to_level", Integer, nullable=False),
    Column("created_at", String, nullable=False),
    Column("reason", String, nullable=False),
)

target_timeline = Table(
    "target_timeline",
    metadata,
    Column("event_id", String, primary_key=True),
    Column("seq", Integer, nullable=False),
    Column("version_id", String, nullable=False),
    Column("target_type", String, nullable=False),
    Column("target_id", String, nullable=False),
    Column("kind", String, nullable=False),
    Column("actor_id", String, nullable=True),
    Column("created_at", String, nullable=False),
    Column("detail", String, nullable=False),
)
