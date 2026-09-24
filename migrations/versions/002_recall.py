"""建立身份关系双时间轴与药品召回触达全套表。"""

from alembic import op

from pharmacy_identity.schema import metadata

revision = "002_recall"
down_revision = "001_foundation"
branch_labels = None
depends_on = None

# 001 已建立 service_metadata；本迁移只增删下面这些表，drop_all 会误伤历史表。
_TABLES_002 = [
    "notifications",
    "escalations",
    "reassignments",
    "disputes",
    "receipts",
    "recall_targets",
    "recall_versions",
    "recalls",
    "actors",
    "supplies",
    "entity_relations",
    "entity_licenses",
    "stores",
    "legal_entities",
    "brands",
]


def upgrade() -> None:
    # service_metadata 已由 001 建立，create_all 只补缺表。
    metadata.create_all(bind=op.get_bind())


def downgrade() -> None:
    bind = op.get_bind()
    for name in _TABLES_002:
        table = metadata.tables[name]
        table.drop(bind=bind, checkfirst=True)
