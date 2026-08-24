"""Baseline schema (pre-Alembic create_all layout).

Revision ID: 0001_baseline
Revises:
Create Date: 2026-08-24

Creates the original Phase-1 schema exactly as ``Base.metadata.create_all``
produced it before migrations were introduced, so databases created by older
versions can be stamped at this revision and migrated forward.
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0001_baseline"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "flights",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("reference_time", sa.String(length=64), nullable=True),
        sa.Column("data_source", sa.String(length=128), nullable=True),
        sa.Column("data_recorder", sa.String(length=128), nullable=True),
        sa.Column("title", sa.String(length=256), nullable=True),
        sa.Column("theater", sa.String(length=128), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "objects",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("flight_id", sa.Integer(), nullable=False),
        sa.Column("acmi_id", sa.String(length=16), nullable=False),
        sa.Column("type", sa.String(length=128), nullable=True),
        sa.Column("name", sa.String(length=128), nullable=True),
        sa.Column("pilot", sa.String(length=128), nullable=True),
        sa.Column("group_name", sa.String(length=128), nullable=True),
        sa.Column("country", sa.String(length=8), nullable=True),
        sa.Column("first_seen", sa.Float(), nullable=False),
        sa.Column("last_seen", sa.Float(), nullable=False),
        sa.Column("removed", sa.Boolean(), nullable=False),
        sa.ForeignKeyConstraint(["flight_id"], ["flights.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_objects_flight_acmi_id",
        "objects",
        ["flight_id", "acmi_id"],
        unique=True,
    )
    op.create_table(
        "tracks",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("flight_id", sa.Integer(), nullable=False),
        sa.Column("object_id", sa.Integer(), nullable=False),
        sa.Column("mission_time", sa.Float(), nullable=False),
        sa.Column("latitude", sa.Float(), nullable=True),
        sa.Column("longitude", sa.Float(), nullable=True),
        sa.Column("altitude", sa.Float(), nullable=True),
        sa.Column("u", sa.Float(), nullable=True),
        sa.Column("v", sa.Float(), nullable=True),
        sa.Column("roll", sa.Float(), nullable=True),
        sa.Column("pitch", sa.Float(), nullable=True),
        sa.Column("yaw", sa.Float(), nullable=True),
        sa.Column("heading", sa.Float(), nullable=True),
        sa.Column("speed", sa.Float(), nullable=True),
        sa.Column("on_ground", sa.Boolean(), nullable=True),
        sa.Column("agl", sa.Float(), nullable=True),
        sa.Column("aoa", sa.Float(), nullable=True),
        sa.ForeignKeyConstraint(["flight_id"], ["flights.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["object_id"], ["objects.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_tracks_flight_id", "tracks", ["flight_id"])
    op.create_index("ix_tracks_object_id", "tracks", ["object_id"])
    op.create_table(
        "landings",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("flight_id", sa.Integer(), nullable=False),
        sa.Column("object_id", sa.Integer(), nullable=False),
        sa.Column(
            "carrier_object_id", sa.Integer(), nullable=True
        ),
        sa.Column("kind", sa.String(length=16), nullable=True),
        sa.Column("outcome", sa.String(length=16), nullable=True),
        sa.Column("touchdown_time", sa.Float(), nullable=True),
        sa.Column("venue_name", sa.String(length=128), nullable=True),
        sa.Column("latitude", sa.Float(), nullable=True),
        sa.Column("longitude", sa.Float(), nullable=True),
        sa.Column("altitude", sa.Float(), nullable=True),
        sa.Column("heading", sa.Float(), nullable=True),
        sa.Column("speed", sa.Float(), nullable=True),
        sa.Column("descent_rate", sa.Float(), nullable=True),
        sa.Column("grade", sa.String(length=32), nullable=True),
        sa.Column("score", sa.Float(), nullable=True),
        sa.Column("comment", sa.String(length=1024), nullable=True),
        sa.Column("factors", sa.JSON(), nullable=True),
        sa.Column("metrics", sa.JSON(), nullable=True),
        sa.Column("approach_track", sa.JSON(), nullable=True),
        sa.Column("grading_version", sa.String(length=32), nullable=True),
        sa.Column("graded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["flight_id"], ["flights.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["object_id"], ["objects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["carrier_object_id"], ["objects.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_landings_flight_id", "landings", ["flight_id"])
    op.create_index("ix_landings_object_id", "landings", ["object_id"])


def downgrade() -> None:
    op.drop_table("landings")
    op.drop_table("tracks")
    op.drop_index("ix_objects_flight_acmi_id", table_name="objects")
    op.drop_table("objects")
    op.drop_table("flights")
