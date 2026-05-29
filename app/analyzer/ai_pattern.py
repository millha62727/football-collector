"""Grounded AI analysis for analyzer matches.

This module does NOT ask the model to invent statistics. It sends:
  1) deterministic features from parser.compute(), and
  2) empirical base-rates from database.compute_pattern_stats().
The LLM's job is to explain and synthesize, not to be the source of numbers.
"""
from __future__ import annotations

import json
import re
from typing import Any, Optional

from ..database import compute_pattern_stats
from . import ai_client as AI
from . import parser as P


def _first_present_milestone(result: dict[str, Any]) -> Optional[dict[str, Any]]:
    for m in result.get("milestones") or []:
        if m:
            return m
    return None


def _opening_ou_fallback(rows: list[dict[str, str]]) -> Optional[float]:
    """First non-zero O/U line from the opening snapshot.

    Milestone `c` is the mode of O/U over the 1H_2'..1H_5' window, which misses
    when DB odds history is sparse (a handful of snapshots). Fall back to the
    first row's line so the AI bucket aligns with Layer B (which keys off the
    opening snapshot too).
    """
    for r in rows:
        v = P.to_f(r.get("Over/Under Line", 0))
        if v and v > 0:
            return v
    return None


def build_feature_digest(
    rows: list[dict[str, str]],
    *,
    meta: Optional[dict[str, Any]] = None,
    pred_fh: Optional[int] = None,
    pred_fa: Optional[int] = None,
    overrides: Optional[dict[int, dict[str, float]]] = None,
) -> dict[str, Any]:
    """Compute the compact deterministic feature payload sent to the LLM."""
    result = P.compute(rows, overrides=overrides, pred_fh=pred_fh, pred_fa=pred_fa)
    first = _first_present_milestone(result) or {}
    goals = P.get_goals(rows)
    opening_ou = first.get("c")
    if opening_ou is None:
        opening_ou = _opening_ou_fallback(rows)
    return {
        "meta": meta or {},
        "real_score": [result.get("real_fh"), result.get("real_fa")],
        "effective_prediction": [result.get("effective_fh"), result.get("effective_fa")],
        "opening_hc": first.get("a"),
        "opening_hc_side": first.get("a_side"),
        "opening_ou": opening_ou,
        "goal_count_detected": len(goals),
        "goal_sequence": [
            {"score": f"{h}-{a}", "row_index": idx, "minute": P.half_to_minute(rows[idx].get("Half", ""))}
            for idx, h, a in goals[:9]
        ],
        "milestones": result.get("milestones"),
    }


def _json_for_prompt(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def _trim_stats_for_prompt(stats: dict[str, Any]) -> dict[str, Any]:
    """Slim the base-rate payload sent to the LLM.

    The full stats dict (10 by_open_hc buckets + score_dist in both bucket and
    overall) bloats the prompt, which makes a reasoning model burn more of its
    budget — empirically enough to truncate the answer (finish_reason=length,
    empty content). The LLM only needs the headline rates + sample sizes; the
    full object is still returned to the UI separately.
    """
    def slim(agg: Optional[dict[str, Any]], keep_scores: bool) -> dict[str, Any]:
        if not agg:
            return {}
        out = {k: agg.get(k) for k in (
            "n", "fav_cover_rate", "fav_cover_n", "over_rate", "over_n",
            "avg_goals", "btts_rate", "fav_win_rate", "draw_rate",
        ) if agg.get(k) is not None}
        if keep_scores and agg.get("score_dist"):
            out["score_dist"] = agg["score_dist"][:5]
        return out

    return {
        "n_total": stats.get("n_total"),
        "filters": stats.get("filters"),
        "bucket": slim(stats.get("bucket"), keep_scores=True),
        "overall": slim(stats.get("overall"), keep_scores=False),
        "top_open_hc_buckets": (stats.get("by_open_hc") or [])[:3],
    }


async def analyze_match(
    rows: list[dict[str, str]],
    *,
    meta: Optional[dict[str, Any]] = None,
    pred_fh: Optional[int] = None,
    pred_fa: Optional[int] = None,
    overrides: Optional[dict[int, dict[str, float]]] = None,
    prestigious_only: bool = False,
) -> dict[str, Any]:
    """Run grounded LLM analysis for one analyzer state."""
    if not AI.is_configured():
        raise RuntimeError("AI chưa được cấu hình")

    features = build_feature_digest(
        rows, meta=meta, pred_fh=pred_fh, pred_fa=pred_fa, overrides=overrides
    )
    stats = compute_pattern_stats(
        open_hc=str(features.get("opening_hc")) if features.get("opening_hc") is not None else None,
        open_ou=str(features.get("opening_ou")) if features.get("opening_ou") is not None else None,
        prestigious_only=prestigious_only,
    )

    prompt = f"""Bạn là trợ lý phân tích dữ liệu bóng đá/kèo châu Á. Chỉ dùng số liệu trong JSON, không bịa sample size/tỉ lệ.

NHIỆM VỤ:
- Tóm tắt pattern của trận theo dữ liệu milestone.
- Đưa nhận định có điều kiện về: kèo chấp favorite-cover, tài/xỉu, và tỉ số hợp lý.
- Luôn nhắc sample size. Nếu sample nhỏ (<30) phải hạ confidence rõ ràng.
- Không đưa lời khuyên cá cược chắc thắng. Viết ngắn, thực dụng, tiếng Việt.
- CHỈ trả JSON, không kèm giải thích ngoài JSON.

FEATURES_JSON={_json_for_prompt(features)}
BASE_RATE_JSON={_json_for_prompt(_trim_stats_for_prompt(stats))}

Trả về JSON đúng schema:
{{
  "summary": "1-2 câu",
  "signals": ["..."],
  "prediction": {{"score": "x-y", "handicap_lean": "favorite|underdog|no_edge", "ou_lean": "over|under|no_edge"}},
  "confidence": 0.0,
  "caveats": ["..."]
}}
"""
    data = await AI.chat(
        [{"role": "user", "content": prompt}],
        temperature=0.0,
        # This is an INTERPRETATION task — Layer B already computed every number,
        # so deep reasoning is wasted and (with always-reason models like
        # deepseek-v4) burns the whole token budget before any `content` is
        # emitted (finish_reason=length, empty answer). Force a modest effort
        # regardless of the global AI_REASONING_EFFORT, and keep a generous cap.
        reasoning_effort="medium",
        max_tokens=4000,
        timeout=90,
    )
    content = AI.extract_content(data)
    parsed = _parse_json_loose(content)
    return {
        "features": features,
        "stats": stats,
        "model": data.get("model"),
        "content": content,
        "parsed": parsed,
        "finish_reason": ((data.get("choices") or [{}])[0]).get("finish_reason"),
        "reasoning_chars": len(AI.extract_reasoning(data)),
        "usage": data.get("usage") or {},
    }


def _parse_json_loose(text: str) -> Optional[dict[str, Any]]:
    """Best-effort JSON extraction from an LLM reply.

    Handles: clean JSON, ```json fenced blocks, and leading/trailing prose by
    slicing to the outermost balanced braces. Returns None if nothing parses.
    """
    if not text:
        return None
    s = text.strip()
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        pass
    # Strip markdown fences.
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", s, re.S)
    if fence:
        try:
            return json.loads(fence.group(1))
        except json.JSONDecodeError:
            pass
    # Fall back to the outermost {...} span.
    start = s.find("{")
    end = s.rfind("}")
    if 0 <= start < end:
        try:
            return json.loads(s[start:end + 1])
        except json.JSONDecodeError:
            return None
    return None
