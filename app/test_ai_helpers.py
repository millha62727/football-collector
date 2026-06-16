"""Tests for AI pipeline pure helpers (Hướng 4 — test coverage).

Covers (theo risk-order):
  P0: _split_quarter, _cover_score, _ou_score  (tầng thấp nhất, sai ở đây
      thì tất cả aggregate Layer B sai mà không warning)
  P0: _parse_completion                        (silent failure với
      reasoning_content only)
  P1: _parse_json_loose
  P2: _norm_tags, _trim_stats_for_prompt

Run:
    python -m app.test_ai_helpers
hoặc
    python app/test_ai_helpers.py
"""
from __future__ import annotations

import os
import sys

# Allow `python app/test_ai_helpers.py` from any cwd
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.analyzer import ai_client  # noqa: E402
from app.analyzer import ai_pattern  # noqa: E402
from app.database import _cover_score, _ou_score, _split_quarter  # noqa: E402


# ---- tiny assert helpers (không dùng pytest để giữ convention project) ----

_FAILURES: list[str] = []


def _check(cond: bool, label: str, detail: str = "") -> None:
    if cond:
        print(f"  ✓ {label}")
    else:
        msg = f"  ✗ {label}" + (f" — {detail}" if detail else "")
        print(msg)
        _FAILURES.append(msg)


# =========================================================================
# P0: _split_quarter — tầng thấp nhất, sai ở đây lan ra tất cả
# =========================================================================

def test_split_quarter() -> None:
    print("\n[P0] _split_quarter")
    # whole / half lines → [line] (không split)
    for line in [-0.5, 0.0, 0.5, 1.0, 1.5, -2.0]:
        out = _split_quarter(line)
        _check(out == [line], f"whole/half {line} → [{line}]", f"got {out}")

    # quarter lines → split thành 2
    _check(_split_quarter(-0.25) == [-0.5, 0.0], "quarter -0.25 → [-0.5, 0.0]")
    _check(_split_quarter(-0.75) == [-1.0, -0.5], "quarter -0.75 → [-1.0, -0.5]")
    _check(_split_quarter(0.25) == [0.0, 0.5], "quarter +0.25 → [0.0, 0.5]")
    _check(_split_quarter(0.75) == [0.5, 1.0], "quarter +0.75 → [0.5, 1.0]")
    _check(_split_quarter(-1.25) == [-1.5, -1.0], "quarter -1.25 → [-1.5, -1.0]")


# =========================================================================
# P0: _cover_score — Asian handicap averaging
# =========================================================================

def test_cover_score() -> None:
    print("\n[P0] _cover_score")
    # Whole line, favorite win by exactly line → push (0.5)
    _check(_cover_score(-1.0, 1.0) == 0.5, "home fav -1.0, win by 1 → push")
    # Whole line, favorite win by more → win (1.0)
    _check(_cover_score(-1.0, 2.0) == 1.0, "home fav -1.0, win by 2 → win")
    # Whole line, favorite win by less → loss (0.0)
    _check(_cover_score(-1.0, 0.0) == 0.0, "home fav -1.0, draw → loss")

    # Half line, favorite win by 0 → loss (margin=0, line=-0.5, eff=-0.5)
    _check(_cover_score(-0.5, 0.0) == 0.0, "home fav -0.5, draw → loss")
    # Half line, favorite win by 1 → win
    _check(_cover_score(-0.5, 1.0) == 1.0, "home fav -0.5, win by 1 → win")

    # Quarter line -0.25, win by 0 → half loss (avg of push + loss)
    # parts: -0.5, 0.0; eff for 0: 0+(-0.5)=-0.5 (loss), 0+0=0 (push)
    # avg: (0 + 0.5) / 2 = 0.25
    s = _cover_score(-0.25, 0)
    _check(abs(s - 0.25) < 1e-9, f"home fav -0.25, draw → 0.25 (got {s})")

    # Quarter line -0.75, win by 1 → half win (avg of win + push)
    # parts: -1.0, -0.5; eff: 1-1=0 (push), 1-0.5=0.5 (win)
    # avg: (0.5 + 1.0) / 2 = 0.75
    s = _cover_score(-0.75, 1)
    _check(abs(s - 0.75) < 1e-9, f"home fav -0.75, win by 1 → 0.75 (got {s})")

    # Away favorite (line positive from home's POV, here line=-0.5 means home fav)
    # margin=0, away fav -0.5 means away needs to give 0.5 → call with margin from away POV
    # For simplicity: margin=(home-away), line=(from home POV, negative if home fav)
    # Here margin=0 (draw), line=-0.5 (home fav) → home loses (-0.5)
    # We can also test away fav: line=0.5 means away fav
    # margin=0 (draw), away fav by 0.5 → away loses (away-margin=0-0=-0.5)
    # Hmm, function signature: `margin = (that side's score) − (opponent score)`
    # Caller decides. Let's test with the documented semantics for home fav only.


# =========================================================================
# P0: _ou_score — Over/Under
# =========================================================================

def test_ou_score() -> None:
    print("\n[P0] _ou_score")
    # Whole line, exactly total → push
    _check(_ou_score(2.5, 2) == 0.0, "OU 2.5, total 2 → under")
    _check(_ou_score(2.5, 3) == 1.0, "OU 2.5, total 3 → over")
    _check(_ou_score(2.5, 2.5) == 0.5, "OU 2.5, total 2.5 → push")

    # Half line, total=2
    _check(_ou_score(2.0, 2) == 0.5, "OU 2.0, total 2 → push")
    _check(_ou_score(2.0, 3) == 1.0, "OU 2.0, total 3 → over")
    _check(_ou_score(2.0, 1) == 0.0, "OU 2.0, total 1 → under")

    # Quarter line 2.25, total=2 → half under (avg of push + under)
    s = _ou_score(2.25, 2)
    _check(abs(s - 0.25) < 1e-9, f"OU 2.25, total 2 → 0.25 (got {s})")

    # Quarter line 2.75, total=3 → half over
    s = _ou_score(2.75, 3)
    _check(abs(s - 0.75) < 1e-9, f"OU 2.75, total 3 → 0.75 (got {s})")


# =========================================================================
# P0: _parse_completion — silent failure với reasoning_content only
# =========================================================================

def test_parse_completion_reasoning_only() -> None:
    """Quan trọng nhất: nếu model chỉ emit reasoning_content, content='' không crash."""
    print("\n[P0] _parse_completion (reasoning-only)")
    sse = (
        'data: {"choices":[{"delta":{"reasoning_content":"thinking..."}}]}\n\n'
        'data: {"choices":[{"delta":{"reasoning_content":" more thinking"}}]}\n\n'
        'data: {"choices":[{"finish_reason":"stop","delta":{}}]}\n\n'
        'data: [DONE]\n\n'
    )
    out = ai_client._parse_completion(sse)
    msg = out["choices"][0]["message"]
    _check(msg["content"] == "", f"content should be '' (got {msg['content']!r})")
    _check(
        "thinking" in msg.get("reasoning_content", ""),
        f"reasoning_content preserved (got {msg.get('reasoning_content', '')!r})",
    )
    _check(out["choices"][0].get("finish_reason") == "stop", "finish_reason preserved")


def test_parse_completion_normal_text() -> None:
    """Happy path: chỉ content, không reasoning."""
    print("\n[P0] _parse_completion (normal text)")
    sse = (
        'data: {"choices":[{"delta":{"content":"Hello"}}]}\n\n'
        'data: {"choices":[{"delta":{"content":" world"}}]}\n\n'
        'data: {"choices":[{"finish_reason":"stop","delta":{}}]}\n\n'
        'data: [DONE]\n\n'
    )
    out = ai_client._parse_completion(sse)
    msg = out["choices"][0]["message"]
    _check(msg["content"] == "Hello world", f"content joined (got {msg['content']!r})")
    _check(msg.get("reasoning_content", "") == "", "no reasoning_content")


def test_parse_completion_mixed() -> None:
    """Model emit cả reasoning lẫn content (DeepSeek v4 / Claude thinking)."""
    print("\n[P0] _parse_completion (mixed)")
    sse = (
        'data: {"choices":[{"delta":{"reasoning_content":"step 1"}}]}\n\n'
        'data: {"choices":[{"delta":{"content":"Answer: "}}]}\n\n'
        'data: {"choices":[{"delta":{"reasoning_content":"step 2"}}]}\n\n'
        'data: {"choices":[{"delta":{"content":"42"}}]}\n\n'
        'data: {"choices":[{"finish_reason":"stop","delta":{}}]}\n\n'
        'data: [DONE]\n\n'
    )
    out = ai_client._parse_completion(sse)
    msg = out["choices"][0]["message"]
    _check(msg["content"] == "Answer: 42", f"content split across chunks (got {msg['content']!r})")
    _check(
        msg.get("reasoning_content", "") == "step 1step 2",
        f"reasoning_content ordered (got {msg.get('reasoning_content', '')!r})",
    )


def test_parse_completion_no_choices_raises() -> None:
    """Edge case: stream kết thúc không có choices (lỗi upstream) → raise.
    Caller (analyze_and_store) catch và lưu parse_ok=False, không crash.
    """
    print("\n[P0] _parse_completion (no choices → raises RuntimeError)")
    sse = 'data: [DONE]\n\n'
    raised = False
    try:
        ai_client._parse_completion(sse)
    except RuntimeError as e:
        raised = True
        _check("nhận dạng" in str(e) or "DONE" in str(e), f"error message informative (got {e!r})")
    _check(raised, "RuntimeError raised (not silent failure)")


# =========================================================================
# P1: _parse_json_loose
# =========================================================================

def test_parse_json_loose_clean() -> None:
    print("\n[P1] _parse_json_loose (clean JSON)")
    out = ai_pattern._parse_json_loose('{"a": 1, "b": [2,3]}')
    _check(out == {"a": 1, "b": [2, 3]}, f"clean JSON parsed (got {out})")


def test_parse_json_loose_fenced() -> None:
    print("\n[P1] _parse_json_loose (markdown fence)")
    text = 'Some prose\n```json\n{"a": 1}\n```\nMore prose'
    out = ai_pattern._parse_json_loose(text)
    _check(out == {"a": 1}, f"fence JSON parsed (got {out})")


def test_parse_json_loose_outermost_braces() -> None:
    print("\n[P1] _parse_json_loose (outermost braces fallback)")
    text = 'Some prose {"a": 1, "b": 2} trailing'
    out = ai_pattern._parse_json_loose(text)
    _check(out == {"a": 1, "b": 2}, f"outermost braces extracted (got {out})")


def test_parse_json_loose_empty() -> None:
    print("\n[P1] _parse_json_loose (empty input)")
    _check(ai_pattern._parse_json_loose("") is None, "'' → None")
    _check(ai_pattern._parse_json_loose(None) is None, "None → None")  # type: ignore[arg-type]


def test_parse_json_loose_nested() -> None:
    print("\n[P1] _parse_json_loose (nested braces)")
    text = '{"outer": {"inner": [1, 2, {"deep": true}]}}'
    out = ai_pattern._parse_json_loose(text)
    _check(
        out == {"outer": {"inner": [1, 2, {"deep": True}]}},
        f"nested parsed (got {out})",
    )


# =========================================================================
# P2: _norm_tags
# =========================================================================

def test_norm_tags() -> None:
    print("\n[P2] _norm_tags")
    # None input
    _check(ai_pattern._norm_tags(None) == [], "None → []")
    # Empty tags
    _check(ai_pattern._norm_tags({"tags": []}) == [], "empty tags → []")
    # Normalize: spaces → underscores, lowercase
    out = ai_pattern._norm_tags({"tags": ["Fav No Cover", "BTTS hit", "fav_no_cover"]})
    _check(out == ["fav_no_cover", "btts_hit"], f"normalized (got {out})")
    # Cap at 12
    out = ai_pattern._norm_tags({"tags": [f"tag_{i}" for i in range(20)]})
    _check(len(out) == 12, f"capped at 12 (got {len(out)})")
    # Drop empty
    out = ai_pattern._norm_tags({"tags": ["valid", "", "  ", "also_valid"]})
    _check(out == ["valid", "also_valid"], f"empty dropped (got {out})")


# =========================================================================
# P2: _trim_stats_for_prompt
# =========================================================================

def test_trim_stats_for_prompt() -> None:
    print("\n[P2] _trim_stats_for_prompt")
    # Build a stats dict similar to compute_pattern_stats output
    stats = {
        "n_total": 1000,
        "filters": {"open_hc": None, "open_ou": None, "prestigious_only": False},
        "bucket": {
            "n": 100,
            "fav_cover_rate": 0.5,
            "score_dist": [("1-1", 100), ("2-1", 80), ("0-0", 50), ("3-1", 30), ("0-1", 20),
                           ("2-2", 15), ("1-0", 10), ("3-0", 5)],  # 8 entries
        },
        "overall": {
            "n": 1000,
            "fav_cover_rate": 0.5,
            "score_dist": [("1-1", 100), ("2-1", 80), ("0-0", 50)],
        },
        "by_open_hc": [
            {"open_hc": "-0.5", "n": 200},
            {"open_hc": "-1.0", "n": 150},
            {"open_hc": "+0.5", "n": 100},
            {"open_hc": "-1.5", "n": 50},  # should be dropped (top 3)
        ],
    }
    out = ai_pattern._trim_stats_for_prompt(stats)

    # Top-level keys preserved
    for k in ("n_total", "filters", "bucket", "overall", "top_open_hc_buckets"):
        _check(k in out, f"key {k!r} preserved")

    # bucket score_dist kept ≤ 5 (slim with keep_scores=True)
    bucket_sd = out["bucket"].get("score_dist")
    if bucket_sd is not None:
        _check(len(bucket_sd) <= 5, f"bucket score_dist ≤ 5 (got {len(bucket_sd)})")

    # overall score_dist DROPPED (slim with keep_scores=False)
    _check("score_dist" not in out["overall"], "overall score_dist dropped (save tokens)")

    # by_open_hc only top 3
    _check(
        len(out["top_open_hc_buckets"]) == 3,
        f"top_open_hc_buckets capped at 3 (got {len(out['top_open_hc_buckets'])})",
    )


# =========================================================================
# Driver
# =========================================================================

def main() -> int:
    print("=" * 70)
    print("AI helpers test suite — Hướng 4 (test coverage)")
    print("=" * 70)

    test_split_quarter()
    test_cover_score()
    test_ou_score()
    test_parse_completion_reasoning_only()
    test_parse_completion_normal_text()
    test_parse_completion_mixed()
    test_parse_completion_no_choices_raises()
    test_parse_json_loose_clean()
    test_parse_json_loose_fenced()
    test_parse_json_loose_outermost_braces()
    test_parse_json_loose_empty()
    test_parse_json_loose_nested()
    test_norm_tags()
    test_trim_stats_for_prompt()

    print("\n" + "=" * 70)
    if _FAILURES:
        print(f"FAILED: {len(_FAILURES)} assertion(s)")
        for f in _FAILURES:
            print(f)
        return 1
    print("ALL PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
