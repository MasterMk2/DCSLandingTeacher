"""Add approach_pattern column to landings table."""

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "0004_approach_pattern"
down_revision = "0003_multi_source_support"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # landings テーブルに approach_pattern 追加
    op.add_column(
        "landings",
        sa.Column("approach_pattern", sa.String(16), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("landings", "approach_pattern")