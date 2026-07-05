"""Volume 2 — OHLCV candles and raw ticks time-series tables.

Revision ID: 0002
Revises: 0001
Create Date: 2026-07-05

"""
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "ohlcv_candles",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("data", JSONB(), nullable=True),
        sa.Column("symbol", sa.String(50), nullable=False),
        sa.Column("timeframe", sa.String(10), nullable=False),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("open", sa.Float(), nullable=False),
        sa.Column("high", sa.Float(), nullable=False),
        sa.Column("low", sa.Float(), nullable=False),
        sa.Column("close", sa.Float(), nullable=False),
        sa.Column("volume", sa.BigInteger(), nullable=False),
        sa.UniqueConstraint("symbol", "timeframe", "ts", name="uq_ohlcv_symbol_tf_ts"),
    )
    op.create_index("ix_ohlcv_candles_symbol", "ohlcv_candles", ["symbol"])
    op.create_index("ix_ohlcv_candles_ts", "ohlcv_candles", ["ts"])

    op.create_table(
        "raw_ticks",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("data", JSONB(), nullable=True),
        sa.Column("symbol", sa.String(50), nullable=False),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ltp", sa.Float(), nullable=False),
    )
    op.create_index("ix_raw_ticks_symbol", "raw_ticks", ["symbol"])
    op.create_index("ix_raw_ticks_ts", "raw_ticks", ["ts"])


def downgrade() -> None:
    op.drop_table("raw_ticks")
    op.drop_table("ohlcv_candles")
