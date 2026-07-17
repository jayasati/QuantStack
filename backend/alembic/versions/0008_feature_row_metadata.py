"""feature_store: add per-row collector_version/last_updated/feature_quality_score.

Data foundation audit (2026-07-17, feature-row metadata item): the mandate's
per-feature metadata contract (timestamp, symbol, feature_version,
collector_version, last_updated, feature_quality_score) was only ~half
satisfied -- `feature_store` had timestamp/symbol/feature_version but not the
other three (feature_quality_score lived in a separate `feature_quality`
table, never stamped onto the observation itself). Nullable, no backfill --
existing rows predate this metadata and stay NULL; every new write from
`FeatureStore._write_offline` populates all three going forward. Purely
additive column adds on an already-large table (`feature_store` holds
170k-360k+ rows per symbol/timeframe per the 2026-07-17 collector audit) --
nullable columns with no server default are a metadata-only ALTER TABLE in
Postgres, no table rewrite, safe at that scale.

Revision ID: 0008
Revises: 0007
Create Date: 2026-07-17

"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("feature_store", sa.Column("collector_version", sa.String(20), nullable=True))
    op.add_column(
        "feature_store", sa.Column("last_updated", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "feature_store", sa.Column("feature_quality_score", sa.Float(), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("feature_store", "feature_quality_score")
    op.drop_column("feature_store", "last_updated")
    op.drop_column("feature_store", "collector_version")
