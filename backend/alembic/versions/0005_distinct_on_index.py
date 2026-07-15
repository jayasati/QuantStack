"""Index to serve FeatureStore.latest()'s DISTINCT ON (feature_name) query.

perf-audit-2026-07-14 finding 6: `latest()` fetched up to 5000 rows and kept
only the first-per-feature-name in Python (~97% waste). Rewritten to
`DISTINCT ON (feature_name) ... WHERE symbol = ? AND timeframe = ? ORDER BY
feature_name, ts DESC` -- the existing (symbol, timeframe, ts) index from
0004 doesn't have feature_name as a leading/sort column, so Postgres still
has to sort matching rows by feature_name itself. This index puts
(symbol, timeframe, feature_name, ts DESC) in the query's actual scan order.

Revision ID: 0005
Revises: 0004
Create Date: 2026-07-14

"""
from collections.abc import Sequence

from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "CREATE INDEX ix_feature_store_symbol_timeframe_feature_ts ON feature_store "
        "(symbol, timeframe, feature_name, ts DESC)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_feature_store_symbol_timeframe_feature_ts")
