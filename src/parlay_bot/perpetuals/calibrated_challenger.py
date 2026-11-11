"""Calibrated incumbent: the live model's call pulled toward the up-rate.

Live grading showed the models get direction slightly right but are
overconfident. Out of sample (fit on one 3.5-day half, scored on the next)
shrinking the incumbent toward the up-rate beat both the raw model and the
up-rate at 5 minutes. This challenger issues

    p = up_rate + a * (incumbent - up_rate),   0 <= a <= 1

with ``a`` the least-squares (Brier-optimal) shrink fitted ONLY on forecasts
whose outcome was already graded before this forecast's clock. With too few
graded outcomes it says the up-rate (a = 0) instead of guessing.

It is a shadow challenger like every other family: same targets, same
grading, same quote shadow, and promotion only through the unchanged gates.
"""
from __future__ import annotations

import hashlib
import json
import logging
import threading
from datetime import UTC, datetime, timedelta

_LOG = logging.getLogger(__name__)

FAMILY = "calib_incumbent_v1"
FIT_WINDOW = timedelta(days=7)
# A 7-day fit barely moves in an hour; every 30 min its 12 reads of 7 days
# caused ~120 slow warnings a day (Nov 11).
REFIT_EVERY = timedelta(minutes=60)
MIN_GRADED = 500
# The incumbent's call is recorded on every family row; read it from one
# family so each incumbent forecast counts once.
SOURCE_FAMILY = "gaussian_nb_v1"


def shrink(dd: float, de: float) -> float:
    """Brier-optimal a for p = u + a(q - u): minimises sum((u - y + a d)^2)."""
    if dd <= 0:
        return 0.0
    return max(0.0, min(1.0, -de / dd))


class CalibratedIncumbent:
    family = FAMILY

    def __init__(self, store) -> None:
        self.store = store
        self._fits: dict[tuple[str, int], tuple[float, int]] = {}
        self._fitted_at: datetime | None = None
        self._refitting: threading.Thread | None = None

    def refit_in_background(self, now: datetime, assets) -> None:
        """Start a due refit on its own thread; the cycle never waits for it.

        Its 12 seven-day counts took about half a minute in production, at
        the start of the first cycle after every restart and once an hour,
        delaying that cycle's predictions. Until the first fit lands this
        family simply issues nothing (``call`` returns None), as before a
        first fit; afterwards it uses the newest completed fit.
        """
        stamp = now.astimezone(UTC)
        if self._fitted_at is not None and timedelta(0) <= stamp - self._fitted_at < REFIT_EVERY:
            return
        running = self._refitting
        if running is not None and running.is_alive():
            return

        def work() -> None:
            try:
                self.refit(stamp, tuple(assets))
            except Exception:  # noqa: BLE001 - optional challenger only
                _LOG.exception("RivaL calibrated challenger fit unavailable")

        self._refitting = threading.Thread(target=work, name="rival-calib-refit", daemon=True)
        self._refitting.start()

    def refit(self, now: datetime, assets, horizons=(5, 15, 60)) -> None:
        """One causal fit per asset/horizon, at most every REFIT_EVERY."""
        stamp = now.astimezone(UTC)
        if self._fitted_at is not None and timedelta(0) <= stamp - self._fitted_at < REFIT_EVERY:
            return
        fits: dict[tuple[str, int], tuple[float, int]] = {}
        with self.store.connect() as conn:
            for asset in sorted(assets):
                for horizon in horizons:
                    row = conn.execute(
                        """SELECT COUNT(*) AS n,
                                  SUM((incumbent_up-baseline_up)*(incumbent_up-baseline_up)) AS dd,
                                  SUM((incumbent_up-baseline_up)*(baseline_up-outcome_up)) AS de
                           FROM rival_model_shadow_forecasts
                           WHERE asset=? AND horizon_minutes=? AND family=?
                             AND issued_at>=? AND issued_at<=?
                             AND settled_at IS NOT NULL AND settled_at<=?
                             AND outcome_up IN (0,1)""",
                        (asset, horizon, SOURCE_FAMILY, (stamp - FIT_WINDOW).isoformat(),
                         stamp.isoformat(), stamp.isoformat()),
                    ).fetchone()
                    n = int(row["n"] or 0)
                    a = (shrink(float(row["dd"] or 0), float(row["de"] or 0))
                         if n >= MIN_GRADED else 0.0)
                    fits[(asset, horizon)] = (a, n)
        self._fits, self._fitted_at = fits, stamp

    def call(self, *, asset: str, horizon: int, incumbent_up: float,
             baseline_up: float) -> dict | None:
        fit = self._fits.get((asset, horizon))
        if fit is None or not (0 <= incumbent_up <= 1 and 0 <= baseline_up <= 1):
            return None
        a, n = fit
        probability = baseline_up + a * (incumbent_up - baseline_up)
        probability = max(0.02, min(0.98, probability))
        if not 0 < probability < 1:
            return None
        return {
            "family": self.family,
            "probability": probability,
            "training_rows": n,
            "model_sha256": hashlib.sha256(json.dumps(
                [asset, horizon, round(a, 6), n, self._fitted_at.isoformat()],
                separators=(",", ":")).encode()).hexdigest(),
            "shrink": a,
        }
