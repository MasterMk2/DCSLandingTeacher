"""Add import_jobs table for durable import job history (Issue #28).

Previously import jobs lived only in ``ImportJobManager._jobs`` (a process-local
dict), so every server restart wiped job metadata and
``GET /api/imports/{id}`` returned 404 for completed jobs. This table persists
each job's status, progress and outcome so the history survives restarts.
"""

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "0007_import_jobs"
down_revision = "0006_reconcile_schema"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "import_jobs",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("filename", sa.String(length=256), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("progress_percent", sa.Integer(), nullable=True),
        sa.Column("frames_processed", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("total_frames", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("landings_detected", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("duplicates_skipped", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error", sa.String(length=1024), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    op.drop_table("import_jobs")
