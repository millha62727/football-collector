"""Validate margin signal on FULL dataset (not just 200 random).

Question: does the 91.2% fav_cover rate for margin>=2 at 75' hold across
the full population, or is it a small-sample artifact?

Output:
  1. Margin × fav_cover table on full data
  2. Drill-down: for margin>=2 cases, how often did fav ALREADY hold the
     lead (no late goal allowed)? This checks if 91% is "fav held on" vs
     "we filtered out cases where fav blew the lead".
"""
from __future__ import annotations
import sys, json
sys.path.insert(0, "/app")

from app.database import _connect, _parse_open_line_to_float, _cover_score


def main():
    with _connect() as conn:
        cur = conn.cursor()

        # For each FT match with >=75' data, compute:
        #  - score at 75' (h, a)
        #  - FT score (h, a)
        #  - open_hc + side (from first snapshot)
        #  - fav_covered outcome
        #  - margin at 75' from fav POV
        #  - margin at FT from fav POV
        #  - did fav blow the lead? (margin at 75' > 0 but margin at FT <= 0)
        #  - did fav extend? (margin at 75' < margin at FT, with positive sign)

        cur.execute("""
            WITH matches_with_data AS (
              SELECT
                m.id AS match_id,
                m.home_score AS ft_h, m.away_score AS ft_a,
                m.home_score + m.away_score AS total_goals
              FROM matches m
              WHERE m.status='FT'
                AND m.home_score IS NOT NULL AND m.away_score IS NOT NULL
                AND EXISTS (
                    SELECT 1 FROM match_odds_history o
                    WHERE o.match_id = m.id AND o.minute <= 75
                      AND o.home_handicap IS NOT NULL
                      AND o.home_score IS NOT NULL
                )
            ),
            opening AS (
              SELECT DISTINCT ON (o.match_id)
                o.match_id, o.home_handicap AS open_hc
              FROM match_odds_history o
              WHERE o.home_handicap IS NOT NULL
              ORDER BY o.match_id, o.minute ASC, o.captured_at ASC
            ),
            at_75 AS (
              SELECT DISTINCT ON (o.match_id)
                o.match_id,
                o.home_score AS h_75, o.away_score AS a_75
              FROM match_odds_history o
              WHERE o.minute <= 75 AND o.home_score IS NOT NULL
              ORDER BY o.match_id, o.minute DESC, o.captured_at DESC
            )
            SELECT
              m.match_id,
              m.ft_h, m.ft_a,
              o.open_hc,
              a.h_75, a.a_75
            FROM matches_with_data m
            JOIN opening o ON o.match_id = m.match_id
            JOIN at_75 a ON a.match_id = m.match_id
            WHERE o.open_hc IS NOT NULL AND o.open_hc <> '0'
              AND m.total_goals >= 1
        """)
        rows = cur.fetchall()

    print(f"Matches with full data: {len(rows)}")

    # Compute margin buckets + outcomes
    buckets = {
        "lead_-3_or_worse": [],  # fav trailing by 3+
        "lead_-2": [],
        "lead_-1": [],
        "level": [],
        "lead_+1": [],
        "lead_+2": [],
        "lead_+3_or_more": [],
        "no_score_data": [],
    }
    late_goal_after_75 = {"yes": 0, "no": 0}
    blew_lead = {"yes": 0, "no": 0}
    extended_lead = {"yes": 0, "no": 0}

    for match_id, ft_h, ft_a, open_hc, h_75, a_75 in rows:
        if h_75 is None or a_75 is None:
            buckets["no_score_data"].append(match_id)
            continue

        line_f = _parse_open_line_to_float(open_hc)
        if line_f is None or line_f == 0:
            continue
        side = "home" if line_f < 0 else "away"

        # Margin at 75 from fav POV
        margin_75 = (h_75 - a_75) if side == "home" else (a_75 - h_75)
        # Margin at FT from fav POV
        margin_ft = (ft_h - ft_a) if side == "home" else (ft_a - ft_h)

        # Fav covered?
        cover_75_score = _cover_score(line_f, margin_75) > 0.5
        cover_ft = _cover_score(line_f, margin_ft) > 0.5

        # Bucket
        if margin_75 <= -3:
            buckets["lead_-3_or_worse"].append((match_id, cover_ft, margin_75, margin_ft))
        elif margin_75 == -2:
            buckets["lead_-2"].append((match_id, cover_ft, margin_75, margin_ft))
        elif margin_75 == -1:
            buckets["lead_-1"].append((match_id, cover_ft, margin_75, margin_ft))
        elif margin_75 == 0:
            buckets["level"].append((match_id, cover_ft, margin_75, margin_ft))
        elif margin_75 == 1:
            buckets["lead_+1"].append((match_id, cover_ft, margin_75, margin_ft))
        elif margin_75 == 2:
            buckets["lead_+2"].append((match_id, cover_ft, margin_75, margin_ft))
        else:
            buckets["lead_+3_or_more"].append((match_id, cover_ft, margin_75, margin_ft))

        # Did fav blow the lead?
        if margin_75 > 0 and margin_ft <= 0:
            blew_lead["yes"] += 1
        elif margin_75 > 0:
            blew_lead["no"] += 1

        # Did fav extend the lead?
        if margin_75 > 0 and margin_ft > margin_75:
            extended_lead["yes"] += 1
        elif margin_75 > 0:
            extended_lead["no"] += 1

        # Late goal (any side scored after 75')
        if (ft_h + ft_a) > (h_75 + a_75):
            late_goal_after_75["yes"] += 1
        else:
            late_goal_after_75["no"] += 1

    print()
    print("=" * 70)
    print("MARGIN AT 75' (fav POV) × fav_cover rate (FULL DATA)")
    print("=" * 70)
    print(f"{'Bucket':<25} {'n':>6} {'fav_cov%':>10} {'CI95':>20}")
    print("-" * 70)
    total_n = 0
    total_fav_cov = 0
    for name in ["lead_-3_or_worse", "lead_-2", "lead_-1", "level",
                  "lead_+1", "lead_+2", "lead_+3_or_more"]:
        sub = buckets[name]
        n = len(sub)
        if n == 0:
            continue
        fc = sum(1 for _, cov, _, _ in sub if cov)
        rate = fc / n
        # Wilson 95% CI
        from math import sqrt
        if n > 0:
            z = 1.96
            p = rate
            denom = 1 + z*z/n
            center = (p + z*z/(2*n)) / denom
            half = z * sqrt(p*(1-p)/n + z*z/(4*n*n)) / denom
            ci_lo = max(0, center - half)
            ci_hi = min(1, center + half)
            ci_s = f"[{ci_lo*100:.1f}-{ci_hi*100:.1f}%]"
        else:
            ci_s = "—"
        print(f"{name:<25} {n:>6} {rate*100:>9.1f}% {ci_s:>20}")
        total_n += n
        total_fav_cov += fc
    print("-" * 70)
    baseline = total_fav_cov / total_n if total_n else 0
    print(f"{'TOTAL (baseline)':<25} {total_n:>6} {baseline*100:>9.1f}%")

    print()
    print("=" * 70)
    print("DRILL-DOWN: bias check")
    print("=" * 70)
    n_blew = blew_lead["yes"] + blew_lead["no"]
    print(f"When fav leading at 75' (margin > 0, n={n_blew}):")
    print(f"  Blew lead (FT margin <= 0): {blew_lead['yes']} = {blew_lead['yes']/n_blew*100:.1f}%")
    print(f"  Held lead (FT margin > 0):  {blew_lead['no']} = {blew_lead['no']/n_blew*100:.1f}%")
    print()
    n_ext = extended_lead["yes"] + extended_lead["no"]
    print(f"When fav leading at 75' (margin > 0, n={n_ext}):")
    print(f"  Extended lead (FT margin > 75' margin): {extended_lead['yes']} = {extended_lead['yes']/n_ext*100:.1f}%")
    print(f"  Held/shrunk (FT margin <= 75' margin):  {extended_lead['no']} = {extended_lead['no']/n_ext*100:.1f}%")
    print()
    print(f"Overall: any goal scored after 75'?")
    n_late = late_goal_after_75["yes"] + late_goal_after_75["no"]
    print(f"  Yes: {late_goal_after_75['yes']} = {late_goal_after_75['yes']/n_late*100:.1f}%")
    print(f"  No:  {late_goal_after_75['no']} = {late_goal_after_75['no']/n_late*100:.1f}%")

    # Save
    with open("/tmp/momentum_full.json", "w") as f:
        json.dump({
            "total_matches": total_n,
            "baseline_fav_cover_rate": baseline,
            "buckets": {k: len(v) for k, v in buckets.items()},
            "bucket_outcomes": {
                k: {
                    "n": len(v),
                    "fav_cover_count": sum(1 for _, cov, _, _ in v if cov),
                    "fav_cover_rate": sum(1 for _, cov, _, _ in v if cov) / len(v) if v else None,
                }
                for k, v in buckets.items()
            },
            "blew_lead_when_leading_at_75": blew_lead,
            "extended_lead_when_leading_at_75": extended_lead,
            "any_late_goal_after_75": late_goal_after_75,
        }, f, indent=2, default=str)
    print(f"\nSaved /tmp/momentum_full.json")


main()
