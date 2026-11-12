"""The status card's Predictions field and database lines, in plain words.

The card listed every test model by internal id with its raw clock
("pooled_15m_1a10acfb6945: day 8/21 · 649/150 targets · gain +0.008"), and
every slow database job read "A database job took a long time" whatever
job it was. Inputs below are the lines from the Nov 10 status card.
"""
from __future__ import annotations

from parlay_bot.discord_app.status_card import (
    SHOWN_CLOSEST,
    family_words,
    plain,
    prediction_lines,
)

NOV_10 = [
    ("research-loop:counts",
     "🔬 Research loop: 0 open experiment(s); last 7 days: 0 promoted, 2 discarded, 0 failed"),
    ("shadow-progress:calib_incumbent_v1",
     "📊 calib_incumbent_v1: day 8/21 · 1713/150 targets · gain +0.007"),
    ("shadow-progress:flow_v1_venue", "📊 flow_v1_venue: day 1/21 · 5/150 targets · gain +0.007"),
    ("shadow-progress:flow_v2_venue", "📊 flow_v2_venue: day 1/21 · 5/150 targets · gain -0.003"),
    ("shadow-progress:gaussian_nb_v1",
     "📊 gaussian_nb_v1: day 12/21 · 2731/150 targets · gain -0.007"),
    ("shadow-progress:knn21_v1", "📊 knn21_v1: day 12/21 · 2731/150 targets · gain -0.005"),
    ("shadow-progress:liq_v1", "📊 liq_v1: day 4/21 · 945/150 targets · gain -0.060"),
    ("shadow-progress:pooled_15m_1a10acfb6945",
     "📊 pooled_15m_1a10acfb6945: day 8/21 · 649/150 targets · gain +0.008"),
    ("shadow-progress:pooled_15m_218257cd35a7",
     "📊 pooled_15m_218257cd35a7: day 2/21 · 8/150 targets · gain -0.026"),
    ("shadow-progress:pooled_5m_1273de73aa15",
     "📊 pooled_5m_1273de73aa15: day 8/21 · 1920/150 targets · gain +0.002"),
    ("shadow-progress:pooled_60m_798009383b39",
     "📊 pooled_60m_798009383b39: day 2/21 · 2/150 targets · gain +0.194"),
    ("shadow-progress:archive_v1", "⏳ archive_v1: no usable artifact — clock not started"),
]


def test_the_field_leads_with_a_count_and_the_closest_three_in_plain_words():
    lines = prediction_lines(NOV_10)
    # 10 progress lines, but the 6-year model's versions fold per timeframe:
    # calibrated, order-flow 1 and 2, pattern A and B, liquidity, and the
    # 6-year model at 15, 5 and 60 minutes.
    assert lines[0] == ("📊 9 model groups in forward testing. Longest observation: day 12 of 21. "
                       "Examples below are ordered by observation time, not accuracy.")
    assert lines[1:1 + SHOWN_CLOSEST] == [
        ("• pattern test A [scope unspecified]: day 12 of 21, 2731 results (minimum met), "
        "behind the main model on raw score; full gates unverified"),
        ("• pattern test B [scope unspecified]: day 12 of 21, 2731 results (minimum met), "
        "behind the main model on raw score; full gates unverified"),
        ("• 6-year history model (5-min) [scope unspecified]: day 8 of 21, "
         "1920 results (minimum met), ahead on raw score; qualification pending; full gates unverified"),
    ]
    assert lines[1 + SHOWN_CLOSEST] == "• 6 other groups; less observation time"
    # Every other research line is kept, as written.
    assert lines[-2:] == [NOV_10[0][1], NOV_10[-1][1]]


def test_no_internal_id_or_raw_clock_reaches_the_summary():
    summary = "\n".join(prediction_lines(NOV_10)[:1 + SHOWN_CLOSEST + 1])
    for raw in ("pooled_", "gaussian_nb_v1", "knn21_v1", "/150 targets", "gain +", "_v1"):
        assert raw not in summary


def test_a_tiny_sample_is_never_called_ahead():
    lines = prediction_lines([
        ("shadow-progress:pooled_60m_798009383b39",
         "📊 pooled_60m_798009383b39: day 2/21 · 2/150 targets · gain +0.194"),
    ])
    assert lines == [
        ("📊 1 model group in forward testing. Longest observation: day 2 of 21. "
         "Examples below are ordered by observation time, not accuracy."),
        ("• 6-year history model (60-min) [scope unspecified]: day 2 of 21, "
         "2 of 150 results, too few results to judge; full gates unverified"),
    ]


def test_versions_of_one_model_are_one_line_led_by_the_furthest():
    lines = prediction_lines([
        ("shadow-progress:pooled_15m_aaaaaaaaaaaa",
         "📊 pooled_15m_aaaaaaaaaaaa: day 2/21 · 8/150 targets · gain -0.026"),
        ("shadow-progress:pooled_15m_bbbbbbbbbbbb",
         "📊 pooled_15m_bbbbbbbbbbbb: day 8/21 · 649/150 targets · gain +0.0001"),
    ])
    assert lines[1] == ("• 6-year history model (15-min) (2 versions) [scope unspecified]: "
                        "day 8 of 21, 649 results (minimum met), level with the main model; "
                        "full gates unverified")


def test_lines_it_cannot_read_are_kept_as_they_were():
    research = [("shadow-progress:odd", "📊 something new: day ? of 21"),
                ("research-loop:counts", "🔬 Research loop: 1 open experiment(s)")]
    assert prediction_lines(research) == [text for _, text in research]


def test_family_names():
    assert family_words("calib_incumbent_v1") == "calibrated main model"
    assert family_words("flow_v1_venue") == "order-flow test 1, per exchange"
    assert family_words("pooled_5m_1273de73aa15_venue") == (
        "6-year history model (5-min), per exchange")
    assert family_words("cross_v1") == "all-exchanges test"
    assert family_words("regime_wild_v1") == "wild-only test"


def test_each_slow_database_line_says_which_job():
    assert plain(
        "RivaL PostgreSQL connection held 12.40s owner=multi_exchange._withdraw_no_trade_labels:139 "
        "worker=asyncio_3 queries=1 sql_total=12.10s slowest=UPDATE:12.10s commit_total=0.20s "
        "(SQL/parameters omitted)"
    ) == "A database job took a long time (multi exchange withdraw no trade labels)"
    assert plain(
        "RivaL PostgreSQL slow operation owner=runtime_health.inspect:99 operation=SELECT "
        "statement=perp_forward_audit#867b87a6 duration=2.40s (SQL/parameters omitted)"
    ) == "A database query took over 2 seconds (runtime health inspect)"
    # Without an owner the line still reads plainly.
    assert plain("RivaL PostgreSQL connection held 6.00s") == "A database job took a long time"
