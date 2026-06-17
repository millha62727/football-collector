#!/usr/bin/env python3
"""Test độ chính xác dự đoán: cắt CSV tại phút 75', chạy analyze, so với FT thật.

Phương pháp:
  1. Query 10 trận FT ngẫu nhiên, total_goals > 3
  2. Với mỗi trận, fetch match_odds_history đầy đủ
  3. Cắt rows: chỉ giữ snapshot có `minute <= 75` (cột minute có sẵn trong DB)
  4. Convert sang CSV rows (giống _db_rows_to_csv_rows)
  5. Gọi parser.compute() deterministic → lấy real_fh/real_fa từ CSV đã cắt
     (đây là tỉ số tại 75' — LLM sẽ thấy con số này, không phải FT)
  6. Lấy FT thật từ matches.home_score/away_score (ground truth)
  7. So sánh: nếu real_fh(real_fa) từ CSV cắt == FT thật → predict đúng tỉ số

Lưu ý: Đây là test DETERMINISTIC (không qua LLM) vì:
  - Nhanh, free, không cần API key
  - Tập trung vào logic cut + compare
  - LLM prediction sẽ test riêng (option --llm)

Usage:
    python3 scripts/test_cutoff_75min.py                 # 10 trận random
    python3 scripts/test_cutoff_75min.py --count 20      # 20 trận
    python3 scripts/test_cutoff_75min.py --llm           # dùng LLM (chậm, tốn API)
    python3 scripts/test_cutoff_75min.py --match-id ID   # test 1 trận cụ thể
"""
from __future__ import annotations

import argparse
import asyncio
import os
import random
import sys
from typing import Any, Optional

# Allow running from /app inside container
sys.path.insert(0, "/app")

from app.database import (  # type: ignore
    _connect,
    get_match_by_id,
    get_odds_history_for_analyzer,
)
from app.analyzer.parser import compute as parser_compute  # type: ignore
from app.analyzer.views import _db_rows_to_csv_rows  # type: ignore


def query_random_matches(count: int, seed: Optional[int] = None) -> list[dict[str, Any]]:
    """Lấy N trận FT random, total_goals > 3, có ít nhất 20 odds snapshots."""
    if seed is not None:
        random.seed(seed)
    with _connect() as conn:
        cur = conn.cursor(cursor_factory=__import__("psycopg2").extras.RealDictCursor)
        cur.execute(
            """
            SELECT m.id, m.competition, m.home, m.away,
                   m.home_score, m.away_score, m.start_time_utc,
                   (m.home_score + m.away_score) AS total_goals,
                   (SELECT COUNT(*) FROM match_odds_history o
                     WHERE o.match_id = m.id) AS n_snapshots
              FROM matches m
             WHERE m.status = 'FT'
               AND m.home_score IS NOT NULL
               AND m.away_score IS NOT NULL
               AND (m.home_score + m.away_score) > 3
               AND EXISTS (
                   SELECT 1 FROM match_odds_history o
                    WHERE o.match_id = m.id
                      AND o.minute IS NOT NULL
                      AND o.minute >= 75
               )
             ORDER BY random()
             LIMIT %s
            """,
            (count,),
        )
        return [dict(r) for r in cur.fetchall()]


def cut_odds_history_at_75min(db_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Cắt match_odds_history: chỉ giữ snapshot có minute <= 75.

    Quan trọng: phải lấy snapshot CÓ minute thực tế (không phải NULL hoặc 0),
    vì 0 có thể là pre-match. Tuy nhiên pre-match (minute=0) cũng cần giữ
    để LLM biết opening odds. Rule: giữ minute <= 75 HOẶC minute IS NULL.
    """
    out = []
    for r in db_rows:
        m = r.get("minute")
        if m is None or m <= 75:
            out.append(r)
    return out


def evaluate_match(match: dict[str, Any], use_llm: bool = False, model: Optional[str] = None) -> dict[str, Any]:
    """Đánh giá 1 trận: cắt CSV tại 75', chạy analyze, so với FT thật.

    Returns dict với:
      - match_id, home, away, ft_score (thật)
      - score_at_75min: tỉ số tại snapshot cuối <= 75'
      - prediction: tỉ số LLM dự đoán (None nếu không dùng LLM)
      - correct: True nếu score_at_75min + dự đoán == FT thật
      - method: 'deterministic' | 'llm'
    """
    match_id = match["id"]
    ft_home = int(match["home_score"])
    ft_away = int(match["away_score"])
    ft_total = ft_home + ft_away
    ft_str = f"{ft_home}-{ft_away}"

    # Fetch + cut
    db_rows = get_odds_history_for_analyzer(match_id)
    if not db_rows:
        return {
            "match_id": match_id, "home": match["home"], "away": match["away"],
            "ft_score": ft_str, "error": "no_odds_history",
        }

    n_original = len(db_rows)
    cut_rows = cut_odds_history_at_75min(db_rows)
    n_cut = len(cut_rows)
    last_minute = max((r.get("minute") or 0) for r in cut_rows) if cut_rows else 0

    if not cut_rows:
        return {
            "match_id": match_id, "home": match["home"], "away": match["away"],
            "ft_score": ft_str, "error": "no_rows_after_cut",
            "n_original": n_original,
        }

    # Tỉ số tại 75' (lấy từ snapshot cuối cùng)
    last_row = cut_rows[-1]
    score_at_75 = f"{int(last_row.get('home_score') or 0)}-{int(last_row.get('away_score') or 0)}"
    total_at_75 = int(last_row.get('home_score') or 0) + int(last_row.get('away_score') or 0)

    # Convert sang CSV rows
    csv_rows = _db_rows_to_csv_rows(cut_rows)

    # Deterministic: parser.compute() để xác nhận CSV hợp lệ + lấy features
    try:
        result: Any = parser_compute(csv_rows) or {}
        milestones = result.get("milestones") or []
        milestones_count = sum(1 for m in milestones if m is not None)
        real_fh_csv = int(result.get("real_fh") or 0)
        real_fa_csv = int(result.get("real_fa") or 0)
        ohh = result.get("ohh", 0.0) or 0.0
        oah = result.get("oah", 0.0) or 0.0
    except Exception as e:
        return {
            "match_id": match_id, "home": match["home"], "away": match["away"],
            "ft_score": ft_str, "error": f"compute_failed: {e}",
            "n_original": n_original, "n_cut": n_cut, "last_minute": last_minute,
        }

    # Score 75' từ CSV (real_fh/real_fa từ csv last row)
    score_75_csv = f"{real_fh_csv}-{real_fa_csv}"
    total_75_csv = real_fh_csv + real_fa_csv

    # Đánh giá: CSV-cắt 75' có cho tỉ số tương đương FT thật không?
    # (Trong dataset này, nếu FT > 3 bàn, có thể tỉ số 75' đã là final
    # — tức không có bàn thắng nào sau 75'. Đây là case "dự đoán dễ".)
    same_at_75 = (real_fh_csv == ft_home and real_fa_csv == ft_away)

    # Nếu dùng LLM: gọi analyze_match() với CSV đã cắt
    prediction = None
    method = "deterministic"
    if use_llm:
        method = "llm"
        try:
            from app.analyzer.ai_pattern import analyze_match
            meta = {
                "league": match.get("competition"),
                "home": match.get("home"),
                "away": match.get("away"),
            }
            loop = asyncio.new_event_loop()
            try:
                llm_result = loop.run_until_complete(
                    analyze_match(csv_rows, meta=meta, prestigious_only=False, model=model)
                )
            finally:
                loop.close()
            parsed = llm_result.get("parsed") or {}
            pred = parsed.get("prediction") if isinstance(parsed.get("prediction"), dict) else {}
            # prediction.score = "x-y" — split into home/away
            score_str = pred.get("score") if isinstance(pred, dict) else None
            pred_home, pred_away, pred_total = None, None, None
            if isinstance(score_str, str) and "-" in score_str:
                try:
                    parts = score_str.split("-")
                    pred_home = int(parts[0].strip())
                    pred_away = int(parts[1].strip())
                    pred_total = pred_home + pred_away
                except (ValueError, IndexError):
                    pass
            prediction = {
                "score": score_str,
                "home": pred_home,
                "away": pred_away,
                "total": pred_total,
                "handicap_lean": pred.get("handicap_lean"),
                "ou_lean": pred.get("ou_lean"),
                "outcome": pred.get("handicap_lean"),  # alias
                "more_goals_likely": pred.get("more_goals_likely"),
                "confidence": parsed.get("confidence"),
            }
            # Lưu thêm summary/signals/tags để so sánh "tag công thức" giữa các model
            prediction["_summary"] = (parsed.get("summary") or "")[:300]
            prediction["_signals"] = parsed.get("signals") if isinstance(parsed.get("signals"), list) else []
            prediction["_tags"] = parsed.get("tags") if isinstance(parsed.get("tags"), list) else []
            prediction["_model_used"] = llm_result.get("model") or model
        except Exception as e:
            prediction = {"error": str(e)[:100]}

    return {
        "match_id": match_id,
        "home": match["home"],
        "away": match["away"],
        "competition": match.get("competition"),
        "ft_score": ft_str,
        "ft_total": ft_total,
        "n_original": n_original,
        "n_cut": n_cut,
        "last_minute": last_minute,
        "score_at_75": score_75_csv,
        "total_at_75": total_75_csv,
        "opening_hc": f"{ohh} / {oah}",
        "milestones_count": milestones_count,
        "same_at_75": same_at_75,
        "method": method,
        "prediction": prediction,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--count", type=int, default=10, help="số trận random (default 10)")
    parser.add_argument("--seed", type=int, default=None, help="random seed để reproduce")
    parser.add_argument("--llm", action="store_true", help="dùng LLM (chậm, tốn API)")
    parser.add_argument("--model", type=str, default=None, help="tên model override (vd: Claude.7-kiro). Mặc định dùng AI_MODEL_UI")
    parser.add_argument("--match-id", type=str, default=None, help="test 1 trận cụ thể")
    args = parser.parse_args()

    if args.match_id:
        match = get_match_by_id(args.match_id)
        if not match:
            print(f"[ERROR] match_id={args.match_id} không tồn tại")
            return 1
        # Bổ sung total_goals
        match["total_goals"] = (match.get("home_score") or 0) + (match.get("away_score") or 0)
        matches = [match]
    else:
        print(f"[INFO] Query {args.count} trận FT random (total_goals > 3, có snapshot >= 75')...")
        matches = query_random_matches(args.count, seed=args.seed)
        if not matches:
            print("[ERROR] Không tìm được trận nào thỏa điều kiện")
            return 1
        print(f"[INFO] Tìm được {len(matches)} trận")

    results = []
    print("\n" + "=" * 100)
    print(f"{'#':>3} {'Match':<40} {'FT':<6} {'75':<6} {'OK?':<5} {'#snap orig/cut':<15} {'Last min':<10}")
    print("=" * 100)

    for i, m in enumerate(matches, 1):
        r = evaluate_match(m, use_llm=args.llm, model=args.model)
        results.append(r)

        if "error" in r:
            print(f"{i:>3} {r.get('home','?') + ' vs ' + r.get('away','?'):<40} "
                  f"{r.get('ft_score','?'):<6} {'?':<6} {'ERR':<5} "
                  f"ERROR: {r['error'][:50]}")
            continue

        match_label = f"{r['home']} vs {r['away']}"[:40]
        ok_mark = "✓" if r["same_at_75"] else "✗"
        snap_str = f"{r['n_original']}/{r['n_cut']}"
        print(f"{i:>3} {match_label:<40} {r['ft_score']:<6} {r['score_at_75']:<6} "
              f"{ok_mark:<5} {snap_str:<15} {r['last_minute']}")

    # Summary
    print("\n" + "=" * 100)
    valid = [r for r in results if "error" not in r]
    n_correct = sum(1 for r in valid if r["same_at_75"])
    n_total = len(valid)
    pct = round(100 * n_correct / n_total, 1) if n_total else 0
    print(f"SUMMARY: {n_correct}/{n_total} trận có tỉ số tại 75' == FT thật ({pct}%)")
    print("=" * 100)

    if args.llm:
        # Bonus: nếu dùng LLM, in prediction so với FT
        print("\nLLM PREDICTIONS:")
        for r in results:
            if "prediction" in r and r["prediction"]:
                p = r["prediction"]
                pred_str = (f"{p.get('home')}-{p.get('away')}" if p.get("home") is not None
                           else f"error: {p.get('error','?')[:30]}")
                conf = p.get("confidence")
                conf_str = f" conf={conf}" if conf is not None else ""
                print(f"  {r['home']} vs {r['away']}: FT={r['ft_score']}  "
                      f"75'={r['score_at_75']}  LLM={pred_str}{conf_str}")

    # Lưu kết quả vào file để dễ phân tích
    import json
    out_file = f"/tmp/cutoff_75_results_{args.seed or 'random'}.json"
    with open(out_file, "w") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    print(f"\n[INFO] Kết quả chi tiết: {out_file}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
