"""model_versions/retraining_runs: wire up the model/dataset registry.

Data foundation audit (2026-07-17) found both tables were dead scaffolding
since Volume 1 -- 0 rows, 0 readers, 0 writers, confirmed live on
quantstack-vm (`SELECT count(*) FROM model_versions` = 0,
`retraining_runs` = 0). `EnsemblePredictionEngine.train()` is the first
writer: every train() attempt gets a `retraining_runs` row (successful or
not); a successful one also gets a `model_versions` row carrying the
data_hash + git_commit provenance pair the audit specifically asked for,
linked via `retraining_runs.model_version_id` -- this codebase's first
foreign key (NAMING_CONVENTION has reserved the "fk" naming pattern since
Volume 1 but nothing used it until now). Safe to add real columns/an FK
directly rather than nullable-then-backfill: both tables are confirmed
empty, so there's no existing data to violate any constraint.

Revision ID: 0007
Revises: 0006
Create Date: 2026-07-17

"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("model_versions", sa.Column("symbol", sa.String(50), nullable=True))
    op.add_column("model_versions", sa.Column("timeframe", sa.String(10), nullable=True))
    op.add_column("model_versions", sa.Column("direction", sa.String(10), nullable=True))
    op.add_column(
        "model_versions", sa.Column("trained_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column("model_versions", sa.Column("data_hash", sa.String(64), nullable=True))
    op.add_column("model_versions", sa.Column("git_commit", sa.String(40), nullable=True))
    op.add_column("model_versions", sa.Column("sample_count", sa.Integer(), nullable=True))
    op.add_column("model_versions", sa.Column("holdout_count", sa.Integer(), nullable=True))
    op.add_column("model_versions", sa.Column("status", sa.String(20), nullable=True))
    op.create_index("ix_model_versions_symbol", "model_versions", ["symbol"])

    op.add_column("retraining_runs", sa.Column("model_name", sa.String(100), nullable=True))
    op.add_column("retraining_runs", sa.Column("symbol", sa.String(50), nullable=True))
    op.add_column("retraining_runs", sa.Column("timeframe", sa.String(10), nullable=True))
    op.add_column("retraining_runs", sa.Column("direction", sa.String(10), nullable=True))
    op.add_column("retraining_runs", sa.Column("trigger", sa.String(20), nullable=True))
    op.add_column(
        "retraining_runs", sa.Column("started_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "retraining_runs", sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column("retraining_runs", sa.Column("model_version_id", sa.BigInteger(), nullable=True))
    op.create_index("ix_retraining_runs_model_name", "retraining_runs", ["model_name"])
    op.create_index("ix_retraining_runs_symbol", "retraining_runs", ["symbol"])
    op.create_foreign_key(
        "fk_retraining_runs_model_version_id_model_versions",
        "retraining_runs", "model_versions",
        ["model_version_id"], ["id"],
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_retraining_runs_model_version_id_model_versions",
        "retraining_runs", type_="foreignkey",
    )
    op.drop_index("ix_retraining_runs_symbol", table_name="retraining_runs")
    op.drop_index("ix_retraining_runs_model_name", table_name="retraining_runs")
    op.drop_column("retraining_runs", "model_version_id")
    op.drop_column("retraining_runs", "completed_at")
    op.drop_column("retraining_runs", "started_at")
    op.drop_column("retraining_runs", "trigger")
    op.drop_column("retraining_runs", "direction")
    op.drop_column("retraining_runs", "timeframe")
    op.drop_column("retraining_runs", "symbol")
    op.drop_column("retraining_runs", "model_name")

    op.drop_index("ix_model_versions_symbol", table_name="model_versions")
    op.drop_column("model_versions", "status")
    op.drop_column("model_versions", "holdout_count")
    op.drop_column("model_versions", "sample_count")
    op.drop_column("model_versions", "git_commit")
    op.drop_column("model_versions", "data_hash")
    op.drop_column("model_versions", "trained_at")
    op.drop_column("model_versions", "direction")
    op.drop_column("model_versions", "timeframe")
    op.drop_column("model_versions", "symbol")
