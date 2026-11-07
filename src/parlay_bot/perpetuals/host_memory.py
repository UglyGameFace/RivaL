"""Small, dependency-free host memory telemetry for a 512 MB Discloud bot.

This is an optional-work deferral signal, not an OOM recovery mechanism.
An external SIGKILL cannot be caught by Python. Live market observations,
settlement, Discord handlers and existing forecasts must continue.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

MIB = 1024 * 1024
# Only valid on the shipped 512 MB Discloud configuration when cgroups are
# unavailable. Operators changing RAM should set RIVAL_HOST_MEMORY_MB too.
_FALLBACK_MB = 512
_PRESSURE_FRACTION = 0.83


def _read_bytes(path: str) -> str | None:
    try:
        return Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return None


def _positive_limit(text: str | None) -> int | None:
    try:
        size = int(str(text).strip())
    except (ValueError, TypeError):
        return None
    # A huge integer means "unlimited" in older memory cgroup deployments.
    return size if 64 * MIB <= size < 1 << 50 else None


def _status_kb(key: str) -> int | None:
    status = _read_bytes("/proc/self/status")
    if not status:
        return None
    for line in status.splitlines():
        if line.startswith(key):
            bits = line.split()
            if len(bits) == 3 and bits[2] == "kB" and bits[1].isdigit():
                return int(bits[1]) * 1024
    return None


def _rss_bytes() -> int | None:
    return _status_kb("VmRSS:")


def _vm_data_bytes() -> int | None:
    """Heap (VmData): data segment, where Python objects and C malloc live.

    The receipt's RSS alone cannot tell heap growth from mmap'd or
    file-backed growth; the Nov 5 kill was diagnosed from anonymous
    memory found by hand, not from any receipt. Recording the heap beside
    RSS makes the next receipt say which one moved.
    """
    return _status_kb("VmData:")


def _cgroup_limit() -> int | None:
    for path in (
        "/sys/fs/cgroup/memory.max",
        "/sys/fs/cgroup/memory/memory.limit_in_bytes",
    ):
        limit = _positive_limit(_read_bytes(path))
        if limit:
            return limit
    return None


def _nonnegative_usage(text: str | None) -> int | None:
    """Read cgroup usage/peak; zero is valid, unlike a memory limit."""
    try:
        size = int(str(text).strip())
    except (ValueError, TypeError):
        return None
    return size if 0 <= size < 1 << 50 else None


def _cgroup_usage() -> int | None:
    for path in (
        "/sys/fs/cgroup/memory.current",
        "/sys/fs/cgroup/memory/memory.usage_in_bytes",
    ):
        size = _nonnegative_usage(_read_bytes(path))
        if size is not None:
            return size
    return None


def _cgroup_peak() -> int | None:
    for path in (
        "/sys/fs/cgroup/memory.peak",
        "/sys/fs/cgroup/memory/memory.max_usage_in_bytes",
    ):
        size = _nonnegative_usage(_read_bytes(path))
        if size is not None:
            return size
    return None


def _fallback_limit() -> int:
    raw = os.environ.get("RIVAL_HOST_MEMORY_MB", str(_FALLBACK_MB))
    try:
        amount = int(raw)
    except (ValueError, TypeError):
        amount = _FALLBACK_MB
    return (amount if 128 <= amount <= 65536 else _FALLBACK_MB) * MIB


def _cgroup_oom_kills() -> int | None:
    raw = _read_bytes("/sys/fs/cgroup/memory.events")
    if raw is None:
        raw = _read_bytes("/sys/fs/cgroup/memory/memory.oom_control")
    if raw is not None:
        for line in raw.splitlines():
            key, _, value = line.partition(" ")
            if key == "oom_kill" and value.strip().isdigit():
                return int(value.strip())
    return None


@dataclass(frozen=True)
class HostMemory:
    rss_bytes: int | None
    limit_bytes: int
    oom_kills: int | None
    cgroup_bytes: int | None = None
    cgroup_peak_bytes: int | None = None
    vm_data_bytes: int | None = None

    @property
    def pressure(self) -> bool:
        # Discloud enforces the container/cgroup limit, not this process's
        # VmRSS alone. The container can also contain file cache, other
        # processes and shared allocations. Fall back to RSS when unavailable.
        readings = (self.rss_bytes, self.cgroup_bytes)
        used = max((value for value in readings if value is not None), default=None)
        return used is not None and used >= self.limit_bytes * _PRESSURE_FRACTION

    @property
    def detail(self) -> str:
        rss = "unavailable" if self.rss_bytes is None else str(self.rss_bytes // MIB)
        usage = ("unavailable" if self.cgroup_bytes is None
                 else str(self.cgroup_bytes // MIB))
        peak = ("unavailable" if self.cgroup_peak_bytes is None
                else str(self.cgroup_peak_bytes // MIB))
        kills = "unavailable" if self.oom_kills is None else str(self.oom_kills)
        heap = ("unavailable" if self.vm_data_bytes is None
                else str(self.vm_data_bytes // MIB))
        return (f"rss_mb={rss} heap_mb={heap} cgroup_mb={usage} cgroup_peak_mb={peak} "
                f"limit_mb={self.limit_bytes // MIB} cgroup_oom_kills={kills}")


def snapshot() -> HostMemory:
    cgroup_limit = _cgroup_limit()
    return HostMemory(
        rss_bytes=_rss_bytes(),
        limit_bytes=cgroup_limit or _fallback_limit(),
        oom_kills=_cgroup_oom_kills(),
        # An unlimited/root cgroup may measure the entire host rather than
        # this 512 MB app. Only compare usage with a real cgroup limit.
        cgroup_bytes=_cgroup_usage() if cgroup_limit is not None else None,
        cgroup_peak_bytes=_cgroup_peak() if cgroup_limit is not None else None,
        vm_data_bytes=_vm_data_bytes(),
    )
