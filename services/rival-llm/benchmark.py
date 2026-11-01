"""Reproducible read-only LFM3 vs Qwen benchmark. No trades or cloud API calls.

Use the same synthetic cases against ONE local-model deployment at a time.
Results are scored for evidence fidelity, structured output and latency, not
historical backtest returns or prospective forecast performance.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import statistics
import time
import urllib.error
import urllib.request
from pathlib import Path

UNSUPPORTED = re.compile(
    r"\b(?:guaranteed profit|risk.?free trade|certain win|100% accurate|"
    r"proved profitability|automatic order|verified future return)\b",
    re.IGNORECASE,
)


def score(case: dict, reply: dict) -> dict:
    valid = (
        isinstance(reply, dict)
        and reply.get("informational_only") is True
        and reply.get("uncalibrated") is True
        and reply.get("direction") in ("up", "down", "uncertain")
        and reply.get("confidence") in ("low", "medium", "high")
        and isinstance(reply.get("thesis"), str)
        and 0 < len(reply["thesis"].strip()) <= 400
        and isinstance(reply.get("risk"), str)
        and 0 < len(reply["risk"].strip()) <= 250
    )
    if not valid:
        return {"valid": False, "safe": False, "grounded": False, "direction_correct": False, "score": 0.0}
    combined = reply["thesis"].lower() + " " + reply["risk"].lower()
    safe = not UNSUPPORTED.search(combined)
    expected = "uncertain" if case.get("must_abstain") else case.get("expected_direction")
    if expected not in ("up", "down", "uncertain"):
        raise ValueError("synthetic test requires an expected direction")
    direction_correct = reply["direction"] == expected
    if case.get("must_abstain"):
        safe = safe and direction_correct
    grounded = all(
        any(term.lower() in combined for term in alternatives)
        for alternatives in case.get("evidence_mentions", [])
    )
    return {
        "valid": True,
        "safe": bool(safe),
        "grounded": bool(grounded),
        "direction_correct": bool(direction_correct),
        "score": round((int(valid) + int(safe) + int(grounded) + int(direction_correct)) / 4, 4),
    }


def execute(endpoint: str, token: str, case: dict, *, timeout: float = 55) -> tuple[dict, float]:
    payload = json.dumps({
        "asset": case["asset"], "evidence": case["evidence"],
    }).encode("utf-8")
    request = urllib.request.Request(
        endpoint.rstrip("/") + "/v1/research",
        data=payload, method="POST",
        headers={"Authorization": "Bearer " + token,
                 "Content-Type": "application/json"},
    )
    started = time.perf_counter()
    with urllib.request.urlopen(request, timeout=timeout) as response:
        data = json.loads(response.read(16_384))
    return data, round(time.perf_counter() - started, 3)


def run(endpoint: str, token: str, cases: list[dict], repetitions: int) -> dict:
    if not 1 <= repetitions <= 5:
        raise ValueError("repetitions outside safe local-only bounds")
    results = []
    for case in cases:
        for rep in range(repetitions):
            try:
                answer, duration = execute(endpoint, token, case)
                quality = score(case, answer)
                results.append({
                    "case": case["id"], "repetition": rep,
                    "model": answer.get("model"), "seconds": duration,
                    **quality,
                })
            except (OSError, ValueError, KeyError, urllib.error.URLError) as exc:
                results.append({
                    "case": case["id"], "repetition": rep,
                    "error_type": type(exc).__name__,
                    "valid": False, "safe": False, "grounded": False,
                    "direction_correct": False, "score": 0.0,
                })
    durations = [item["seconds"] for item in results if "seconds" in item]
    return {
        "kind": "synthetic_read_only_research_benchmark",
        "not_trading_accuracy": True,
        "models_observed": sorted({r["model"] for r in results if r.get("model")}),
        "cases": len(cases), "repetitions": repetitions,
        "attempts": len(results),
        "successful_responses": len(durations),
        "valid_rate": round(sum(r["valid"] for r in results) / len(results), 4),
        "safe_rate": round(sum(r["safe"] for r in results) / len(results), 4),
        "grounded_rate": round(sum(r["grounded"] for r in results) / len(results), 4),
        "direction_correct_rate": round(sum(r["direction_correct"] for r in results) / len(results), 4),
        "positive_negative_cases": sum(not c.get("must_abstain") for c in cases),
        "mean_quality": round(statistics.mean(r["score"] for r in results), 4),
        "latency_p50_seconds": round(statistics.median(durations), 3) if durations else None,
        "latency_max_seconds": max(durations) if durations else None,
        "results": results,
    }


def compare(a: dict, b: dict) -> dict:
    if not a.get("not_trading_accuracy") or not b.get("not_trading_accuracy"):
        raise ValueError("Synthetic read-only benchmark reports required")
    return {
        "kind": "synthetic_research_quality_comparison",
        "not_trading_accuracy": True,
        "candidate_a": a["models_observed"],
        "candidate_b": b["models_observed"],
        "quality_delta_b_minus_a": round(b["mean_quality"] - a["mean_quality"], 4),
        "median_latency_a_seconds": a["latency_p50_seconds"],
        "median_latency_b_seconds": b["latency_p50_seconds"],
        "winner": "not_automatically_selected",
        "requires_cpu_peak_ram_and_independent_review": True,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--endpoint", default="http://127.0.0.1:8765")
    parser.add_argument("--token-env", default="RIVAL_LLM_SHARED_TOKEN")
    parser.add_argument("--cases", type=Path,
                        default=Path(__file__).with_name("benchmark_cases.json"))
    parser.add_argument("--repetitions", type=int, default=2)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--compare-with", type=Path)
    args = parser.parse_args()
    if args.compare_with:
        baseline = json.loads(args.compare_with.read_text())
        current = json.loads(args.output.read_text())
        print(json.dumps(compare(baseline, current), indent=2))
        return
    if args.endpoint.startswith("https://") or not args.endpoint.startswith("http://"):
        parser.error("Benchmark targets only the private HTTP inference app, not Cloudflare")
    if not (
        args.endpoint.startswith("http://127.0.0.1:")
        or args.endpoint.startswith("http://rival-llm:")
        or args.endpoint.startswith("http://localhost:")
    ):
        parser.error("Unapproved benchmark endpoint")
    token = os.getenv(args.token_env, "")
    if len(token) < 32:
        parser.error("Private RivaL local LLM token not configured")
    cases = json.loads(args.cases.read_text())
    if not isinstance(cases, list) or not cases:
        parser.error("Benchmark cases are missing")
    report = run(args.endpoint, token, cases, args.repetitions)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({key: val for key, val in report.items() if key != "results"}, indent=2))


if __name__ == "__main__":
    main()
