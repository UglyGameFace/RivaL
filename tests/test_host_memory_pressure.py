"""Low-memory regression tests: protect live learning and honest OOM telemetry."""
from __future__ import annotations

from parlay_bot.perpetuals import host_memory


def test_cgroup_v2_limit_and_proc_rss(monkeypatch):
    fixtures = {
        "/sys/fs/cgroup/memory.max": str(512 * host_memory.MIB),
        "/proc/self/status": "Name:\tpython\nVmRSS:\t440000 kB\n",
        "/sys/fs/cgroup/memory.events": "low 0\nhigh 4\noom 2\noom_kill 1\n",
    }
    monkeypatch.setattr(host_memory, "_read_bytes", fixtures.get)
    state = host_memory.snapshot()
    assert state.limit_bytes == 512 * host_memory.MIB
    assert state.rss_bytes == 440000 * 1024
    assert state.oom_kills == 1
    assert state.pressure
    assert "rss_mb=429" in state.detail


def test_cgroup_v1_fallback_and_unknown_rss(monkeypatch):
    fixtures = {
        "/sys/fs/cgroup/memory.max": "max",
        "/sys/fs/cgroup/memory/memory.limit_in_bytes": str(768 * host_memory.MIB),
        "/proc/self/status": "Name:\tpython\n",
    }
    monkeypatch.setattr(host_memory, "_read_bytes", fixtures.get)
    assert host_memory.snapshot() == host_memory.HostMemory(
        rss_bytes=None, limit_bytes=768 * host_memory.MIB, oom_kills=None,
    )
    assert not host_memory.snapshot().pressure


def test_very_large_unlimited_cgroup_does_not_mask_host_setting(monkeypatch):
    monkeypatch.setenv("RIVAL_HOST_MEMORY_MB", "1024")
    monkeypatch.setattr(host_memory, "_read_bytes", lambda path: (
        str(9223372036854771712) if "memory.max" in path else None
    ))
    state = host_memory.snapshot()
    assert state.limit_bytes == 1024 * host_memory.MIB
    assert not state.pressure


def test_pressure_limit_and_boundary_are_deterministic():
    limit = 512 * host_memory.MIB
    threshold = int(limit * host_memory._PRESSURE_FRACTION)
    assert not host_memory.HostMemory(threshold - 1, limit, 0).pressure
    assert host_memory.HostMemory(threshold + 1, limit, 0).pressure


def test_invalid_environment_and_cgroup_fail_closed_for_deferral(monkeypatch):
    monkeypatch.setenv("RIVAL_HOST_MEMORY_MB", "invalid")
    monkeypatch.setattr(host_memory, "_read_bytes", lambda path: "bad value")
    state = host_memory.snapshot()
    assert state.limit_bytes == 512 * host_memory.MIB
    assert state.rss_bytes is None
    assert not state.pressure


def test_no_mem_pressure_when_rss_below_bound():
    state = host_memory.HostMemory(240 * host_memory.MIB, 512 * host_memory.MIB, None)
    assert not state.pressure
    assert "cgroup_oom_kills=unavailable" in state.detail


def test_cgroup_usage_controls_pressure_when_python_rss_is_low(monkeypatch):
    """Discloud kills the container, which can exceed its Python VmRSS."""
    fixtures = {
        "/sys/fs/cgroup/memory.max": str(512 * host_memory.MIB),
        "/sys/fs/cgroup/memory.current": str(480 * host_memory.MIB),
        "/sys/fs/cgroup/memory.peak": str(490 * host_memory.MIB),
        "/proc/self/status": "VmRSS:\t100000 kB\n",
        "/sys/fs/cgroup/memory.events": "oom_kill 0\n",
    }
    monkeypatch.setattr(host_memory, "_read_bytes", fixtures.get)
    state = host_memory.snapshot()
    assert state.rss_bytes == 100000 * 1024
    assert state.cgroup_bytes == 480 * host_memory.MIB
    assert state.cgroup_peak_bytes == 490 * host_memory.MIB
    assert state.pressure
    assert "cgroup_mb=480 cgroup_peak_mb=490" in state.detail


def test_cgroup_v1_usage_and_peak_fallback(monkeypatch):
    fixtures = {
        "/sys/fs/cgroup/memory.max": "max",
        "/sys/fs/cgroup/memory/memory.limit_in_bytes": str(512 * host_memory.MIB),
        "/sys/fs/cgroup/memory/memory.usage_in_bytes": str(440 * host_memory.MIB),
        "/sys/fs/cgroup/memory/memory.max_usage_in_bytes": str(490 * host_memory.MIB),
        "/proc/self/status": "VmRSS:\t99000 kB\n",
    }
    monkeypatch.setattr(host_memory, "_read_bytes", fixtures.get)
    state = host_memory.snapshot()
    assert state.pressure
    assert state.cgroup_bytes == 440 * host_memory.MIB
    assert state.cgroup_peak_bytes == 490 * host_memory.MIB


def test_cgroup_usage_unavailable_preserves_rss_fallback(monkeypatch):
    monkeypatch.setattr(host_memory, "_read_bytes", lambda _: "invalid")
    state = host_memory.snapshot()
    assert state.cgroup_bytes is None
    assert state.cgroup_peak_bytes is None
    assert not state.pressure
    assert "cgroup_mb=unavailable" in state.detail


def test_cgroup_smaller_than_rss_does_not_hide_memory_pressure():
    limit = 512 * host_memory.MIB
    state = host_memory.HostMemory(
        450 * host_memory.MIB, limit, 0,
        cgroup_bytes=30 * host_memory.MIB,
    )
    assert state.pressure


def test_nonnegative_cgroup_usage_boundaries():
    assert host_memory._nonnegative_usage("0") == 0
    assert host_memory._nonnegative_usage("-1") is None
    assert host_memory._nonnegative_usage("max") is None
    assert host_memory._nonnegative_usage(str(1 << 50)) is None
