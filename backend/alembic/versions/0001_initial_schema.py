"""Initial schema — the 20 Volume 1 foundation tables (created empty).

Revision ID: 0001
Revises:
Create Date: 2026-07-05

"""
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision: str = "0001"
down_revision: str | None = None
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


# table name -> (extra columns, indexed column names)
TABLES: dict[str, tuple[list[sa.Column], list[str]]] = {
    "collectors": (
        [
            sa.Column("name", sa.String(100), nullable=False, unique=True),
            sa.Column("version", sa.String(20), nullable=False),
            sa.Column("enabled", sa.Boolean(), nullable=False),
        ],
        [],
    ),
    "collector_health": (
        [
            sa.Column("collector_name", sa.String(100), nullable=False),
            sa.Column("quality_score", sa.Float(), nullable=True),
        ],
        ["collector_name"],
    ),
    "market_events": (
        [
            sa.Column("event_type", sa.String(100), nullable=False),
            sa.Column("source", sa.String(100), nullable=False),
        ],
        ["event_type"],
    ),
    "feature_store": (
        [
            sa.Column("feature_name", sa.String(200), nullable=False),
            sa.Column("feature_version", sa.String(20), nullable=False),
        ],
        ["feature_name"],
    ),
    "feature_versions": (
        [
            sa.Column("feature_name", sa.String(200), nullable=False),
            sa.Column("version", sa.String(20), nullable=False),
        ],
        ["feature_name"],
    ),
    "market_regime": (
        [
            sa.Column("regime", sa.String(50), nullable=False),
            sa.Column("probability", sa.Float(), nullable=True),
        ],
        ["regime"],
    ),
    "regime_weights": ([sa.Column("regime", sa.String(50), nullable=False)], ["regime"]),
    "breadth_metrics": ([], []),
    "sector_rotation": ([sa.Column("sector", sa.String(50), nullable=True)], ["sector"]),
    "relative_strength": ([sa.Column("symbol", sa.String(50), nullable=True)], ["symbol"]),
    "market_structure": ([sa.Column("symbol", sa.String(50), nullable=True)], ["symbol"]),
    "event_risk": ([sa.Column("event_name", sa.String(200), nullable=True)], []),
    "prediction_results": (
        [
            sa.Column("symbol", sa.String(50), nullable=True),
            sa.Column("model_version", sa.String(50), nullable=True),
        ],
        ["symbol"],
    ),
    "signal_quality": ([sa.Column("grade", sa.String(10), nullable=True)], []),
    "trade_signals": (
        [
            sa.Column("symbol", sa.String(50), nullable=True),
            sa.Column("direction", sa.String(10), nullable=True),
        ],
        ["symbol"],
    ),
    "trade_log": (
        [
            sa.Column("signal_id", sa.BigInteger(), nullable=True),
            sa.Column("outcome", sa.String(50), nullable=True),
        ],
        ["signal_id"],
    ),
    "model_versions": (
        [
            sa.Column("model_name", sa.String(100), nullable=True),
            sa.Column("version", sa.String(50), nullable=True),
        ],
        ["model_name"],
    ),
    "retraining_runs": ([sa.Column("status", sa.String(50), nullable=True)], []),
    "system_metrics": (
        [
            sa.Column("metric_name", sa.String(100), nullable=True),
            sa.Column("value", sa.Float(), nullable=True),
        ],
        ["metric_name"],
    ),
    "audit_log": (
        [
            sa.Column("actor", sa.String(100), nullable=True),
            sa.Column("action", sa.String(200), nullable=True),
        ],
        [],
    ),
}


def upgrade() -> None:
    for table_name, (extra_columns, indexed) in TABLES.items():
        op.create_table(table_name, *_base_columns(), *extra_columns)
        for column_name in indexed:
            op.create_index(
                f"ix_{table_name}_{column_name}", table_name, [column_name]
            )


def downgrade() -> None:
    for table_name in reversed(list(TABLES)):
        op.drop_table(table_name)
