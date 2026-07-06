"""Volume 3 — feature store, registry, and metadata tables (Chapter 8).

feature_store and feature_versions were created as thin stubs in 0001; this
revision gives them their real shape and adds the five remaining feature
metadata tables. Both stub tables are still empty at this point, so the new
NOT NULL columns need no backfill.

Revision ID: 0003
Revises: 0002
Create Date: 2026-07-06

"""
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _base_columns() -> list[sa.Column]:
    return [
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("data", JSONB(), nullable=True),
    ]


def upgrade() -> None:
    # --- feature_store: from stub to per-observation storage ------------------
    op.add_column("feature_store", sa.Column("symbol", sa.String(50), nullable=False))
    op.add_column("feature_store", sa.Column("timeframe", sa.String(10), nullable=False))
    op.add_column("feature_store", sa.Column("ts", sa.DateTime(timezone=True), nullable=False))
    op.add_column("feature_store", sa.Column("value", sa.Float(), nullable=False))
    op.add_column("feature_store", sa.Column("window_size", sa.Integer(), nullable=True))
    op.create_index("ix_feature_store_symbol", "feature_store", ["symbol"])
    op.create_index("ix_feature_store_ts", "feature_store", ["ts"])
    op.create_unique_constraint(
        "uq_feature_store_identity",
        "feature_store",
        ["feature_name", "feature_version", "symbol", "timeframe", "ts"],
    )

    # --- feature_versions: one row per published version ----------------------
    op.add_column("feature_versions", sa.Column("description", sa.String(500), nullable=True))
    op.create_unique_constraint(
        "uq_feature_versions_identity", "feature_versions", ["feature_name", "version"]
    )

    # --- feature_registry ------------------------------------------------------
    op.create_table(
        "feature_registry",
        *_base_columns(),
        sa.Column("feature_name", sa.String(200), nullable=False),
        sa.Column("category", sa.String(50), nullable=False),
        sa.Column("description", sa.String(500), nullable=False),
        sa.Column("version", sa.String(20), nullable=False),
        sa.Column("calculation_frequency", sa.String(50), nullable=False),
        sa.Column("owner", sa.String(100), nullable=False),
        sa.Column("quality_threshold", sa.Float(), nullable=False),
        sa.Column("unit", sa.String(50), nullable=False),
        sa.Column("expected_min", sa.Float(), nullable=True),
        sa.Column("expected_max", sa.Float(), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.UniqueConstraint("feature_name", name="uq_feature_registry_feature_name"),
    )
    op.create_index("ix_feature_registry_category", "feature_registry", ["category"])

    # --- feature_dependencies ---------------------------------------------------
    op.create_table(
        "feature_dependencies",
        *_base_columns(),
        sa.Column("feature_name", sa.String(200), nullable=False),
        sa.Column("depends_on", sa.String(200), nullable=False),
        sa.UniqueConstraint("feature_name", "depends_on", name="uq_feature_dependencies_edge"),
    )
    op.create_index(
        "ix_feature_dependencies_feature_name", "feature_dependencies", ["feature_name"]
    )
    op.create_index("ix_feature_dependencies_depends_on", "feature_dependencies", ["depends_on"])

    # --- feature_quality ---------------------------------------------------------
    op.create_table(
        "feature_quality",
        *_base_columns(),
        sa.Column("feature_name", sa.String(200), nullable=False),
        sa.Column("symbol", sa.String(50), nullable=True),
        sa.Column("timeframe", sa.String(10), nullable=True),
        sa.Column("quality_score", sa.Float(), nullable=False),
        sa.Column("sample_count", sa.Integer(), nullable=False),
    )
    op.create_index("ix_feature_quality_feature_name", "feature_quality", ["feature_name"])

    # --- feature_statistics -------------------------------------------------------
    op.create_table(
        "feature_statistics",
        *_base_columns(),
        sa.Column("feature_name", sa.String(200), nullable=False),
        sa.Column("symbol", sa.String(50), nullable=True),
        sa.Column("timeframe", sa.String(10), nullable=True),
        sa.Column("mean", sa.Float(), nullable=True),
        sa.Column("std", sa.Float(), nullable=True),
        sa.Column("min_value", sa.Float(), nullable=True),
        sa.Column("max_value", sa.Float(), nullable=True),
        sa.Column("sample_count", sa.Integer(), nullable=False),
    )
    op.create_index("ix_feature_statistics_feature_name", "feature_statistics", ["feature_name"])

    # --- feature_drift -------------------------------------------------------------
    op.create_table(
        "feature_drift",
        *_base_columns(),
        sa.Column("feature_name", sa.String(200), nullable=False),
        sa.Column("metric", sa.String(50), nullable=False),
        sa.Column("value", sa.Float(), nullable=False),
        sa.Column("threshold", sa.Float(), nullable=False),
        sa.Column("breached", sa.Boolean(), nullable=False),
    )
    op.create_index("ix_feature_drift_feature_name", "feature_drift", ["feature_name"])

    # --- feature_usage ---------------------------------------------------------------
    op.create_table(
        "feature_usage",
        *_base_columns(),
        sa.Column("feature_name", sa.String(200), nullable=False),
        sa.Column("consumer", sa.String(100), nullable=False),
        sa.UniqueConstraint("feature_name", "consumer", name="uq_feature_usage_edge"),
    )
    op.create_index("ix_feature_usage_feature_name", "feature_usage", ["feature_name"])


def downgrade() -> None:
    op.drop_table("feature_usage")
    op.drop_table("feature_drift")
    op.drop_table("feature_statistics")
    op.drop_table("feature_quality")
    op.drop_table("feature_dependencies")
    op.drop_table("feature_registry")

    op.drop_constraint("uq_feature_versions_identity", "feature_versions", type_="unique")
    op.drop_column("feature_versions", "description")

    op.drop_constraint("uq_feature_store_identity", "feature_store", type_="unique")
    op.drop_index("ix_feature_store_ts", table_name="feature_store")
    op.drop_index("ix_feature_store_symbol", table_name="feature_store")
    op.drop_column("feature_store", "window_size")
    op.drop_column("feature_store", "value")
    op.drop_column("feature_store", "ts")
    op.drop_column("feature_store", "timeframe")
    op.drop_column("feature_store", "symbol")
