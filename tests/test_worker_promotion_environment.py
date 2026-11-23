"""Worker-environment regression: the promotion path needs no bot deps.

PR #758 moved the proof-review driver into the rival-worker service,
whose environment deliberately installs only psycopg / pydantic /
httpx (services/rival-worker/requirements.txt -- no discord.py,
websockets, sentry-sdk, duckdb, or ML stack). The first live slot
failed on every cycle: ``select_family_proof_scope`` reaches
``family_quotes``, which imports ``live_quotes``, whose top-level
``import discord`` (used only by the ``quote_embed`` display helper)
raised ModuleNotFoundError in the worker. Local and CI environments
install the full dependency set, so no existing test could see it.

This test replays the worker's environment honestly: a subprocess
whose import system refuses every bot-only package, running the
driver's scope selection and one snapshot-isolated shadow slot
end-to-end against the same seeded fixtures as
``tests/test_promotion_driver.py``. If any module on the promotion
path -- top-level or delayed import -- reaches for a bot-only
dependency again, the subprocess dies and this test fails.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Packages the bot installs (pyproject.toml + requirements.txt,
# including the archive / ml-research extras) that the worker's
# requirements.txt deliberately omits.
BOT_ONLY_PACKAGES = (
    "discord", "websockets", "sentry_sdk", "duckdb", "numpy", "pandas",
    "sklearn", "lightgbm", "xgboost", "catboost",
)

_SCENARIO = r"""
import sys

BLOCKED = frozenset({
    "discord", "websockets", "sentry_sdk", "duckdb", "numpy", "pandas",
    "sklearn", "lightgbm", "xgboost", "catboost",
})


class _WorkerEnvBlocker:
    # Refuse bot-only packages as if they were never installed.

    def find_spec(self, fullname, path=None, target=None):
        if fullname.split(".")[0] in BLOCKED:
            raise ModuleNotFoundError(
                f"No module named {fullname!r} "
                "(blocked: rival-worker environment simulation)")
        return None


sys.meta_path.insert(0, _WorkerEnvBlocker())
for _name in [m for m in sys.modules if m.split(".")[0] in BLOCKED]:
    del sys.modules[_name]

import tempfile
from pathlib import Path

# The whole Job-2 import closure, named explicitly so a new top-level
# offender fails here even before the slot runs.
import parlay_bot.perpetuals.family_governance  # noqa: F401
import parlay_bot.perpetuals.family_quotes  # noqa: F401
import parlay_bot.perpetuals.live_quotes  # noqa: F401
import parlay_bot.perpetuals.profit_proof  # noqa: F401
from parlay_bot.perpetuals import promotion_driver as driver
from parlay_bot.storage.hot import SQLiteHotStore
from tests.test_family_cost_promotion import _seed
from tests.test_promotion_driver import NOW, _scope_row, _seed_candle

with tempfile.TemporaryDirectory() as tmp:
    store = SQLiteHotStore(Path(tmp) / "worker.sqlite")
    try:
        _seed_candle(store, observed_at=NOW)
        slot, scope = driver.select_proof_scope(store, NOW)
        assert scope is not None, "no proof scope selected"
        _seed(store, NOW, quote_count=0, horizon=scope[3])

        shadow = driver.run_promotion_cycle(store, now=NOW, shadow=True)
        assert shadow["slot"] == slot and shadow["scope"] == scope
        assert shadow["status"] == "held"
        assert shadow["selected_family"] is None
        assert shadow["row_sha256"] is not None
        assert shadow["promotion_detected"] is False
        # Snapshot-isolated: the shadow slot restored its writes.
        assert _scope_row(store, scope) is None
    finally:
        store.close()
print("WORKER_ENV_OK")
"""


def test_promotion_path_runs_without_bot_dependencies():
    env = dict(os.environ)
    pythonpath = [str(ROOT), str(ROOT / "src")]
    if env.get("PYTHONPATH"):
        pythonpath.append(env["PYTHONPATH"])
    env["PYTHONPATH"] = os.pathsep.join(pythonpath)
    result = subprocess.run(
        [sys.executable, "-c", _SCENARIO],
        cwd=ROOT, env=env, capture_output=True, text=True, timeout=600,
        check=False)
    assert result.returncode == 0 and "WORKER_ENV_OK" in result.stdout, (
        "promotion path failed in the simulated worker environment:\n"
        + result.stdout[-2000:] + "\n" + result.stderr[-4000:])
