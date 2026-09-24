"""召回双时点身份关系、处置与升级表。"""

from alembic import op
import sqlalchemy as sa

revision = "002_recall"
down_revision = "001_foundation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "brands",
        sa.Column("brand_id", sa.String(), primary_key=True),
        sa.Column("name", sa.String(), nullable=False),
    )
    op.create_table(
        "operators",
        sa.Column("operator_id", sa.String(), primary_key=True),
        sa.Column("brand_id", sa.String(), sa.ForeignKey("brands.brand_id"), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
    )
    op.create_table(
        "actors",
        sa.Column("actor_id", sa.String(), primary_key=True),
        sa.Column("brand_id", sa.String(), sa.ForeignKey("brands.brand_id"), nullable=True),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("role", sa.String(), nullable=False),
        sa.Column("operator_id", sa.String(), sa.ForeignKey("operators.operator_id"), nullable=True),
        sa.Column("store_id", sa.String(), nullable=True),
        sa.Column("entry_id", sa.String(), nullable=True),
    )
    op.create_table(
        "stores",
        sa.Column("store_id", sa.String(), primary_key=True),
        sa.Column("brand_id", sa.String(), sa.ForeignKey("brands.brand_id"), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
    )
    op.create_table(
        "online_entries",
        sa.Column("entry_id", sa.String(), primary_key=True),
        sa.Column("brand_id", sa.String(), sa.ForeignKey("brands.brand_id"), nullable=False),
        sa.Column("host_store_id", sa.String(), sa.ForeignKey("stores.store_id"), nullable=True),
        sa.Column("name", sa.String(), nullable=False),
    )
    # 经营关系按版本时间段记录：许可证持有、加盟、直营等；valid_to 为空表示至今。
    op.create_table(
        "outlet_relations",
        sa.Column("relation_id", sa.String(), primary_key=True),
        sa.Column("target_type", sa.String(), nullable=False),
        sa.Column("target_id", sa.String(), nullable=False),
        sa.Column("operator_id", sa.String(), sa.ForeignKey("operators.operator_id"), nullable=False),
        sa.Column("relation_kind", sa.String(), nullable=False),
        sa.Column("valid_from", sa.String(), nullable=False),
        sa.Column("valid_to", sa.String(), nullable=True),
        sa.Column("changed_by_actor_id", sa.String(), nullable=False),
        sa.Column("created_at", sa.String(), nullable=False),
        sa.CheckConstraint("valid_to IS NULL OR valid_to > valid_from", name="relation_window_order"),
    )
    op.create_index("ix_relation_target", "outlet_relations", ["target_type", "target_id"])
    op.create_index("ix_relation_operator", "outlet_relations", ["operator_id"])

    # 供货事件：药品在某一时刻进入某门店/线上入口；快照批号，定责在召回时按时间重放。
    op.create_table(
        "supply_events",
        sa.Column("supply_id", sa.String(), primary_key=True),
        sa.Column("brand_id", sa.String(), sa.ForeignKey("brands.brand_id"), nullable=False),
        sa.Column("product_code", sa.String(), nullable=False),
        sa.Column("product_name", sa.String(), nullable=False),
        sa.Column("batch_no", sa.String(), nullable=False),
        sa.Column("target_type", sa.String(), nullable=False),
        sa.Column("target_id", sa.String(), nullable=False),
        sa.Column("supplied_at", sa.String(), nullable=False),
        sa.Column("created_at", sa.String(), nullable=False),
    )
    op.create_index("ix_supply_batch", "supply_events", ["brand_id", "product_code", "batch_no"])

    op.create_table(
        "recalls",
        sa.Column("recall_id", sa.String(), primary_key=True),
        sa.Column("brand_id", sa.String(), sa.ForeignKey("brands.brand_id"), nullable=False),
        sa.Column("created_by_actor_id", sa.String(), nullable=False),
        sa.Column("created_at", sa.String(), nullable=False),
        sa.Column("current_version_no", sa.Integer(), nullable=False, server_default="1"),
    )
    # 召回版本：批号纠错开启新版本，旧版本冻结保留；产品/批号/风险/生效时刻均在版本上。
    op.create_table(
        "recall_versions",
        sa.Column("version_id", sa.String(), primary_key=True),
        sa.Column("recall_id", sa.String(), sa.ForeignKey("recalls.recall_id"), nullable=False),
        sa.Column("version_no", sa.Integer(), nullable=False),
        sa.Column("product_code", sa.String(), nullable=False),
        sa.Column("product_name", sa.String(), nullable=False),
        sa.Column("batch_no", sa.String(), nullable=False),
        sa.Column("risk_level", sa.String(), nullable=False),
        sa.Column("effective_at", sa.String(), nullable=False),
        sa.Column("correction_reason", sa.String(), nullable=True),
        sa.Column("created_by_actor_id", sa.String(), nullable=False),
        sa.Column("created_at", sa.String(), nullable=False),
        sa.Column("frozen_at", sa.String(), nullable=False),
        sa.UniqueConstraint("recall_id", "version_no", name="uq_version_no"),
    )

    # 双时点解析结果：每个入口的纳入/转交/历史/排除/待定在版本创建时冻结。
    op.create_table(
        "recall_targets",
        sa.Column("target_uid", sa.String(), primary_key=True),
        sa.Column("version_id", sa.String(), sa.ForeignKey("recall_versions.version_id"), nullable=False),
        sa.Column("target_type", sa.String(), nullable=False),
        sa.Column("target_id", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("reason_code", sa.String(), nullable=False),
        sa.Column("reason_detail", sa.String(), nullable=False),
        sa.Column("responsible_operator_id", sa.String(), nullable=True),
        sa.Column("historical_operator_id", sa.String(), nullable=True),
        sa.Column("relation_kind", sa.String(), nullable=True),
        sa.Column("basis_relation_id", sa.String(), nullable=True),
        sa.Column("attribution_by_actor_id", sa.String(), nullable=True),
        sa.Column("latest_disposition", sa.String(), nullable=True),
        sa.Column("latest_disposition_at", sa.String(), nullable=True),
        sa.Column("escalated_level", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("final_escalated", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("manually_reassigned", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("due_at", sa.String(), nullable=True),
        sa.Column("created_at", sa.String(), nullable=False),
        sa.UniqueConstraint("version_id", "target_type", "target_id", name="uq_version_target"),
    )
    op.create_index("ix_target_version", "recall_targets", ["version_id"])
    op.create_index("ix_target_outlet", "recall_targets", ["target_type", "target_id"])

    # 入口与供货事件的多对多冻结：逐条保留供货当日的关系事实。
    op.create_table(
        "recall_target_supplies",
        sa.Column(
            "target_uid", sa.String(), sa.ForeignKey("recall_targets.target_uid"), primary_key=True
        ),
        sa.Column("supply_id", sa.String(), sa.ForeignKey("supply_events.supply_id"), primary_key=True),
        sa.Column("supply_at_operator_id", sa.String(), nullable=True),
        sa.Column("supply_at_relation_kind", sa.String(), nullable=True),
        sa.Column("supply_at_relation_id", sa.String(), nullable=True),
    )

    op.create_table(
        "receipts",
        sa.Column("receipt_id", sa.String(), primary_key=True),
        sa.Column("version_id", sa.String(), sa.ForeignKey("recall_versions.version_id"), nullable=False),
        sa.Column("target_type", sa.String(), nullable=False),
        sa.Column("target_id", sa.String(), nullable=False),
        sa.Column("disposition", sa.String(), nullable=False),
        sa.Column("detail", sa.String(), nullable=True),
        sa.Column("device_id", sa.String(), nullable=False),
        sa.Column("device_journal_no", sa.String(), nullable=False),
        sa.Column("actor_id", sa.String(), nullable=True),
        sa.Column("submitted_at", sa.String(), nullable=False),
        sa.Column("received_at", sa.String(), nullable=False),
        sa.Column("superseded", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("carried_from_version_id", sa.String(), nullable=True),
        sa.UniqueConstraint("device_id", "device_journal_no", name="uq_device_journal"),
    )
    op.create_index("ix_receipt_version_target", "receipts", ["version_id", "target_type", "target_id"])

    op.create_table(
        "disputes",
        sa.Column("dispute_id", sa.String(), primary_key=True),
        sa.Column("version_id", sa.String(), sa.ForeignKey("recall_versions.version_id"), nullable=False),
        sa.Column("target_type", sa.String(), nullable=False),
        sa.Column("target_id", sa.String(), nullable=False),
        sa.Column("raised_by_actor_id", sa.String(), nullable=False),
        sa.Column("raised_at", sa.String(), nullable=False),
        sa.Column("reason", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False, server_default="open"),
        sa.Column("reviewer_actor_id", sa.String(), nullable=True),
        sa.Column("reviewed_at", sa.String(), nullable=True),
        sa.Column("decision", sa.String(), nullable=True),
        sa.Column("review_note", sa.String(), nullable=True),
    )
    op.create_index("ix_dispute_version_target", "disputes", ["version_id", "target_type", "target_id"])

    op.create_table(
        "reassignments",
        sa.Column("reassignment_id", sa.String(), primary_key=True),
        sa.Column("version_id", sa.String(), sa.ForeignKey("recall_versions.version_id"), nullable=False),
        sa.Column("target_type", sa.String(), nullable=False),
        sa.Column("target_id", sa.String(), nullable=False),
        sa.Column("from_operator_id", sa.String(), nullable=True),
        sa.Column("to_operator_id", sa.String(), sa.ForeignKey("operators.operator_id"), nullable=False),
        sa.Column("by_actor_id", sa.String(), nullable=False),
        sa.Column("reason", sa.String(), nullable=False),
        sa.Column("created_at", sa.String(), nullable=False),
    )

    # 通知外发箱：停机期间保持 pending，恢复后续发；(版本,入口,层级,类型,收件人) 幂等。
    op.create_table(
        "notifications",
        sa.Column("notification_id", sa.String(), primary_key=True),
        sa.Column("version_id", sa.String(), sa.ForeignKey("recall_versions.version_id"), nullable=False),
        sa.Column("target_type", sa.String(), nullable=False),
        sa.Column("target_id", sa.String(), nullable=False),
        sa.Column("level", sa.Integer(), nullable=False),
        sa.Column("notice_kind", sa.String(), nullable=False),
        sa.Column("recipient_operator_id", sa.String(), nullable=True),
        sa.Column("payload", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False, server_default="pending"),
        sa.Column("created_at", sa.String(), nullable=False),
        sa.Column("due_at", sa.String(), nullable=False),
        sa.Column("sent_at", sa.String(), nullable=True),
        sa.UniqueConstraint(
            "version_id", "target_type", "target_id", "level", "notice_kind",
            "recipient_operator_id",
            name="uq_notification_level",
        ),
    )
    op.create_index("ix_notification_status", "notifications", ["status", "due_at"])

    op.create_table(
        "escalations",
        sa.Column("escalation_id", sa.String(), primary_key=True),
        sa.Column("version_id", sa.String(), nullable=False),
        sa.Column("target_type", sa.String(), nullable=False),
        sa.Column("target_id", sa.String(), nullable=False),
        sa.Column("from_level", sa.Integer(), nullable=False),
        sa.Column("to_level", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.String(), nullable=False),
        sa.Column("reason", sa.String(), nullable=False),
    )

    op.create_table(
        "target_timeline",
        sa.Column("event_id", sa.String(), primary_key=True),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("version_id", sa.String(), nullable=False),
        sa.Column("target_type", sa.String(), nullable=False),
        sa.Column("target_id", sa.String(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("actor_id", sa.String(), nullable=True),
        sa.Column("created_at", sa.String(), nullable=False),
        sa.Column("detail", sa.String(), nullable=False),
    )
    op.create_index(
        "ix_timeline_version_target", "target_timeline", ["version_id", "target_type", "target_id"]
    )
    op.create_index("ix_timeline_seq", "target_timeline", ["seq"], unique=True)


def downgrade() -> None:
    for table in (
        "target_timeline",
        "escalations",
        "notifications",
        "reassignments",
        "disputes",
        "receipts",
        "recall_target_supplies",
        "recall_targets",
        "recall_versions",
        "recalls",
        "supply_events",
        "outlet_relations",
        "online_entries",
        "stores",
        "actors",
        "operators",
        "brands",
    ):
        op.drop_table(table)
