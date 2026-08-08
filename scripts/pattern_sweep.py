#!/usr/bin/env python3
"""Pattern sweep — batch-generate AI patterns for finished matches.

Picks up to N eligible FT matches that have no match_patterns row yet for the
configured model, runs grounded AI analysis on each (Layer C), and persists the
structured result. Idempotent (skips already-processed matches), bounded (hard
cap per run), and rate-limited (sleep between calls) so it never hammers the
ai-box endpoint or starves the 1.9 GB VPS.

Designed to be invoked from a host cron via:
    docker exec -i football_dashboard python scripts/pattern_sweep.py

Environment knobs (all optional):
    PATTERN_SWEEP_BATCH        max matches per run            (default 20)
    PATTERN_SWEEP_DELAY_S      seconds between AI calls       (default 2.0)
    PATTERN_SWEEP_PRESTIGIOUS  '1' = only prestigious leagues (default 1)
    PATTERN_SWEEP_MIN_SNAPS    min odds snapshots required    (default 4)
    PATTERN_SWEEP_PER_CALL_TO  per-match hard timeout seconds (default 120)

Exit 0 on a clean run (even with per-match failures, which are logged and
counted). Exit 1 only on a fatal setup error (AI not configured, import fail).
"""
from __future__ import annotations

import asyncio
import os
import sys
import time

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


async def _run() -> int:
    try:
        from app.analyzer import ai_client as AI
        from app.analyzer import ai_pattern as AIP
        from app.database import get_unprocessed_ft_matches, count_pattern_progress
    except Exception as exc:  # pragma: no cover
        print(f"[sweep] import failed: {exc!r}", file=sys.stderr)
        return 1

    if not AI.is_configured("cron"):
        print("[sweep] AI not configured for cron scope (cần AI_BASE_URL+AI_API_KEY+AI_MODEL_CRON/AI_MODEL) — skip", file=sys.stderr)
        return 1

    model = AI._model("cron")  # the canonical model string patterns are keyed on; cron scope reads AI_MODEL_CRON (with AI_MODEL legacy fallback)
    batch = max(1, _int("PATTERN_SWEEP_BATCH", 20))
    delay = max(0.0, _float("PATTERN_SWEEP_DELAY_S", 2.0))
    prestigious = os.getenv("PATTERN_SWEEP_PRESTIGIOUS", "1").strip() not in ("0", "false", "")
    min_snaps = max(1, _int("PATTERN_SWEEP_MIN_SNAPS", 4))
    per_call_to = max(30, _int("PATTERN_SWEEP_PER_CALL_TO", 120))

    matches = get_unprocessed_ft_matches(
        model, limit=batch, prestigious_only=prestigious, min_odds_snapshots=min_snaps,
    )
    progress = count_pattern_progress(model, prestigious_only=prestigious)
    print(f"[sweep] model={model} batch={batch} prestigious={prestigious} "
          f"| eligible={progress['eligible']} processed={progress['processed']} "
          f"pending={progress['pending']} | picked={len(matches)}")

    if not matches:
        print("[sweep] nothing to do")
        return 0

    # reasoning_effort flows through AI_REASONING_EFFORT env inside ai_client.chat()
    # (ai_pattern.analyze_match -> ai_client.chat picks it up automatically).
    # Concurrent workers (PATTERN_SWEEP_WORKERS, default 3) — aiohttp calls are
    # I/O-bound; the 1.9 GB VPS just waits on HTTP, semaphore keeps AI endpoint
    # load bounded. Unique(match_id, model) + upsert keeps reruns idempotent.
    workers = max(1, _int("PATTERN_SWEEP_WORKERS", 3))
    sem = asyncio.Semaphore(workers)
    counters = {"ok": 0, "empty": 0, "fail": 0, "done": 0}
    total = len(matches)
    c_lock = asyncio.Lock()

    async def _worker(m: dict) -> None:
        mid = m["id"]
        label = f"{m.get('competition','?')[:24]} | {m.get('home','?')} vs {m.get('away','?')}"
        async with sem:
            try:
                res = await asyncio.wait_for(
                    AIP.analyze_and_store(
                        mid,
                        prestigious_only=prestigious,
                        model=model,
                    ),
                    timeout=per_call_to,
                )
                async with c_lock:
                    counters["done"] += 1
                    n = counters["done"]
                    if res.get("parse_ok"):
                        counters["ok"] += 1
                        print(f"[sweep] {n}/{total} OK   {label} "
                              f"tags={res.get('tags')} conf={res.get('confidence')}", flush=True)
                    else:
                        counters["empty"] += 1
                        print(f"[sweep] {n}/{total} EMPTY {label} "
                              f"finish={res.get('finish_reason')} len={res.get('content_len')}",
                              file=sys.stderr, flush=True)
            except asyncio.TimeoutError:
                async with c_lock:
                    counters["done"] += 1
                    counters["fail"] += 1
                    print(f"[sweep] {counters['done']}/{total} TIMEOUT {label}",
                          file=sys.stderr, flush=True)
            except Exception as exc:
                async with c_lock:
                    counters["done"] += 1
                    counters["fail"] += 1
                    print(f"[sweep] {counters['done']}/{total} FAIL {label}: {exc!r}",
                          file=sys.stderr, flush=True)
            if delay:
                await asyncio.sleep(delay)

    await asyncio.gather(*(_worker(m) for m in matches))

    print(f"[sweep] done: ok={counters['ok']} empty={counters['empty']} "
          f"fail={counters['fail']} of {total}", flush=True)
    return 0


def main() -> int:
    t0 = time.time()
    try:
        rc = asyncio.run(_run())
    except KeyboardInterrupt:
        print("[sweep] interrupted", file=sys.stderr)
        return 130
    print(f"[sweep] elapsed {time.time() - t0:.1f}s")
    return rc


if __name__ == "__main__":
    sys.exit(main())
