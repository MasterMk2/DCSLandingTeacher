"""Add source_id to flights and landings for multi-source support."""

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "0003_multi_source_support"
down_revision = "0002_landing_outcome_status"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # flights テーブルに source_id 追加 (index=True で自動的にインデックス作成)
    op.add_column("flights", sa.Column("source_id", sa.String(64), nullable=True, index=True))

    # landings テーブルに source_id 追加 (flights 経由で参照可能だが、クエリ効率化のため直接持つ)
    op.add_column("landings", sa.Column("source_id", sa.String(64), nullable=True, index=True))

    # 既存データの source_id を 'default' で埋める
    op.execute("UPDATE flights SET source_id = 'default' WHERE source_id IS NULL")
    op.execute("UPDATE landings SET source_id = 'default' WHERE source_id IS NULL")


def downgrade() -> None:
    op.drop_index("ix_landings_source_id", table_name="landings")
    op.drop_column("landings", "source_id")
    op.drop_index("ix_flights_source_id", table_name="flights")
    op.drop_column("flights", "source_id")