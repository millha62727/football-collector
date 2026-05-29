"""Grounded AI analysis for analyzer matches.

This module does NOT ask the model to invent statistics. It sends:
  1) deterministic features from parser.compute(), and
  2) empirical base-rates from database.compute_pattern_stats().
The LLM's job is to explain and synthesize, not to be the source of numbers.
"""
from __future__ import annotations

import json
from typing import Any, Optional

from ..database import compute_pattern_stats
from . import ai_client as AI
from . import parser as P


def _first_present_milestone(result: dict[str, Any]) -> Optional[dict[str, Any]]:
    for m in result.get("milestones") or []:
        if m:
            return m
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
    return {
        "meta": meta or {},
        "real_score": [result.get("real_fh"), result.get("real_fa")],
        "effective_prediction": [result.get("effective_fh"), result.get("effective_fa")],
        "opening_hc": first.get("a"),
        "opening_hc_side": first.get("a_side"),
        "opening_ou": first.get("c"),
        "goal_count_detected": len(goals),
        "goal_sequence": [
            {"score": f"{h}-{a}", "row_index": idx, "minute": P.half_to_minute(rows[idx].get("Half", ""))}
            for idx, h, a in goals[:9]
        ],
        "milestones": result.get("milestones"),
    }


def _json_for_prompt(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


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

FEATURES_JSON={_json_for_prompt(features)}
BASE_RATE_JSON={_json_for_prompt(stats)}

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
        max_tokens=900,
        timeout=60,
    )
    content = AI.extract_content(data)
    parsed: Optional[dict[str, Any]] = None
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError:
        parsed = None
    return {
        "features": features,
        "stats": stats,
        "model": data.get("model"),
        "content": content,
        "parsed": parsed,
        "reasoning_chars": len(AI.extract_reasoning(data)),
        "usage": data.get("usage") or {},
    }
