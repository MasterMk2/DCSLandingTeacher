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
    # SQLite runs DDL non-transactionally under alembic, so the two ADD
    # COLUMNs commit independently of the backfill below and of the version
    # stamp. If anything fails in between, the columns exist but the revision
    # does not, and a re-run would die on "duplicate column name". Adding only
    # what is missing makes the migration safe to repeat from that state.
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing = {column['name'] for column in inspector.get_columns('landings')}
    if 'pilot' not in existing:
        op.add_column('landings', sa.Column('pilot', sa.String(128), nullable=True))
    if 'airframe' not in existing:
        op.add_column('landings', sa.Column('airframe', sa.String(128), nullable=True))

    # Backfill from the approach track, which recorded the airframe at
    # detection time. json_extract is SQLite-specific; this project ships on
    # SQLite only (see docs/architecture.md), and the guard keeps the
    # migration from failing anywhere else rather than pretending to work.
    if bind.dialect.name == 'sqlite':
        bind.execute(
            sa.text(
                """
                UPDATE landings
                   SET airframe = json_extract(approach_track, '$.airframe')
                 WHERE approach_track IS NOT NULL
                   AND json_extract(approach_track, '$.airframe') IS NOT NULL
                """
            )
        )


def downgrade() -> None:
    op.drop_column('landings', 'airframe')
    op.drop_column('landings', 'pilot')
