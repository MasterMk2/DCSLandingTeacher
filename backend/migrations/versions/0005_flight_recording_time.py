"""Add recording_time to flights so imports can tell sessions apart."""

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "0005_flight_recording_time"
down_revision = "0004_approach_pattern"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ACMI ヘッダの RecordingTime。ReferenceTime (= .miz のゲーム内日付) は
    # 同じミッションの全セッションで同一なので、セッションの識別に使えない。
    op.add_column(
        "flights",
        sa.Column("recording_time", sa.String(64), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("flights", "recording_time")
