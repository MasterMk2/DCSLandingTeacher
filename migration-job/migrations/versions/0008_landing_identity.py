"""Store pilot/airframe on the landing, and backfill from the approach track.

A landing's aircraft was read from the ``objects`` row, which is mutable and
shared: Tacview reuses an object's hex id within a recording and the ingest
matches on (flight_id, acmi_id), so a later object overwrites the name and
pilot of the row an earlier landing points at. Measured before this
migration: 124 landings displayed an airframe disagreeing with the one in
their own ``approach_track``, including 7 UH-1H landings shown as "AIM_120".

``approach_track.airframe`` is the value captured at detection time, so it is
the correct one and the backfill uses it.

The pilot is NOT backfilled, and the reason is worth stating precisely
because an earlier version of this note got it wrong. The names are not lost:
they were recorded at ingest in ``objects.pilot``, and 476 of the 497 rows
with a null ``landings.pilot`` still join to one (measured 2026-09-06). What
is missing is a DETECTION-TIME copy. Filling the column from the object row
would look like a repair while actually asserting, as this landing's own
recorded fact, a value taken from the mutable row the column exists to stop
trusting -- and roughly 130 of those rows are ones whose object identity is
already known to have been overwritten. Reading through the fallback in
``routes._summary`` shows the same names today without making that claim, so
the column stays null for old rows and only new landings burn theirs in.
"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = '0008_landing_identity'
down_revision = '0007_import_jobs'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('landings', sa.Column('pilot', sa.String(128), nullable=True))
    op.add_column('landings', sa.Column('airframe', sa.String(128), nullable=True))

    # The approach track captures the airframe at detection time. PostgreSQL's
    # JSON operator retrieves that stored value without consulting mutable
    # object metadata.
    op.execute(
        """
        UPDATE landings
           SET airframe = approach_track ->> 'airframe'
         WHERE approach_track IS NOT NULL
           AND approach_track ->> 'airframe' IS NOT NULL
        """
    )


def downgrade() -> None:
    op.drop_column('landings', 'airframe')
    op.drop_column('landings', 'pilot')
