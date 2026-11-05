"""Venue-prior challengers move a pooled model's prior to the venue's own up-rate."""
from __future__ import annotations

import math

import pytest

from parlay_bot.perpetuals.venue_prior import (
    SUFFIX,
    venue_prior_calls,
    venue_prior_probability,
)


def test_a_venue_with_mostly_flat_closes_pulls_the_call_down():
    # dYdX XRP 5m, week to Nov 5: 2% UP (97% flat closes graded NOT UP).
    adjusted = venue_prior_probability(0.52, trained_up=0.5, stream_up=0.05)
    assert adjusted < 0.07
    # The model's own evidence is kept: a stronger UP call stays above the prior.
    assert venue_prior_probability(0.60, trained_up=0.5, stream_up=0.05) > adjusted


def test_matching_priors_change_nothing():
    assert venue_prior_probability(0.57, trained_up=0.48, stream_up=0.48) == pytest.approx(0.57)


def test_bounds_and_bad_inputs():
    assert venue_prior_probability(0.98, trained_up=0.02, stream_up=0.98) == 0.98
    for bad in (0.0, 1.0, math.nan):
        with pytest.raises(ValueError):
            venue_prior_probability(bad, trained_up=0.5, stream_up=0.5)


def test_calls_become_separate_families_and_keep_provenance():
    calls = [
        {"family": "pooled_5m_abcdef123456", "probability": 0.51,
         "model_sha256": "a" * 64, "training_rows": 9},
        {"family": "flow_v2", "probability": 0.55, "model_sha256": "b" * 64,
         "training_rows": 400, "trained_up": 0.47},
        {"family": "calib_incumbent_v1", "probability": 0.5, "model_sha256": "c" * 64},
        {"family": "flow_v1_venue", "probability": 0.5, "model_sha256": "d" * 64},
        {"family": "flow_v1", "probability": "nan?", "model_sha256": "e" * 64},
    ]
    out = venue_prior_calls(calls, stream_up=0.38)
    assert [c["family"] for c in out] == ["pooled_5m_abcdef123456" + SUFFIX, "flow_v2" + SUFFIX]
    assert out[0]["model_sha256"] == "a" * 64 and out[0]["training_rows"] == 9
    assert out[1]["probability"] == pytest.approx(
        venue_prior_probability(0.55, trained_up=0.47, stream_up=0.38))
    assert all(c["family"].startswith(("pooled_", "flow_")) for c in out)
