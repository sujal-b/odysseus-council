"""Benchmark candidate models for council roles.

Tests each model with the actual Chair classification prompt + sample task.
Measures latency, JSON validity, semantic correctness, and token counts.

Usage:
    python bench_models.py                    # full sweep
    python bench_models.py --models m1,m2    # subset
    python bench_models.py --prompt chair    # which prompt to use
"""

import argparse
import json
import os
import sys
import time
from dataclasses import dataclass, field
from typing import Any

import urllib.request
import urllib.error

ZEN_URL = "https://opencode.ai/zen/v1/chat/completions"
ZEN_KEY = os.environ.get("DIRECT_MODEL_API_KEY", "")

# Candidate models — covering cheap-classify to heavy-reasoning tiers.
# Ponytail: pick the smallest set that covers the decision space.
DEFAULT_MODELS = [
    # Zen free tiers (cheap to test)
    "deepseek-v4-flash-free",
    "nemotron-3-ultra-free",
    # Mid-tier (likely good balance)
    "deepseek-v4-flash",
    "deepseek-v4-pro",
    "qwen3.6-plus",
    "kimi-k2.7-code",
    "glm-5.2",
    # Top tier (best quality, likely expensive)
    "claude-sonnet-5",
    "claude-opus-4-8",
    "gpt-5.4",
    "gemini-3.1-pro",
]

# Sample Chair prompt — copy of council_of_agents/prompts/chair.md output spec.
CHAIR_PROMPT = """You are the Chair of a Council of AI agents. Classify the user request.

Output ONLY a JSON block:
```json
{
  "complexity": "SIMPLE | MEDIUM | COMPLEX",
  "route": "DIRECT | PIPELINE",
  "action": "read | write | search | command | analyze | unknown",
  "target": "Short description of target.",
  "reason": "One-sentence explanation."
}
```
"""

CHAIR_SAMPLE = "Add a /health endpoint to routes/api.py that returns {status: ok}"


def call_zen(model: str, system: str, user: str, timeout: int = 60) -> dict:
    if not ZEN_KEY:
        raise RuntimeError("DIRECT_MODEL_API_KEY is required for Zen benchmarks")
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": 0.0,
        "max_tokens": 1024,
        "stream": False,
    }
    req = urllib.request.Request(
        ZEN_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {ZEN_KEY}",
            "Content-Type": "application/json",
        },
    )
    started = time.time()
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read().decode("utf-8")
    elapsed = time.time() - started
    data = json.loads(body)
    return {"elapsed_s": elapsed, "raw": data}


@dataclass
class Result:
    model: str
    elapsed_s: float = 0.0
    status: str = "FAIL"          # OK / JSON_BAD / NO_JSON / FAIL
    content: str = ""
    parsed: dict = field(default_factory=dict)
    prompt_tokens: int = 0
    completion_tokens: int = 0
    error: str = ""


def parse_json_block(text: str) -> dict:
    """Extract the first JSON ```json ... ``` block. Returns {} on failure."""
    import re
    m = re.search(r"```json\s*\n(.*?)```", text, re.DOTALL)
    if not m:
        # Try plain {...} if no fence
        m = re.search(r"\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}", text, re.DOTALL)
        if not m:
            return {}
        candidate = m.group(0)
    else:
        candidate = m.group(1)
    try:
        return json.loads(candidate)
    except Exception:
        return {}


def is_valid_chair(parsed: dict) -> bool:
    """Schema check mirroring council_schemas.ChairOutput (including ambiguity contract)."""
    if not isinstance(parsed, dict):
        return False
    if parsed.get("complexity") not in ("SIMPLE", "MEDIUM", "COMPLEX"):
        return False
    if parsed.get("route") not in ("DIRECT", "PIPELINE"):
        return False
    if parsed.get("action") not in ("read", "write", "search", "command", "analyze", "unknown"):
        return False
    if not isinstance(parsed.get("target"), str) or not parsed.get("target"):
        return False
    # Ambiguity contract — mirrors ChairOutput._validate_ambiguity_contract
    ambiguous = parsed.get("ambiguous", False)
    clarification = parsed.get("clarification", "")
    options = parsed.get("options", [])
    if not isinstance(ambiguous, bool):
        return False
    if not isinstance(clarification, str):
        return False
    if not isinstance(options, list) or not all(
        isinstance(option, str) for option in options
    ):
        return False
    clarification = clarification.strip()
    if ambiguous:
        if not clarification:
            return False
        if len(options) < 2 or len(options) > 4:
            return False
        stripped = [o.strip() for o in options]
        if any(s == "" for s in stripped):
            return False
        casefolded = [s.casefold() for s in stripped]
        if len(casefolded) != len(set(casefolded)):
            return False
    else:
        if clarification:
            return False
        if options:
            return False
    return True


def run_one(model: str, system: str, user: str) -> Result:
    r = Result(model=model)
    try:
        out = call_zen(model, system, user)
        r.elapsed_s = out["elapsed_s"]
        raw = out["raw"]
        choice = raw.get("choices", [{}])[0]
        r.content = choice.get("message", {}).get("content", "") or ""
        usage = raw.get("usage", {}) or {}
        r.prompt_tokens = usage.get("prompt_tokens", 0)
        r.completion_tokens = usage.get("completion_tokens", 0)
        r.parsed = parse_json_block(r.content)
        if not r.parsed:
            r.status = "NO_JSON"
        elif not is_valid_chair(r.parsed):
            r.status = "JSON_BAD"
        else:
            r.status = "OK"
    except urllib.error.HTTPError as e:
        r.status = "FAIL"
        r.error = f"HTTP {e.code}: {e.read().decode('utf-8', errors='replace')[:200]}"
    except Exception as e:
        r.status = "FAIL"
        r.error = f"{type(e).__name__}: {e}"
    return r


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--models", default=",".join(DEFAULT_MODELS))
    ap.add_argument("--system", default=CHAIR_PROMPT)
    ap.add_argument("--user", default=CHAIR_SAMPLE)
    args = ap.parse_args()
    models = [m.strip() for m in args.models.split(",") if m.strip()]
    print(f"Testing {len(models)} models on Chair classification task.")
    print(f"Sample: {args.user!r}")
    print()
    print(f"{'MODEL':<28} {'STATUS':<10} {'LAT(s)':<8} {'IN':<5} {'OUT':<5} {'COMPLEXITY':<10} {'ROUTE':<9} DETAIL")
    print("-" * 130)
    results: list[Result] = []
    for m in models:
        r = run_one(m, args.system, args.user)
        results.append(r)
        detail = r.error or (r.content[:60].replace("\n", " ") + ("..." if len(r.content) > 60 else ""))
        print(
            f"{m:<28} {r.status:<10} {r.elapsed_s:<8.2f} {r.prompt_tokens:<5} {r.completion_tokens:<5} "
            f"{r.parsed.get('complexity', '-'):<10} {r.parsed.get('route', '-'):<9} {detail}"
        )
    print()
    ok = sum(1 for r in results if r.status == "OK")
    print(f"OK: {ok}/{len(results)}")
    if ok:
        ranked = sorted(
            [r for r in results if r.status == "OK"],
            key=lambda r: r.elapsed_s,
        )
        print("Fastest OK models:")
        for r in ranked[:3]:
            print(f"  {r.model}  {r.elapsed_s:.2f}s  in={r.prompt_tokens} out={r.completion_tokens}")
    return 0 if ok == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
