from datetime import UTC, datetime, timedelta

from app.features.replay import walk_forward_snapshots

BASE = datetime(2026, 7, 1, tzinfo=UTC)


def ts(days: int) -> datetime:
    return BASE + timedelta(days=days)


ROWS = [
    ("alpha", ts(0), 1.0, "v1"),
    ("beta", ts(0), 10.0, "v1"),
    ("alpha", ts(1), 2.0, "v1"),
    ("alpha", ts(3), 3.0, "v2"),
    ("beta", ts(4), 20.0, "v1"),
]


def test_snapshot_reflects_state_at_each_moment() -> None:
    snapshots = walk_forward_snapshots(ROWS, [ts(0), ts(2), ts(5)])
    assert snapshots[0]["alpha"]["value"] == 1.0
    assert snapshots[0]["beta"]["value"] == 10.0
    # Day 2: alpha updated on day 1; day-3 value must NOT leak backwards.
    assert snapshots[1]["alpha"]["value"] == 2.0
    assert snapshots[1]["beta"]["value"] == 10.0
    # Day 5: everything caught up, including the v2 recalculation.
    assert snapshots[2]["alpha"] == {"value": 3.0, "ts": ts(3).isoformat(), "version": "v2"}
    assert snapshots[2]["beta"]["value"] == 20.0


def test_no_lookahead_before_first_observation() -> None:
    snapshots = walk_forward_snapshots(ROWS, [ts(-1)])
    assert snapshots[0] == {}


def test_snapshots_are_independent_copies() -> None:
    snapshots = walk_forward_snapshots(ROWS, [ts(1), ts(4)])
    snapshots[0]["alpha"]["value"] = 999.0
    assert snapshots[1]["alpha"]["value"] == 3.0  # later snapshot untouched


def test_unsorted_request_timestamps_are_handled() -> None:
    snapshots = walk_forward_snapshots(ROWS, [ts(5), ts(0)])
    # Output follows ascending time order regardless of request order.
    assert snapshots[0]["alpha"]["value"] == 1.0
    assert snapshots[1]["alpha"]["value"] == 3.0


def test_exact_timestamp_boundary_is_inclusive() -> None:
    snapshots = walk_forward_snapshots(ROWS, [ts(3)])
    assert snapshots[0]["alpha"]["value"] == 3.0  # value stamped exactly at as_of counts
