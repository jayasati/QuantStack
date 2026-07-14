"""Composite indexes for the two hottest read patterns in the app.

Found live (2026-07-14): OpportunityDetectionEngine.scan() against the
real 6-symbol production watchlist took ~6.5s (vs ~0.56s in an isolated
test with a handful of synthetic rows) at feature_store's real production
scale (~561k rows). Root cause: FeatureStore.latest() runs
`WHERE symbol = ? AND timeframe = ? ORDER BY ts DESC LIMIT 5000`, but the
only indexes on feature_store were single-column (symbol) and (ts) --
neither serves this query's leading (symbol, timeframe) equality filter
plus ts ordering, so Postgres falls back to scanning/sorting a large slice
of the symbol's rows on every call. Every intelligence engine's
latest_values() goes through this path.

market_events has the same shape of problem for its own hot pattern --
`WHERE event_type = ? AND source = ? AND data->>'symbol' = ?
AND data->>'timeframe' = ? ORDER BY id DESC LIMIT ?` (regime.py's belief
history/lookup, explain.py's explainability history, candidates.py's
recent(), lifecycle.py's get()/history()) -- with only a single-column
index on event_type, every one of those calls filters `source` and both
JSONB fields with no index assistance at all. market_events is smaller
today (~3.8k rows) than feature_store, but it's an append-only event log
used by nearly every read path in the intelligence/prediction layers, so
this only gets worse as it grows.

Revision ID: 0004
Revises: 0003
Create Date: 2026-07-14

"""
from collections.abc import Sequence

from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "ix_feature_store_symbol_timeframe_ts",
        "feature_store",
        ["symbol", "timeframe", "ts"],
    )
    op.execute(
        "CREATE INDEX ix_market_events_lookup ON market_events "
        "(event_type, source, (data->>'symbol'), (data->>'timeframe'), id)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_market_events_lookup")
    op.drop_index("ix_feature_store_symbol_timeframe_ts", table_name="feature_store")
