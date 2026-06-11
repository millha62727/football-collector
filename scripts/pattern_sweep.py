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

    ok = fail = empty = 0
    for i, m in enumerate(matches, 1):
        mid = m["id"]
        label = f"{m.get('competition','?')[:24]} | {m.get('home','?')} vs {m.get('away','?')}"
        try:
            res = await asyncio.wait_for(
                AIP.analyze_and_store(mid, prestigious_only=prestigious),
                timeout=per_call_to,
            )
            if res.get("parse_ok"):
                ok += 1
                print(f"[sweep] {i}/{len(matches)} OK   {label} "
                      f"tags={res.get('tags')} conf={res.get('confidence')}")
            else:
                empty += 1
                print(f"[sweep] {i}/{len(matches)} EMPTY {label} "
                      f"finish={res.get('finish_reason')} len={res.get('content_len')}",
                      file=sys.stderr)
        except asyncio.TimeoutError:
            fail += 1
            print(f"[sweep] {i}/{len(matches)} TIMEOUT {label}", file=sys.stderr)
        except Exception as exc:
            fail += 1
            print(f"[sweep] {i}/{len(matches)} FAIL {label}: {exc!r}", file=sys.stderr)
        if i < len(matches) and delay:
            await asyncio.sleep(delay)

    print(f"[sweep] done: ok={ok} empty={empty} fail={fail} of {len(matches)}")
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
