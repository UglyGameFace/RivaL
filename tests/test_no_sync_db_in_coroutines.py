"""No coroutine may open a database connection on the event loop.

These coroutines run on the Discord event loop. A synchronous PostgreSQL
round trip inside one blocks every command and gateway heartbeat; the
production stall watch caught exactly that in kalshi_updown.settle.
Database work goes through asyncio.to_thread (a nested sync def is fine).
"""
import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src" / "parlay_bot"


def test_no_store_connect_directly_inside_async_def():
    offenders = []
    for path in SRC.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for fn in ast.walk(tree):
            if not isinstance(fn, ast.AsyncFunctionDef):
                continue
            stack = list(ast.iter_child_nodes(fn))
            while stack:
                node = stack.pop()
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                    continue
                if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                        and node.func.attr == "connect"
                        and "store" in ast.unparse(node.func.value)):
                    offenders.append(f"{path.relative_to(SRC)}:{node.lineno} {fn.name}")
                stack.extend(ast.iter_child_nodes(node))
    assert offenders == []
