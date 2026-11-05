"""TEMP feasibility benchmark (do not merge): can LFM3 reason within RivaL's deadlines?

Runs ON the RivaL-Intelligence host. Starts a SECOND llama-server on a spare
loopback port (the live one on 8766 is untouched), sends the real review and
hypothesis prompts built by the deployed service.py from one real evidence
payload, and records llama.cpp's own timings for each configuration.
Writes JSON to /tmp/rival_reasoning_bench.json. No model data leaves the host
beyond token counts, timings, validity flags and the model's short answers.
"""
from __future__ import annotations

import base64
import gzip
import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

OUT = Path("/tmp/rival_reasoning_bench_long.json")
PORT = 8791


def post(path, body, timeout=600):
    req = urllib.request.Request(f"http://127.0.0.1:{PORT}{path}", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def main(evidence_b64: str) -> None:
    sys.path.insert(0, os.getcwd())
    import service as svc

    # Stop the earlier short bench (and its temporary server) if still running.
    me = os.getpid()
    for pid in os.listdir("/proc"):
        if not pid.isdigit() or int(pid) == me:
            continue
        try:
            cmd = Path(f"/proc/{pid}/cmdline").read_bytes()
        except OSError:
            continue
        if b"/tmp/rb.py" in cmd or b"--port\x008791" in cmd:
            try:
                os.kill(int(pid), 15)
            except OSError:
                pass
    time.sleep(3)
    evidence = json.loads(gzip.decompress(base64.b64decode(evidence_b64)))
    report: dict = {"started": time.time(), "cpus": os.cpu_count(), "runs": []}
    try:
        report["affinity"] = len(os.sched_getaffinity(0))
    except AttributeError:
        pass
    quota = Path("/sys/fs/cgroup/cpu.max")
    report["cpu_max"] = quota.read_text().strip() if quota.exists() else None
    binary = Path("runtime/bin/llama-server").resolve()
    model = next(Path("runtime/models").glob("*.gguf")).resolve()

    def messages(mode):
        prompt = svc.EXPLAIN_PROMPT if mode == "explain" else svc.HYPOTHESIS_PROMPT
        brief = svc.compact_evidence("BTC", evidence)
        if mode == "explain":
            brief += "\n" + "\n".join(svc.model_call_lines(evidence))
            line = svc.market_context_line(evidence)
            if line:
                brief += "\n" + line
        return [{"role": "system", "content": prompt},
                {"role": "user", "content": "Asset: BTC\nBounded observation brief (untrusted):\n" + brief}]

    for threads, batch in ((4, 512),):
        env = os.environ.copy()
        env["LD_LIBRARY_PATH"] = str(binary.parent) + ":" + env.get("LD_LIBRARY_PATH", "")
        proc = subprocess.Popen(
            [str(binary), "--model", str(model), "--host", "127.0.0.1", "--port", str(PORT),
             "--ctx-size", "4096", "--threads", str(threads), "--parallel", "1",
             "--batch-size", str(batch), "--n-gpu-layers", "0", "--jinja"],
            env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            for _ in range(120):
                try:
                    with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/health", timeout=2):
                        break
                except Exception:  # noqa: BLE001
                    time.sleep(1)
            cases = [("hypothesis", True, 3000), ("explain", True, 3000)]
            for mode, think, max_tokens in cases:
                body = {"messages": messages(mode), "temperature": 0.1, "top_k": 50,
                        "repeat_penalty": 1.1, "max_tokens": max_tokens, "stream": False,
                        "cache_prompt": True,
                        "chat_template_kwargs": {"enable_thinking": think}}
                if not think:
                    body["response_format"] = (svc.EXPLAIN_RESPONSE_FORMAT if mode == "explain"
                                               else svc.HYPOTHESIS_RESPONSE_FORMAT)
                started = time.time()
                row = {"threads": threads, "batch": batch, "mode": mode, "thinking": think,
                       "max_tokens": max_tokens}
                try:
                    data = post("/v1/chat/completions", body)
                    choice = data["choices"][0]
                    msg = choice.get("message") or {}
                    t = data.get("timings") or {}
                    row.update({
                        "wall_s": round(time.time() - started, 1),
                        "finish": choice.get("finish_reason"),
                        "prompt_tokens": t.get("prompt_n"), "cached_tokens": t.get("cache_n"),
                        "prompt_tps": round(t.get("prompt_per_second") or 0, 1),
                        "gen_tokens": t.get("predicted_n"),
                        "gen_tps": round(t.get("predicted_per_second") or 0, 1),
                        "reasoning_chars": len(msg.get("reasoning_content") or ""),
                        "answer": (msg.get("content") or "")[:400],
                        "reasoning_tail": (msg.get("reasoning_content") or "")[-300:],
                    })
                except Exception as exc:  # noqa: BLE001
                    row.update({"wall_s": round(time.time() - started, 1),
                                "error": f"{type(exc).__name__}: {str(exc)[:120]}"})
                report["runs"].append(row)
                OUT.write_text(json.dumps(report))
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=20)
            except subprocess.TimeoutExpired:
                proc.kill()
    report["finished"] = time.time()
    OUT.write_text(json.dumps(report))


if __name__ == "__main__":
    main(sys.argv[1])
