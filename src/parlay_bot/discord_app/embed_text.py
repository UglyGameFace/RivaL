"""Word-boundary fitting for Discord embed text.

Moved here from ``home.py`` (audit Step 1, PR #476) so the fitter is a
leaf module — no ``parlay_bot`` imports of its own — and text builders in
``perpetuals/`` can use it without importing the whole ``discord_app.home``
module (which imports ``perpetuals`` itself). ``home.py`` re-exports both
names, so existing ``from parlay_bot.discord_app.home import ...`` imports
keep working.
"""

DISCORD_FIELD_LIMIT = 1024


def fit_embed_field_value(text: str, *, budget: int = DISCORD_FIELD_LIMIT) -> str:
    """Fit an embed field value to Discord's limit without cutting mid-word.

    The old pattern sliced at [:1024], which cut live numbers mid-word —
    the 2026-11-10 audit screenshot shows the regime field ending "A
    stable shadow score is not tradin". Lines are dropped from the tail
    (lowest priority last) until the value fits; only a single line that
    alone exceeds the budget is cut, at the last word boundary, with an
    ellipsis. Short values pass through unchanged.
    """
    if len(text) <= budget:
        return text
    lines = text.split("\n")
    while len(lines) > 1 and len("\n".join(lines)) > budget:
        lines.pop()
    fitted = "\n".join(lines)
    if len(fitted) <= budget:
        return fitted
    cut = fitted[: budget - 1].rsplit(" ", 1)[0]
    if not cut:
        cut = fitted[: budget - 1]
    return cut + "…"
