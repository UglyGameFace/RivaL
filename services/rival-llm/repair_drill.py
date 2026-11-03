"""Controlled crash-and-recover drill for the private LFM3 backend (issue #268).

Runs the shipped gateway, supervisor and llama.cpp binary with the real,
SHA-verified LFM3 weights on an isolated CI runner, never on the production
host. It waits for a healthy backend, kills the llama.cpp process with
SIGKILL, and requires the unmodified BackendRepair supervisor to relaunch it
and the authenticated /v1/status endpoint to report ready again with
repair_count == 1. Prints one JSON receipt; exits non-zero on any failure.

Usage (from an extracted Discloud package directory):
    python repair_drill.py --app-dir discloud-download
"""
from __future__ import annotations

import argparse
import json
import os
import secrets
import signal
import sys
import threading
import time
import urllib.request
from pathlib import Path


def _status(port: int, token: str) -> dict:
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/v1/status",
        headers={"Authorization": f"Bearer {token}"}, method="GET",
    )
    with urllib.request.urlopen(request, timeout=5) as response:
        return json.loads(response.read(4096))


def _tick_until(supervisor, gateway, predicate, *, deadline: float, step: float) -> bool:
    while time.monotonic() < deadline:
        supervisor.tick(gateway)
        if predicate():
            return True
        time.sleep(step)
    return False


def run(app_dir: Path, *, ready_timeout: float, step: float) -> dict:
    os.chdir(app_dir)
    sys.path.insert(0, str(app_dir))
    import service
    from model_download import ensure_model

    profile = "lfm3"
    repo, filename = service.MODEL_CATALOG[profile]
    model_path = ensure_model(repo=repo, filename=filename, profile=profile)
    binary = Path("runtime/bin/llama-server").resolve()
    command, env = service.backend_command(binary, model_path, service.CONTEXT_TOKENS)
    process = service.subprocess.Popen(command, env=env)
    token = secrets.token_hex(32)
    gateway = service.Gateway(
        "127.0.0.1", 0, token=token, model=profile, process=process,
    )
    server = threading.Thread(target=gateway.serve_forever, daemon=True)
    server.start()
    supervisor = service.BackendRepair(
        process, command, env, binary=binary, model=model_path,
    )
    receipt: dict = {"drill": "lfm3_backend_crash_recovery", "passed": False}
    try:
        started = time.monotonic()
        if not _tick_until(
            supervisor, gateway, gateway.backend_ready,
            deadline=started + ready_timeout, step=step,
        ):
            receipt["failure"] = "backend_never_became_ready"
            return receipt
        receipt["first_ready_seconds"] = round(time.monotonic() - started, 1)
        before = _status(gateway.server_port, token)
        if before.get("ready") is not True or before.get("repair_count") != 0:
            receipt["failure"] = "unexpected_status_before_fault"
            return receipt

        killed_pid = supervisor.process.pid
        os.kill(killed_pid, signal.SIGKILL)
        supervisor.process.wait(timeout=10)
        receipt["killed_pid"] = killed_pid
        fault_at = time.monotonic()

        recovered = _tick_until(
            supervisor, gateway,
            lambda: (gateway.repair_count == 1
                     and supervisor.process.pid != killed_pid
                     and gateway.backend_ready()),
            deadline=fault_at + ready_timeout, step=step,
        )
        receipt["last_fault"] = supervisor.last_fault
        receipt["repair_state"] = gateway.repair_state
        receipt["repair_count"] = gateway.repair_count
        if not recovered:
            receipt["failure"] = "backend_not_recovered"
            return receipt
        receipt["recovery_seconds"] = round(time.monotonic() - fault_at, 1)
        receipt["new_pid_differs"] = supervisor.process.pid != killed_pid
        after = _status(gateway.server_port, token)
        receipt["status_after"] = {
            key: after.get(key) for key in ("ready", "repair_state", "repair_count")
        }
        receipt["passed"] = (
            supervisor.last_fault == "process_exited"
            and after.get("ready") is True
            and after.get("repair_count") == 1
        )
        if not receipt["passed"]:
            receipt["failure"] = "status_after_recovery_mismatch"
        return receipt
    finally:
        gateway.shutdown()
        gateway.server_close()
        current = supervisor.process
        if current.poll() is None:
            current.terminate()
            try:
                current.wait(timeout=10)
            except service.subprocess.TimeoutExpired:
                current.kill()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--app-dir", type=Path, required=True)
    parser.add_argument("--ready-timeout", type=float, default=240.0)
    parser.add_argument("--step", type=float, default=3.0)
    args = parser.parse_args(argv)
    receipt = run(args.app_dir.resolve(), ready_timeout=args.ready_timeout, step=args.step)
    print(json.dumps(receipt, sort_keys=True))
    return 0 if receipt["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
