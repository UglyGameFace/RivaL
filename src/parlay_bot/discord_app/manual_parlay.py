"""Private, cache-only manual sports parlay picker."""
from __future__ import annotations

import discord

from parlay_bot.parlays.manual import available_selections, selected_slip


class _Pick(discord.ui.Select):
    def __init__(self, parent: ManualParlayView):
        self.parent_picker = parent
        options = []
        for i, row in enumerate(parent.visible):
            options.append(discord.SelectOption(
                label=f"{row['event_name'][:55]}",
                description=f"{row['market_name'] or row['market_id']}: {row['outcome_name'] or row['outcome_id']} @ {row['price_decimal']:.2f}"[:100],
                value=str(i),
            ))
        super().__init__(placeholder="Choose a game and pick", options=options[:25])

    async def callback(self, interaction: discord.Interaction) -> None:
        parent = self.parent_picker
        candidate = parent.visible[int(self.values[0])]
        if any(row["fixture_id"] == candidate["fixture_id"] for row in parent.legs):
            await interaction.response.send_message(
                "Only one pick per game is supported. Remove the existing leg first.",
                ephemeral=True,
            )
            return
        if len(parent.legs) >= 6:
            await interaction.response.send_message("Maximum six legs.", ephemeral=True)
            return
        parent.legs.append(candidate)
        parent.page = 0
        parent.refresh_components()
        await interaction.response.edit_message(embed=parent.render(), view=parent)


class ManualParlayView(discord.ui.View):
    def __init__(self, *, owner_id: int, store, eligible_books: set[str]):
        super().__init__(timeout=900)
        self.owner_id = owner_id
        self.rows = available_selections(store, eligible_books=eligible_books)
        self.legs: list[dict] = []
        self.sport = None
        self.book = None
        self.game = None
        self.category = None
        self.page = 0
        self.refresh_components()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.owner_id:
            return True
        await interaction.response.send_message("Open /rival for your own private slip.", ephemeral=True)
        return False

    @property
    def visible(self):
        rows = [r for r in self.rows if
                (self.sport is None or r["sport_name"] == self.sport) and
                (self.game is None or r["fixture_id"] == self.game) and
                (self.category is None or self._category(r) == self.category) and
                (self.book is None or r["bookmaker"] == self.book) and
                (not self.legs or r["bookmaker"] == self.legs[0]["bookmaker"])]
        return rows[self.page * 25:(self.page + 1) * 25]

    @staticmethod
    def _category(row):
        name = str(row.get("market_name") or row.get("market_id") or "").lower()
        if row.get("player_name") or "player" in name:
            return "Player Props"
        if "quarter" in name:
            return "Quarter Bets"
        if "half" in name:
            return "Half Bets"
        return "Full Game"

    def refresh_components(self):
        self.clear_items()
        if self.rows:
            sports = sorted({r["sport_name"] for r in self.rows if r["sport_name"]})
            self.add_item(_Filter(self, "sport", sports))
            books = sorted({r["bookmaker"] for r in self.rows})
            if self.game is None:
                self.add_item(_Filter(self, "book", books))
            games = {}
            for r in self.rows:
                if (self.sport is None or r["sport_name"] == self.sport) and (self.book is None or r["bookmaker"] == self.book):
                    games.setdefault(r["fixture_id"], r["event_name"])
            if games and self.game is None:
                self.add_item(_Filter(self, "game", list(games.items())))
            if self.game is not None:
                categories = sorted({self._category(r) for r in self.rows if r["fixture_id"] == self.game})
                self.add_item(_Filter(self, "category", categories))
            if self.game is not None and self.category is not None and self.visible:
                self.add_item(_Pick(self))
        self.add_item(_Action(self, "Previous", "prev"))
        self.add_item(_Action(self, "Next", "next"))
        self.add_item(_Action(self, "Remove Last", "remove"))
        self.add_item(_Action(self, "Clear", "clear"))

    def render(self) -> discord.Embed:
        lines = [
            "Choose sport → sportsbook → game → bet category → pick.",
            "All prices come from cached observations. No API calls are made by this menu.",
            "Market-implied percentages are **not** model predictions.",
        ]
        for i, row in enumerate(self.legs, 1):
            lines.append(
                f"{i}. **{row['event_name']}** | {row['market_name'] or row['market_id']}: "
                f"{row['outcome_name'] or row['outcome_id']} | "
                f"{row['price_decimal']:.2f} | {row['implied_probability']:.1%} implied"
            )
        if self.legs:
            slip = selected_slip(self.legs)
            lines.append(
                f"**Combined odds:** {slip['combined_decimal_odds']:.2f} | "
                f"**Combined implied probability:** {slip['combined_implied_probability']:.1%}\n"
                "Independent-fixture assumption; not a calibrated win forecast."
            )
        if not self.rows:
            lines.append("No fresh eligible upcoming prices. PASS; try again after the next scheduled refresh.")
        elif not self.visible:
            lines.append("No matching selections. Try another game or category.")
        if self.rows:
            lines.append(f"**Available:** {len({r['fixture_id'] for r in self.rows})} games, "
                         f"{len({r['sport_name'] for r in self.rows})} sports, "
                         f"{len({r['bookmaker'] for r in self.rows})} sportsbooks.")
        return discord.Embed(title="Build Your Own Parlay", description="\n\n".join(lines)[:4000])

    async def redraw(self, interaction):
        self.refresh_components()
        await interaction.response.edit_message(embed=self.render(), view=self)


class _Filter(discord.ui.Select):
    def __init__(self, parent: ManualParlayView, kind: str, values: list[str]):
        self.parent_picker = parent
        self.kind = kind
        super().__init__(
            placeholder={"sport": "1. Choose sport", "book": "2. Choose sportsbook", "game": "3. Choose game", "category": "4. Choose bet category"}[kind],
            options=[discord.SelectOption(label="All", value="__all__")]
                    + [discord.SelectOption(label=(v[1] if kind == "game" else v)[:100], value=(v[0] if kind == "game" else v)) for v in values[:24]],
        )

    async def callback(self, interaction):
        parent = self.parent_picker
        value = None if self.values[0] == "__all__" else self.values[0]
        if self.kind == "sport":
            parent.sport = value
        elif self.kind == "book":
            if parent.legs and value not in (None, parent.legs[0]["bookmaker"]):
                await interaction.response.send_message(
                    "Clear your slip before switching sportsbooks.", ephemeral=True,
                )
                return
            parent.book = value
        elif self.kind == "game":
            parent.game = value
            parent.category = None
        elif self.kind == "category":
            parent.category = value
        if self.kind in ("sport", "book"):
            parent.game = None
            parent.category = None
        parent.page = 0
        await parent.redraw(interaction)


class _Action(discord.ui.Button):
    def __init__(self, parent: ManualParlayView, label: str, action: str):
        self.parent_picker = parent
        self.action = action
        super().__init__(label=label, style=discord.ButtonStyle.secondary)

    async def callback(self, interaction):
        parent = self.parent_picker
        if self.action == "remove" and parent.legs:
            parent.legs.pop()
        elif self.action == "clear":
            parent.legs.clear()
        elif self.action == "prev":
            parent.page = max(0, parent.page - 1)
        elif self.action == "next":
            parent.page += 1
            if not parent.visible:
                parent.page -= 1
        await parent.redraw(interaction)
