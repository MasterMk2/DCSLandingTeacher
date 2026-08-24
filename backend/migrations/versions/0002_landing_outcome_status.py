"""Add landings.outcome_status for two-phase confirmation.

Revision ID: 0002_landing_outcome_status
Revises: 0001_baseline
Create Date: 2026-08-24

Issue #5: a landing detected at touchdown is stored as "provisional" until
its outcome can no longer change, then flipped to "final". Existing rows are
backfilled as "final" via the server default.
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0002_landing_outcome_status"
down_revision: Union[str, None] = "0001_baseline"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("landings") as batch_op:
        batch_op.add_column(
            sa.Column(
                "outcome_status",
                sa.String(length=16),
                nullable=False,
                server_default="final",
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("landings") as batch_op:
        batch_op.drop_column("outcome_status")
