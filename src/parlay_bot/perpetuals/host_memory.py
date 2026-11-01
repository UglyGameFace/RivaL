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


def _rss_bytes() -> int | None:
    status = _read_bytes("/proc/self/status")
    if not status:
        return None
    for line in status.splitlines():
        if line.startswith("VmRSS:"):
            bits = line.split()
            if len(bits) == 3 and bits[2] == "kB" and bits[1].isdigit():
                return int(bits[1]) * 1024
    return None


def _cgroup_limit() -> int | None:
    for path in (
        "/sys/fs/cgroup/memory.max",
        "/sys/fs/cgroup/memory/memory.limit_in_bytes",
    ):
        limit = _positive_limit(_read_bytes(path))
        if limit:
            return limit
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

    @property
    def pressure(self) -> bool:
        # Never postpone useful work when Linux RSS cannot be measured.
        return (
            self.rss_bytes is not None
            and self.rss_bytes >= self.limit_bytes * _PRESSURE_FRACTION
        )

    @property
    def detail(self) -> str:
        rss = "unavailable" if self.rss_bytes is None else str(self.rss_bytes // MIB)
        kills = "unavailable" if self.oom_kills is None else str(self.oom_kills)
        return f"rss_mb={rss} limit_mb={self.limit_bytes // MIB} cgroup_oom_kills={kills}"


def snapshot() -> HostMemory:
    return HostMemory(
        rss_bytes=_rss_bytes(),
        limit_bytes=_cgroup_limit() or _fallback_limit(),
        oom_kills=_cgroup_oom_kills(),
    )
