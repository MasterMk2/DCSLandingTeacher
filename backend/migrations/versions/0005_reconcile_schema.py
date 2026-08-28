"""Reconcile schema with the ORM models (Issue #24).

The ORM models declare ``flights.source_id`` and ``landings.source_id`` as
non-nullable (with a ``default="default"``), and ``objects.flight_id`` carries
an implicit ``index=True``. The earlier migrations left ``source_id`` nullable
and never created ``ix_objects_flight_id``, so a freshly migrated database did
not match ``Base.metadata``. This revision brings the database into line with
the models so ``alembic check`` is clean.
"""

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "0005_reconcile_schema"
down_revision = "0004_approach_pattern"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. Backfill any NULL source_id rows, then enforce NOT NULL to match the
    #    model (which always provides a "default" value via ORM defaults).
    op.execute("UPDATE flights SET source_id = 'default' WHERE source_id IS NULL")
    op.execute("UPDATE landings SET source_id = 'default' WHERE source_id IS NULL")

    with op.batch_alter_table("flights") as batch_op:
        batch_op.alter_column(
            "source_id",
            existing_type=sa.String(length=64),
            nullable=False,
        )
    with op.batch_alter_table("landings") as batch_op:
        batch_op.alter_column(
            "source_id",
            existing_type=sa.String(length=64),
            nullable=False,
        )

    # 2. Create the missing index that the model's index=True on objects.flight_id
    #    expects.
    with op.batch_alter_table("objects") as batch_op:
        batch_op.create_index("ix_objects_flight_id", ["flight_id"], unique=False)


def downgrade() -> None:
    with op.batch_alter_table("objects") as batch_op:
        batch_op.drop_index("ix_objects_flight_id")

    with op.batch_alter_table("landings") as batch_op:
        batch_op.alter_column(
            "source_id",
            existing_type=sa.String(length=64),
            nullable=True,
        )
    with op.batch_alter_table("flights") as batch_op:
        batch_op.alter_column(
            "source_id",
            existing_type=sa.String(length=64),
            nullable=True,
        )
