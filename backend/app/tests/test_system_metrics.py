"""CPU/memory monitoring unit tests (Volume 1, Chapter 12 gap-fill)."""

from app.core.system_metrics import SystemMetricsSampler


def test_snapshot_reports_process_and_system_metrics() -> None:
    sampler = SystemMetricsSampler()
    snapshot = sampler.snapshot()

    assert "process" in snapshot and "system" in snapshot
    process = snapshot["process"]
    assert process["memory_rss_mb"] > 0
    assert process["num_threads"] >= 1
    assert isinstance(process["cpu_percent"], float)

    system = snapshot["system"]
    assert 0.0 <= system["memory_percent"] <= 100.0
    assert system["memory_available_mb"] > 0
