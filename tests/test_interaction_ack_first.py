"""Every Discord button, select and modal entry point acknowledges before work.

Discord fails an interaction ("This interaction failed") when it is not
acknowledged within 3 seconds. A database read or network call before the
acknowledgement can take longer under load, so the very first await of each
entry point must be the acknowledgement itself, or a hand-off that receives
the interaction (and acknowledges first, checked separately by its own tests).
"""
from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src/parlay_bot/discord_app"
ACK = ("interaction.response.",)


def _entry_points(tree):
    for node in ast.walk(tree):
        if not isinstance(node, ast.AsyncFunctionDef):
            continue
        decorated = any(
            "ui.button" in ast.unparse(d) or "ui.select" in ast.unparse(d)
            for d in node.decorator_list
        )
        if decorated or node.name in ("callback", "on_submit"):
            yield node


def _first_await(node):
    awaits = [n for n in ast.walk(node) if isinstance(n, ast.Await)]
    return min(awaits, key=lambda n: (n.lineno, n.col_offset)) if awaits else None


def test_entry_points_acknowledge_before_any_other_await():
    offenders = []
    for path in sorted(SRC.rglob("*.py")):
        for node in _entry_points(ast.parse(path.read_text())):
            first = _first_await(node)
            if first is None:
                continue
            call = first.value
            text = ast.unparse(call)
            if any(text.startswith(prefix) for prefix in ACK):
                continue
            # A hand-off that receives the interaction acknowledges itself.
            if isinstance(call, ast.Call) and any(
                isinstance(a, ast.Name) and a.id == "interaction" for a in call.args
            ):
                continue
            offenders.append(f"{path.name}:{node.lineno} {node.name}: {text[:80]}")
    assert not offenders, offenders
