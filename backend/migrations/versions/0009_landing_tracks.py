"""Move each landing's approach track out of ``landings``, compressed.

A few hundred KB of JSON sat in the middle of every ``landings`` row, and
SQLite reaches a column that comes after a big value by walking the row's
overflow pages. The list sorts and filters on ``created_at``, ``source_id``,
``pilot`` and ``airframe`` -- all after ``approach_track`` -- so every list
request read every track. Measured on 1,723 synthetic tracks of ~300 KB: the
count and page queries took 320 ms and 960 ms; with the tracks in their own
table, 1.5 ms and 1 ms. zlib shrinks them about sevenfold on the way.

Written to be run again from any point it stopped at: SQLite runs DDL
non-transactionally under alembic (see 0008), and a deploy that gives up
waiting for the health check can kill it half way. Each step checks whether
it is already done, and the copy commits batch by batch (an autocommit
block) and only takes rows not yet copied -- left to alembic's transaction,
the first INSERT opened one that ran to the version stamp, so a copy killed
at 90 % started from zero every time and a deploy with a shorter wait than
the whole copy could never finish it.

Cost measured locally on the synthetic set: 15 s for the copy, 1 s for the
drop. The file keeps its size until it is rebuilt (``VACUUM``, or
``python -m app.compact``, which writes a new file).
"""

import zlib

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "0009_landing_tracks"
down_revision = "0008_landing_identity"
branch_labels = None
depends_on = None

TRIGGER = (
    "CREATE TRIGGER IF NOT EXISTS landings_delete_track AFTER DELETE ON landings "
    "BEGIN DELETE FROM landing_tracks WHERE landing_id = OLD.id; END"
)

#: Rows per round trip of the copy; a track is up to ~500 KB of JSON.
BATCH = 100


def _landing_columns(bind) -> set[str]:
    return {row[1] for row in bind.execute(sa.text("PRAGMA table_info(landings)"))}


def upgrade() -> None:
    bind = op.get_bind()
    if not sa.inspect(bind).has_table("landing_tracks"):
        op.create_table(
            "landing_tracks",
            sa.Column(
                "landing_id",
                sa.Integer(),
                sa.ForeignKey("landings.id", ondelete="CASCADE"),
                primary_key=True,
            ),
            sa.Column("approach_track", sa.LargeBinary(), nullable=True),
        )
    bind.execute(sa.text(TRIGGER))

    if "approach_track" not in _landing_columns(bind):
        return
    # The stored text is compressed as it is, not re-serialised: the JSON a
    # track decompresses to is byte for byte the JSON it was.
    with op.get_context().autocommit_block():
        last_id = 0
        while True:
            rows = bind.execute(
                sa.text(
                    "SELECT l.id, l.approach_track FROM landings AS l "
                    "WHERE l.id > :last AND l.approach_track IS NOT NULL "
                    "AND NOT EXISTS "
                    "(SELECT 1 FROM landing_tracks AS t WHERE t.landing_id = l.id) "
                    "ORDER BY l.id LIMIT :batch"
                ),
                {"last": last_id, "batch": BATCH},
            ).all()
            if not rows:
                break
            bind.execute(
                sa.text(
                    "INSERT INTO landing_tracks (landing_id, approach_track) VALUES (:id, :blob)"
                ),
                [
                    {"id": landing_id, "blob": zlib.compress(str(text).encode("utf-8"), 6)}
                    for landing_id, text in rows
                ],
            )
            last_id = rows[-1][0]
    # Dropping the column frees every page the tracks overflowed into. A
    # SQLite built with SECURE_DELETE (Debian's may be) zeroes each of them
    # and the zeroes go through the WAL: ~the size of the tracks again, on
    # the same disk, measured 90 MB of WAL against 24 MB without on a 67 MB
    # test database. Nothing needs erasing -- the data was just copied -- so
    # not on this connection.
    bind.execute(sa.text("PRAGMA secure_delete = OFF"))
    op.drop_column("landings", "approach_track")


def downgrade() -> None:
    bind = op.get_bind()
    if "approach_track" not in _landing_columns(bind):
        op.add_column("landings", sa.Column("approach_track", sa.JSON(), nullable=True))
    rows = bind.execute(sa.text("SELECT landing_id, approach_track FROM landing_tracks")).all()
    for landing_id, blob in rows:
        if blob is None:
            continue
        bind.execute(
            sa.text("UPDATE landings SET approach_track = :text WHERE id = :id"),
            {"id": landing_id, "text": zlib.decompress(blob).decode("utf-8")},
        )
    bind.execute(sa.text("DROP TRIGGER IF EXISTS landings_delete_track"))
    op.drop_table("landing_tracks")
