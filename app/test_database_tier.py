"""Tests for tier bucketing (Hướng 3 — Phase 1).

Covers:
  - tier_hc: pure function, no DB
  - tier_ou: pure function, no DB

Run:
    python -m app.test_database_tier
hoặc
    python app/test_database_tier.py
"""
from __future__ import annotations

import os
import sys

# Allow `python app/test_database_tier.py` from any cwd
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.database import tier_hc, tier_ou  # noqa: E402


_FAILURES: list[str] = []


def _check(cond: bool, label: str, detail: str = "") -> None:
    if cond:
        print(f"  ✓ {label}")
    else:
        msg = f"  ✗ {label}" + (f" — {detail}" if detail else "")
        print(msg)
        _FAILURES.append(msg)


# =========================================================================
# tier_hc
# =========================================================================

def test_tier_hc_keo_nho() -> None:
    print("\n[tier_hc] kèo_nhỏ (|line| <= 0.5)")
    for line in [0.0, 0.25, -0.25, 0.1, -0.1, 0.5, -0.5]:
        _check(tier_hc(line) == "kèo_nhỏ", f"line={line} → kèo_nhỏ (got {tier_hc(line)!r})")


def test_tier_hc_keo_vua() -> None:
    print("\n[tier_hc] kèo_vừa (0.5 < |line| <= 1.0)")
    for line in [0.75, -0.75, 0.6, -0.6, 1.0, -1.0, 0.9, -0.9]:
        _check(tier_hc(line) == "kèo_vừa", f"line={line} → kèo_vừa (got {tier_hc(line)!r})")


def test_tier_hc_keo_lon() -> None:
    print("\n[tier_hc] kèo_lớn (|line| > 1.0)")
    for line in [1.5, -1.5, 2.0, -2.0, 3.5, 1.1, -1.1]:
        _check(tier_hc(line) == "kèo_lớn", f"line={line} → kèo_lớn (got {tier_hc(line)!r})")


def test_tier_hc_invalid() -> None:
    print("\n[tier_hc] invalid inputs")
    _check(tier_hc(None) is None, "None → None")
    _check(tier_hc("not a number") is None, "string garbage → None")  # type: ignore[arg-type]
    _check(tier_hc(float("nan")) is None, "NaN → None")
    # Quarter ball mapped to nearest bucket (không return None)
    _check(tier_hc(0.3) == "kèo_nhỏ", f"line=0.3 → kèo_nhỏ (got {tier_hc(0.3)!r})")


# =========================================================================
# tier_ou
# =========================================================================

def test_tier_ou_thap() -> None:
    print("\n[tier_ou] thấp (line <= 2.5)")
    for line in [2.0, 2.25, 2.5, 1.5, 0.5, 2.4, 2.1]:
        _check(tier_ou(line) == "thấp", f"line={line} → thấp (got {tier_ou(line)!r})")


def test_tier_ou_vua() -> None:
    print("\n[tier_ou] vừa (2.5 < line <= 3.0)")
    for line in [2.75, 3.0, 2.8, 2.9, 2.6, 2.51]:
        _check(tier_ou(line) == "vừa", f"line={line} → vừa (got {tier_ou(line)!r})")


def test_tier_ou_cao() -> None:
    print("\n[tier_ou] cao (line > 3.0)")
    for line in [3.25, 3.5, 4.0, 4.5, 5.0, 3.1, 3.01]:
        _check(tier_ou(line) == "cao", f"line={line} → cao (got {tier_ou(line)!r})")


def test_tier_ou_invalid() -> None:
    print("\n[tier_ou] invalid inputs")
    _check(tier_ou(None) is None, "None → None")
    _check(tier_ou(0.0) is None, "0.0 → None (OU phải > 0)")
    _check(tier_ou(-1.0) is None, "negative → None")
    _check(tier_ou("garbage") is None, "string → None")  # type: ignore[arg-type]


# =========================================================================
# Driver
# =========================================================================

def main() -> int:
    print("=" * 70)
    print("Tier bucketing test suite — Hướng 3")
    print("=" * 70)

    test_tier_hc_keo_nho()
    test_tier_hc_keo_vua()
    test_tier_hc_keo_lon()
    test_tier_hc_invalid()
    test_tier_ou_thap()
    test_tier_ou_vua()
    test_tier_ou_cao()
    test_tier_ou_invalid()

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
