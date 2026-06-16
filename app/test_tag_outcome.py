"""Tests for tag outcome validation (Hướng 1 — Phase 3).

Covers:
  P0: wilson_ci95  — pure function, edge cases (n=0, p=0, p=1, p clamp)
  P0: tag_outcome_validate — empty rows, missing data, sufficient sample,
       insufficient_data guard (n<15), level side, quarter-line averaging

Run:
    python -m app.test_tag_outcome
hoặc
    python app/test_tag_outcome.py
"""
from __future__ import annotations

import math
import os
import sys

# Allow `python app/test_tag_outcome.py` from any cwd
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.database import (  # noqa: E402
    MIN_VALIDATE_N,
    tag_outcome_validate,
    wilson_ci95,
)


# ---- tiny assert helpers (không dùng pytest để giữ convention project) ----

_FAILURES: list[str] = []


def _check(cond: bool, label: str, detail: str = "") -> None:
    if cond:
        print(f"  ✓ {label}")
    else:
        msg = f"  ✗ {label}" + (f" — {detail}" if detail else "")
        print(msg)
        _FAILURES.append(msg)


def _approx(a: float, b: float, tol: float = 1e-3) -> bool:
    return abs(a - b) < tol


# =========================================================================
# wilson_ci95
# =========================================================================

def test_wilson_ci95_n_zero() -> None:
    print("\n[wilson_ci95] n=0 → (0, 0)")
    c, h = wilson_ci95(0.5, 0)
    _check(c == 0.0 and h == 0.0, f"n=0 → (0,0), got ({c}, {h})")


def test_wilson_ci95_n_negative() -> None:
    print("\n[wilson_ci95] n<0 → (0, 0) — defensive guard")
    c, h = wilson_ci95(0.5, -5)
    _check(c == 0.0 and h == 0.0, f"n<0 → (0,0), got ({c}, {h})")


def test_wilson_ci95_p_clamp() -> None:
    """p ngoài [0, 1] bị clamp trước khi vào công thức Wilson.

    Lưu ý: Wilson KHÔNG trả center=0.0 hay 1.0 cho p extreme vì `denom = 1 +
    z²/n` luôn > 1, kéo center về phía 0.5 (shrinkage). Tại p=1.5, n=100:
    center ≈ 0.982 (chứ không phải 1.0); tại p=-0.3: center ≈ 0.018 (không
    phải 0.0). Đây là tính chất cốt lõi của Wilson so với Wald approx.
    """
    print("\n[wilson_ci95] p out of range → clamp về [0, 1] trước khi tính")
    # p=1.5 clamp về 1.0, center Wilson ≈ 0.982 (không phải 1.0)
    c, h = wilson_ci95(1.5, 100)
    _check(_approx(c, 0.9815, 5e-3), f"p=1.5 → center≈0.9815, got {c:.4f}")
    # p=-0.3 clamp về 0.0, center Wilson ≈ 0.018 (không phải 0.0)
    c, h = wilson_ci95(-0.3, 100)
    _check(_approx(c, 0.0185, 5e-3), f"p=-0.3 → center≈0.0185, got {c:.4f}")


def test_wilson_ci95_at_p_half() -> None:
    """Wilson CI95 tại p=0.5, n=15 phải có center ≈ 0.5, half-width ≈ 0.226."""
    print("\n[wilson_ci95] p=0.5 reference values (anh verify 16/06)")
    # p=0.5, n=15
    c, h = wilson_ci95(0.5, 15)
    _check(_approx(c, 0.5, 1e-3), f"p=0.5, n=15 → center≈0.5, got {c:.4f}")
    _check(_approx(h, 0.226, 5e-3), f"p=0.5, n=15 → half≈0.226, got {h:.4f}")

    # p=0.5, n=100 → half-width ≈ 0.096
    c, h = wilson_ci95(0.5, 100)
    _check(_approx(h, 0.096, 5e-3), f"p=0.5, n=100 → half≈0.096, got {h:.4f}")

    # p=0.5, n=500 → half-width ≈ 0.044
    c, h = wilson_ci95(0.5, 500)
    _check(_approx(h, 0.044, 5e-3), f"p=0.5, n=500 → half≈0.044, got {h:.4f}")


def test_wilson_ci95_extreme_p() -> None:
    """Tại p=0.0 hoặc 1.0, Wilson KHÔNG trả center=0/1 mà shrink về trung gian.

    Tại p=0, n=50: center ≈ 0.0357 (Wilson pull toward 0.5).
    Tại p=1, n=50: center ≈ 0.9643.
    Half-width dương (~0.036) — CI hữu ích.
    """
    print("\n[wilson_ci95] extreme p (0.0 và 1.0) — Wilson shrinkage")
    c0, h0 = wilson_ci95(0.0, 50)
    _check(_approx(c0, 0.0357, 5e-3), f"p=0, n=50 → center≈0.0357, got {c0:.4f}")
    _check(h0 > 0, f"half > 0, got {h0:.4f}")
    c1, h1 = wilson_ci95(1.0, 50)
    _check(_approx(c1, 0.9643, 5e-3), f"p=1, n=50 → center≈0.9643, got {c1:.4f}")
    _check(h1 > 0, f"half > 0, got {h1:.4f}")


def test_wilson_ci95_symmetry() -> None:
    """Wilson CI symmetric quanh 0.5 cho p=0.5 — sanity check."""
    print("\n[wilson_ci95] symmetry check")
    c1, h1 = wilson_ci95(0.3, 100)
    c2, h2 = wilson_ci95(0.7, 100)
    # CI widths giống nhau cho p và 1-p (cùng variance)
    _check(_approx(h1, h2, 1e-3), f"half(p=0.3) ≈ half(p=0.7): {h1:.4f} vs {h2:.4f}")
    # centers đối xứng quanh 0.5
    _check(_approx(c1 + c2, 1.0, 1e-3), f"center(0.3)+center(0.7)≈1.0: {c1+c2:.4f}")


# =========================================================================
# tag_outcome_validate — edge cases
# =========================================================================

def test_tag_outcome_empty_rows() -> None:
    print("\n[tag_outcome_validate] empty rows")
    out = tag_outcome_validate("any_tag", [])
    _check(out["n"] == 0, f"n=0, got {out['n']}")
    _check(out["insufficient_data"] is True, f"insufficient=True, got {out['insufficient_data']}")
    _check(out["fav_cover_rate_actual"] is None, f"rate=None, got {out['fav_cover_rate_actual']}")
    _check(out["reason"] == "no_rows", f"reason='no_rows', got {out['reason']!r}")


def test_tag_outcome_tag_not_present() -> None:
    print("\n[tag_outcome_validate] tag không xuất hiện trong rows")
    rows = [{"tags": ["other_tag"], "open_hc": "-0.5", "open_hc_side": "home",
             "home_score": 1, "away_score": 0}]
    out = tag_outcome_validate("missing_tag", rows)
    _check(out["n"] == 0, f"n=0, got {out['n']}")
    _check(out["reason"] == "no_rows", f"reason='no_rows' (không có row nào có tag)")


def test_tag_outcome_missing_data() -> None:
    """Row có tag nhưng thiếu open_hc hoặc side → valid_n không tăng."""
    print("\n[tag_outcome_validate] missing open_hc/side → skip row")
    rows = [
        # Row OK
        {"tags": ["X"], "open_hc": "-0.5", "open_hc_side": "home",
         "home_score": 1, "away_score": 0},
        # Row thiếu open_hc
        {"tags": ["X"], "open_hc": None, "open_hc_side": "home",
         "home_score": 1, "away_score": 0},
        # Row thiếu side
        {"tags": ["X"], "open_hc": "-0.5", "open_hc_side": None,
         "home_score": 1, "away_score": 0},
        # Row có level side (kèo đồng banh) → skip vì không tính cover được
        {"tags": ["X"], "open_hc": "0.0", "open_hc_side": "level",
         "home_score": 1, "away_score": 0},
    ]
    out = tag_outcome_validate("X", rows)
    _check(out["n"] == 4, f"n=4 (cả 4 rows đều có tag), got {out['n']}")
    _check(out["valid_n"] == 1, f"valid_n=1 (chỉ row 1 hợp lệ), got {out['valid_n']}")


def test_tag_outcome_insufficient_data() -> None:
    """n < MIN_VALIDATE_N → insufficient_data=True, rate=None."""
    print("\n[tag_outcome_validate] n<15 → insufficient_data=True")
    # 10 rows, đủ data
    rows = [
        {"tags": ["X"], "open_hc": "-0.5", "open_hc_side": "home",
         "home_score": 1 if i % 2 == 0 else 0, "away_score": 0}
        for i in range(10)
    ]
    out = tag_outcome_validate("X", rows, min_n=15)
    _check(out["valid_n"] == 10, f"valid_n=10, got {out['valid_n']}")
    _check(out["insufficient_data"] is True, f"insufficient=True (n=10<15), got {out['insufficient_data']}")
    _check(out["fav_cover_rate_actual"] is None, f"rate=None khi insufficient, got {out['fav_cover_rate_actual']}")
    _check(out["ci95_low"] is None, f"ci95_low=None khi insufficient")


def test_tag_outcome_sufficient_data() -> None:
    """n >= 15 + cover 100% → rate=1.0, CI hẹp."""
    print("\n[tag_outcome_validate] n=20, all cover → rate=1.0")
    # 20 rows, home thắng đậm → cover 100%
    rows = [
        {"tags": ["X"], "open_hc": "-0.5", "open_hc_side": "home",
         "home_score": 3, "away_score": 0}
        for _ in range(20)
    ]
    out = tag_outcome_validate("X", rows, min_n=15)
    _check(out["valid_n"] == 20, f"valid_n=20, got {out['valid_n']}")
    _check(out["insufficient_data"] is False, f"insufficient=False (n=20>=15)")
    _check(out["fav_cover_rate_actual"] == 1.0, f"rate=1.0 (all cover), got {out['fav_cover_rate_actual']}")
    # CI phải hẹp — high gần 1.0, low > 0.8
    _check(out["ci95_low"] is not None and out["ci95_low"] > 0.8,
           f"ci95_low > 0.8 (n=20, p=1.0), got {out['ci95_low']}")


def test_tag_outcome_quarter_line() -> None:
    """Quarter-line (-0.75) cover đúng theo công thức average.

    Sign convention: line=-0.75 (home handicap) nghĩa là home CHẤP away 0.75.
    _cover_score: `eff = margin + p` với margin = (fav_score - other_score).
      - margin=1 (home thắng 1-0): eff_p=-1.0 = 0 (push, 0.5) + eff_p=-0.5 = 0.5 (win, 1.0) → 0.75
      - margin=0 (hòa 0-0): eff_p=-1.0 = -1.0 (loss, 0) + eff_p=-0.5 = -0.5 (loss, 0) → 0.0

    Tức là quarter-line -0.75, home thắng 1 bàn (margin=1) chỉ cover 0.75
    (push + win), không phải 1.0. Đây là semantics Asian handicap quarter-ball.
    """
    print("\n[tag_outcome_validate] quarter-line split đúng")
    # 20 rows: open_hc=-0.75, margin=1 → cover=0.75
    rows = [
        {"tags": ["X"], "open_hc": "-0.75", "open_hc_side": "home",
         "home_score": 1, "away_score": 0}
        for _ in range(20)
    ]
    out = tag_outcome_validate("X", rows, min_n=15)
    _check(_approx(out["fav_cover_rate_actual"], 0.75, 1e-3),
           f"quarter -0.75, margin=1 → cover=0.75 (push+win), got {out['fav_cover_rate_actual']}")

    # 20 rows: open_hc=-0.75, margin=0 → cover=0.0 (cả 2 part đều loss)
    rows = [
        {"tags": ["X"], "open_hc": "-0.75", "open_hc_side": "home",
         "home_score": 0, "away_score": 0}
        for _ in range(20)
    ]
    out = tag_outcome_validate("X", rows, min_n=15)
    _check(_approx(out["fav_cover_rate_actual"], 0.0, 1e-3),
           f"quarter -0.75, margin=0 → cover=0.0 (cả 2 part loss), got {out['fav_cover_rate_actual']}")

    # Reference case: open_hc=-0.5 (whole-half), margin=1 (home thắng 1-0) → push
    # vì eff = 1 + (-0.5) = 0.5 (win), KHÔNG phải margin=0 (loss).
    # Test này từng bị viết sai do nhầm "margin=0 là push" — thực ra push chỉ
    # xảy ra khi `margin + p == 0`, tức margin = -p = 0.5 cho line=-0.5.
    rows = [
        {"tags": ["X"], "open_hc": "-0.5", "open_hc_side": "home",
         "home_score": 1, "away_score": 0}
        for _ in range(20)
    ]
    out = tag_outcome_validate("X", rows, min_n=15)
    _check(out["fav_cover_rate_actual"] == 1.0,
           f"whole-half -0.5, margin=1 → win=1.0, got {out['fav_cover_rate_actual']}")

    # Bonus: open_hc=-0.5, margin=0 (hòa 0-0) → loss (0.0)
    rows = [
        {"tags": ["X"], "open_hc": "-0.5", "open_hc_side": "home",
         "home_score": 0, "away_score": 0}
        for _ in range(20)
    ]
    out = tag_outcome_validate("X", rows, min_n=15)
    _check(out["fav_cover_rate_actual"] == 0.0,
           f"whole-half -0.5, margin=0 → loss=0.0 (home không cover handicap), got {out['fav_cover_rate_actual']}")


def test_tag_outcome_away_side() -> None:
    """Side='away' → margin tính từ phía away (aw - hs)."""
    print("\n[tag_outcome_validate] side='away' → margin = aw - hs")
    # 20 rows: open_hc=-0.5 (away favorite), aw thắng 1-0 → margin=1 → cover 1.0
    rows = [
        {"tags": ["X"], "open_hc": "-0.5", "open_hc_side": "away",
         "home_score": 0, "away_score": 1}
        for _ in range(20)
    ]
    out = tag_outcome_validate("X", rows, min_n=15)
    _check(out["fav_cover_rate_actual"] == 1.0, f"away favorite wins → cover=1.0, got {out['fav_cover_rate_actual']}")


def test_tag_outcome_min_n_override() -> None:
    """Caller có thể override min_n (vd muốn strict hơn)."""
    print("\n[tag_outcome_validate] min_n override (caller custom)")
    rows = [
        {"tags": ["X"], "open_hc": "-0.5", "open_hc_side": "home",
         "home_score": 1, "away_score": 0}
        for _ in range(20)
    ]
    out = tag_outcome_validate("X", rows, min_n=50)  # strict hơn default
    _check(out["insufficient_data"] is True, f"n=20 < min_n=50 → insufficient, got {out['insufficient_data']}")


def test_tag_outcome_avg_metrics() -> None:
    """avg_margin và avg_total_goals phải là simple mean của các valid rows."""
    print("\n[tag_outcome_validate] avg_margin + avg_total_goals")
    # 20 rows: home thắng 2-1 → margin=1, total=3
    rows = [
        {"tags": ["X"], "open_hc": "-0.5", "open_hc_side": "home",
         "home_score": 2, "away_score": 1}
        for _ in range(20)
    ]
    out = tag_outcome_validate("X", rows, min_n=15)
    _check(_approx(out["avg_margin"], 1.0, 1e-3), f"avg_margin=1.0, got {out['avg_margin']}")
    _check(_approx(out["avg_total_goals"], 3.0, 1e-3), f"avg_total_goals=3.0, got {out['avg_total_goals']}")


def test_tag_outcome_no_cover_data() -> None:
    """Tất cả rows có tag nhưng đều thiếu data → reason='no_cover_data'."""
    print("\n[tag_outcome_validate] no_cover_data (tag có nhưng data thiếu)")
    rows = [
        {"tags": ["X"], "open_hc": None, "open_hc_side": None,
         "home_score": 1, "away_score": 0},
        {"tags": ["X"], "open_hc": "0", "open_hc_side": "level",
         "home_score": 1, "away_score": 0},
    ]
    out = tag_outcome_validate("X", rows)
    _check(out["n"] == 2, f"n=2 (cả 2 có tag), got {out['n']}")
    _check(out["valid_n"] == 0, f"valid_n=0 (không có row hợp lệ)")
    _check(out["reason"] == "no_cover_data", f"reason='no_cover_data', got {out['reason']!r}")
    _check(out["insufficient_data"] is True, f"insufficient_data=True, got {out['insufficient_data']}")


def test_min_validate_n_constant() -> None:
    """MIN_VALIDATE_N = 15 (frozen trong plan)."""
    print("\n[constants] MIN_VALIDATE_N")
    _check(MIN_VALIDATE_N == 15, f"MIN_VALIDATE_N=15, got {MIN_VALIDATE_N}")


# =========================================================================
# Integration: aggregate_patterns với rows giả lập (không cần DB)
# =========================================================================
# Phần này KHÔNG test được tại local (cần DB). Phase 3.6 sẽ test trên data
# thật qua VPS (anh chạy query và paste output).

def main() -> int:
    print("=" * 70)
    print("Tag outcome validation test suite — Hướng 1 (Phase 3)")
    print("=" * 70)

    test_wilson_ci95_n_zero()
    test_wilson_ci95_n_negative()
    test_wilson_ci95_p_clamp()
    test_wilson_ci95_at_p_half()
    test_wilson_ci95_extreme_p()
    test_wilson_ci95_symmetry()

    test_tag_outcome_empty_rows()
    test_tag_outcome_tag_not_present()
    test_tag_outcome_missing_data()
    test_tag_outcome_insufficient_data()
    test_tag_outcome_sufficient_data()
    test_tag_outcome_quarter_line()
    test_tag_outcome_away_side()
    test_tag_outcome_min_n_override()
    test_tag_outcome_avg_metrics()
    test_tag_outcome_no_cover_data()
    test_min_validate_n_constant()

    print("\n" + "=" * 70)
    if _FAILURES:
        print(f"FAILED: {len(_FAILURES)} assertion(s)")
        for f in _FAILURES:
            print(f)
        return 1
    print(f"ALL PASSED ({len(_FAILURES)} failures)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
