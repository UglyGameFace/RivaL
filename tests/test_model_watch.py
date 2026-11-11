"""Watch several models at once: compare card and a self-updating watch post."""
from __future__ import annotations

from types import SimpleNamespace

import discord
import pytest

from parlay_bot.discord_app import model_watch
from parlay_bot.discord_app.model_watch import ModelWatchService, compare_embed
from parlay_bot.discord_app.research_dashboard import ResearchDashboard
from tests.test_model_detail import _promotion, _seed, store  # noqa: F401 - fixture


def _reports(store):  # noqa: F811
    from parlay_bot.discord_app.model_watch import reports_for
    return reports_for(store, "BTC", ["Trend-only", "6-year", "Cross-all", "Gone"])


def test_compare_card_puts_models_side_by_side_per_timeframe(store):  # noqa: F811
    _seed(store)
    embed = compare_embed(_reports(store), asset="BTC")
    fields = {f.name: f.value for f in embed.fields}
    assert embed.title == "🧪 Compare · BTC · 4 models"
    from tests.test_trial_board import _grouped

    five = _grouped(fields["📏 5-min"])
    # Grouped by status, best edge first; one line per model, phone width.
    assert [words[0] for _, words in five] == ["Trend-only", "6-year", "Cross-all"]
    assert five[0] == ("ahead", ["Trend-only", "12/21", "300", "75%", "+2.5"])
    assert five[2][0] == "cut"
    block = fields["📏 5-min"].split("```")[1].strip().splitlines()
    assert max(len(line) for line in block) <= 35
    assert "Best here: **Trend-only**" in fields["📏 5-min"]
    assert "📏 15-min" in fields and "📏 60-min" not in fields     # no 60-min trials
    assert fields["Not on trial here"].startswith("Gone:")
    assert len(embed) < 6000


class _Message:
    def __init__(self, mid, channel):
        self.id, self.channel, self.embeds, self.edits = mid, channel, [], []

    async def edit(self, *, embed=None, view=None):
        self.edits.append((embed, view))


class _Channel:
    def __init__(self, cid=500, fail=False):
        self.id, self.fail, self.sent = cid, fail, []

    async def send(self, *, embed, view):
        if self.fail:
            raise discord.HTTPException(SimpleNamespace(status=403, reason="no"), "no")
        message = _Message(9000 + len(self.sent), self)
        message.embeds = [embed]
        self.sent.append(message)
        return message


class _Bot:
    def __init__(self, channel):
        self.channel = channel

    def get_partial_messageable(self, cid):
        bot = self

        class _P:
            def get_partial_message(self, mid):
                return next(m for m in bot.channel.sent if m.id == mid)
        return _P()


def _interaction(user=1, channel=None, message=None):
    followups = []

    async def defer(**_):
        return None

    async def send(text, **_):
        followups.append(text)

    return SimpleNamespace(user=SimpleNamespace(id=user, dm_channel=channel), channel=channel,
                           message=message, followups=followups,
                           response=SimpleNamespace(defer=defer),
                           followup=SimpleNamespace(send=send))


@pytest.mark.asyncio
async def test_a_watch_post_updates_itself_and_only_its_owner_can_stop_it(store):  # noqa: F811
    _seed(store)
    channel = _Channel()
    service = ModelWatchService(_Bot(channel), store)
    notice = await service.start(_interaction(channel=channel), "BTC",
                                 ["Trend-only", "6-year"])
    assert notice.startswith("📡 Live watch posted: Trend-only, 6-year on BTC")
    post = channel.sent[0]
    assert post.embeds[0].title == "📡 Live watch · BTC · 2 models"
    assert await service.cycle() == 1 and len(post.edits) == 1      # the 5-minute refresh
    stranger = _interaction(user=2, channel=channel, message=post)
    await service.handle(stranger, "stop")
    assert stranger.followups == ["Only the person who started this watch can stop it."]
    owner = _interaction(user=1, channel=channel, message=post)
    await service.handle(owner, "stop")
    assert owner.followups == ["Watch stopped."]
    assert await service.cycle() == 0                                 # nothing left to edit


@pytest.mark.asyncio
async def test_watches_are_capped_and_fall_back_to_dms(store, monkeypatch):  # noqa: F811
    _seed(store)
    dm = _Channel(cid=777)
    blocked = _Channel(fail=True)
    service = ModelWatchService(_Bot(dm), store)
    interaction = _interaction(channel=blocked)
    interaction.user.dm_channel = dm
    for _ in range(model_watch.MAX_WATCHES_PER_OWNER):
        assert (await service.start(interaction, "BTC", ["Trend-only"])).startswith("📡")
    assert len(dm.sent) == model_watch.MAX_WATCHES_PER_OWNER       # posted to the DM
    assert "already have 3 live watches" in await service.start(interaction, "BTC",
                                                                  ["6-year"])


@pytest.mark.asyncio
async def test_a_deleted_watch_post_is_forgotten(store):  # noqa: F811
    _seed(store)
    channel = _Channel()
    service = ModelWatchService(_Bot(channel), store)
    await service.start(_interaction(channel=channel), "BTC", ["Trend-only"])

    async def gone(**_):
        raise discord.NotFound(SimpleNamespace(status=404, reason="gone"), "gone")

    channel.sent[0].edit = gone
    assert await service.cycle() == 0
    assert service._rows() == []


@pytest.mark.asyncio
async def test_picking_several_models_opens_the_compare_card(store):  # noqa: F811
    _seed(store)
    view = ResearchDashboard(owner_id=1, market=SimpleNamespace(store=store), ai=None,
                             venues=("okx",), watch_service=object())
    view.screen, view.section = "accuracy", "better"
    view._rebuild()

    async def plain():
        return discord.Embed(title="Results")

    view._render_full = plain
    await view.render()
    picker = next(i for i in view.children if getattr(i, "custom_id", "") ==
                  "rival:research:model")
    assert picker.max_values == 3 and picker.min_values == 1
    view.compare = ["Trend-only", "6-year"]
    view._rebuild()
    shown = await view.render()
    assert shown.title == "🧪 Compare · BTC · 2 models"
    ids = {getattr(i, "custom_id", "") for i in view.children}
    assert {"rival:research:watch", "rival:research:model:back"} <= ids
