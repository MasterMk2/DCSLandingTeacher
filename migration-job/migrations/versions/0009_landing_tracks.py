"""Move stored approach tracks into a compressed table on PostgreSQL."""

from __future__ import annotations

import json
import zlib

import sqlalchemy as sa
from alembic import op

revision = '0009_landing_tracks'
down_revision = '0008_landing_identity'
branch_labels = None
depends_on = None

BATCH_SIZE = 100


def upgrade() -> None:
    op.create_table(
        'landing_tracks',
        sa.Column('landing_id', sa.Integer(), sa.ForeignKey('landings.id', ondelete='CASCADE'), primary_key=True),
        sa.Column('approach_track', sa.LargeBinary(), nullable=True),
    )

    connection = op.get_bind()
    insert = sa.text(
        'INSERT INTO landing_tracks (landing_id, approach_track) VALUES (:id, :payload)'
    ).bindparams(sa.bindparam('payload', type_=sa.LargeBinary()))
    last_id = 0
    while True:
        rows = connection.execute(
            sa.text(
                'SELECT id, approach_track::text FROM landings '
                'WHERE id > :last_id AND approach_track IS NOT NULL '
                'ORDER BY id LIMIT :batch_size'
            ),
            {'last_id': last_id, 'batch_size': BATCH_SIZE},
        ).all()
        if not rows:
            break
        connection.execute(
            insert,
            [
                {
                    'id': landing_id,
                    'payload': zlib.compress(track.encode('utf-8'), 6),
                }
                for landing_id, track in rows
            ],
        )
        last_id = rows[-1][0]

    op.drop_column('landings', 'approach_track')


def downgrade() -> None:
    op.add_column('landings', sa.Column('approach_track', sa.JSON(), nullable=True))

    connection = op.get_bind()
    update = sa.text(
        'UPDATE landings SET approach_track = :track WHERE id = :id'
    ).bindparams(sa.bindparam('track', type_=sa.JSON()))
    last_id = 0
    while True:
        rows = connection.execute(
            sa.text(
                'SELECT landing_id, approach_track FROM landing_tracks '
                'WHERE landing_id > :last_id ORDER BY landing_id LIMIT :batch_size'
            ),
            {'last_id': last_id, 'batch_size': BATCH_SIZE},
        ).all()
        if not rows:
            break
        restored = [
            {'id': landing_id, 'track': json.loads(zlib.decompress(payload))}
            for landing_id, payload in rows
            if payload is not None
        ]
        if restored:
            connection.execute(update, restored)
        last_id = rows[-1][0]

    op.drop_table('landing_tracks')
