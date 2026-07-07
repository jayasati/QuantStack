"""Observation-snapshot utilities shared by event-time feature engines.

Collectors publish several labeled observations per run into market_events;
engines that compute on collector-run time (options chain, market breadth)
bucket those observations back into per-run snapshots.
"""

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

# Observations from one collector run land within one interval.
DEFAULT_BUCKET_SECONDS = 60


@dataclass(frozen=True)
class Snapshot:
    """All observations of one collector run, keyed by their label."""

    ts: datetime
    values: dict[str, float] = field(default_factory=dict)
    metadata: dict[str, dict[str, Any]] = field(default_factory=dict)


def bucket_observations(
    observations: Sequence[tuple[datetime, str, float | None, dict[str, Any]]],
    bucket_seconds: int = DEFAULT_BUCKET_SECONDS,
) -> list[Snapshot]:
    """Group (ts, label, value, metadata) observations into run snapshots."""
    buckets: dict[int, dict[str, Any]] = {}
    for ts, label, value, metadata in observations:
        key = int(ts.timestamp()) // bucket_seconds
        bucket = buckets.setdefault(key, {"ts": ts, "values": {}, "metadata": {}})
        bucket["ts"] = max(bucket["ts"], ts)
        if value is not None:
            bucket["values"][label] = float(value)
        bucket["metadata"][label] = metadata or {}
    return [
        Snapshot(ts=b["ts"], values=b["values"], metadata=b["metadata"])
        for _, b in sorted(buckets.items())
    ]
