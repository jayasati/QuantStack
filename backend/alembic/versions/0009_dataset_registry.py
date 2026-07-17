"""dataset_versions: a named, queryable dataset registry.

Data foundation audit (2026-07-17, data-versioning item): the model registry
shipped in migration 0007 records a `data_hash` per trained model, but that
only answers "what hash did THIS model use" -- there was no way to ask "what
datasets exist" or "what's in dataset X" independently of a specific model
row. This adds `dataset_versions` (get-or-create keyed on the unique
`data_hash`, so repeated identical datasets share one row) and links
`model_versions.dataset_version_id` to it -- this codebase's second foreign
key, following migration 0007's precedent. Both new tables/columns are
additive on an empty (`model_versions`, 0 rows as of this session) or
new (`dataset_versions`) table -- no backfill, no risk to existing data.

Revision ID: 0009
Revises: 0008
Create Date: 2026-07-17

"""
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "dataset_versions",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("data", JSONB(), nullable=True),
        sa.Column("name", sa.String(100), nullable=True),
        sa.Column("timeframe", sa.String(10), nullable=True),
        sa.Column("date_range_start", sa.DateTime(timezone=True), nullable=True),
        sa.Column("date_range_end", sa.DateTime(timezone=True), nullable=True),
        sa.Column("row_count", sa.Integer(), nullable=True),
        sa.Column("data_hash", sa.String(64), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_dataset_versions"),
    )
    op.create_index("ix_dataset_versions_name", "dataset_versions", ["name"])
    op.create_unique_constraint(
        "uq_dataset_versions_data_hash", "dataset_versions", ["data_hash"]
    )

    op.add_column("model_versions", sa.Column("dataset_version_id", sa.BigInteger(), nullable=True))
    op.create_foreign_key(
        "fk_model_versions_dataset_version_id_dataset_versions",
        "model_versions", "dataset_versions",
        ["dataset_version_id"], ["id"],
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_model_versions_dataset_version_id_dataset_versions",
        "model_versions", type_="foreignkey",
    )
    op.drop_column("model_versions", "dataset_version_id")
    op.drop_table("dataset_versions")
