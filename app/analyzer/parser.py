"""Pure logic ported from the original Tkinter tool `dien_bien_tran (3).py`.

All helpers are stateless functions operating on a list-of-dict `rows`
(as returned by `csv.DictReader`). The single public entry point is
`compute(rows, overrides, pred_fh, pred_fa)` which returns the 9-row
milestone analysis.
"""
from __future__ import annotations

import csv
import io
import os
import re
from collections import Counter
from typing import Any, Callable, Iterable, Optional

# ---- Primitive helpers -----------------------------------------------------

def to_f(v: Any) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def parse_half(s: str) -> Optional[tuple[int, int]]:
    m = re.match(r"^([12])H_(\d+)", (s or "").strip())
    return (int(m.group(1)), int(m.group(2))) if m else None


def parse_fname(path_or_name: str) -> dict[str, str]:
    """Extract date/time/league/home/away from filename.

    Accepted shapes (basename, with or without `.csv`):
      20260101_1100_<league>-<home>_vs_<away>
      <prefix>_20260101_1100_<league>-<home>_vs_<away>
    """
    b = os.path.splitext(os.path.basename(path_or_name))[0]
    m = re.match(r"^(?:\d+_)?(\d{8})_(\d{4})_(.+?)-(.+?_vs_.+)$", b)
    if not m:
        return {"date": "", "time": "", "league": "", "home": "Home", "away": "Away"}
    d, t = m.group(1), m.group(2)
    teams = m.group(4).split("_vs_")
    return {
        "date": f"{d[6:]}/{d[4:6]}/{d[:4]}",
        "time": f"{t[:2]}:{t[2:]}",
        "league": m.group(3).replace("_", " "),
        "home": teams[0].replace("_", " "),
        "away": teams[1].replace("_", " ") if len(teams) > 1 else "",
    }


def read_csv_text(text: str) -> list[dict[str, str]]:
    """Read CSV from in-memory text (UTF-8 with optional BOM)."""
    if text.startswith("﻿"):
        text = text[1:]
    return list(csv.DictReader(io.StringIO(text)))


def read_csv_path(path: str) -> list[dict[str, str]]:
    with open(path, encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


# ---- Validation ------------------------------------------------------------

def validate(rows: list[dict[str, str]]) -> list[str]:
    errs: list[str] = []
    if not rows:
        return ['File rỗng hoặc không đọc được']
    h1_ok = h2_ok = False
    for r in rows:
        p = parse_half(r.get("Half", ""))
        if not p:
            continue
        if p[0] == 1 and p[1] <= 3:
            h1_ok = True
        if p[0] == 2 and p[1] >= 43:
            h2_ok = True
    if not (h1_ok and h2_ok):
        errs.append('File không đủ dữ liệu "half"')
    last = rows[-1]
    if to_f(last.get("Home Score", 0)) == 0 and to_f(last.get("Away Score", 0)) == 0:
        errs.append("Không ghi nhận được bàn thắng trong trận")
    return errs


# ---- Opening handicap ------------------------------------------------------

def get_open_hcap(rows: list[dict[str, str]]) -> tuple[float, float]:
    """First row that has any non-zero handicap (used to label who is favored)."""
    for r in rows:
        hh = to_f(r.get("Home Handicap", 0))
        ah = to_f(r.get("Away Handicap", 0))
        if hh != 0 or ah != 0:
            return hh, ah
    return 0.0, 0.0


# ---- Goal detection (with cancellation handling) --------------------------

def get_goals(rows: list[dict[str, str]]) -> list[tuple[int, int, int]]:
    """Return list of (row_index, home_score, away_score) for valid goals only.

    A goal is invalidated if a later event shows either score has decreased
    below this milestone — i.e. the goal was cancelled (VAR / overturned).
    """
    events: list[tuple[int, int, int]] = []
    ph = pa = 0
    for i, r in enumerate(rows):
        h = int(to_f(r.get("Home Score", 0)))
        a = int(to_f(r.get("Away Score", 0)))
        if h != ph or a != pa:
            events.append((i, h, a))
            ph, pa = h, a

    valid: list[tuple[int, int, int]] = []
    for ei, (idx, h, a) in enumerate(events):
        prev_h = events[ei - 1][1] if ei > 0 else 0
        prev_a = events[ei - 1][2] if ei > 0 else 0
        if h < prev_h or a < prev_a:
            continue  # this event itself is a cancellation
        cancelled = any(
            (events[j][1] < h or events[j][2] < a)
            for j in range(ei + 1, len(events))
        )
        if not cancelled:
            valid.append((idx, h, a))
    return valid


# ---- Generic row scanners --------------------------------------------------

def mode_of(vals: Iterable[Any]) -> Any:
    vals = list(vals)
    return Counter(vals).most_common(1)[0][0] if vals else None


def in_range(rows: list[dict[str, str]], hn: int, lo: int, hi: int) -> list[dict[str, str]]:
    out = []
    for r in rows:
        p = parse_half(r.get("Half", ""))
        if p and p[0] == hn and lo <= p[1] <= hi:
            out.append(r)
    return out


def back_val(rows: list[dict[str, str]], idx: int, col: str, cond: Callable[[float], bool]) -> Optional[float]:
    for i in range(idx - 1, -1, -1):
        v = to_f(rows[i].get(col, 0))
        if cond(v):
            return v
    return None


def fwd_val(rows: list[dict[str, str]], idx: int, col: str, cond: Callable[[float], bool]) -> Optional[float]:
    for i in range(idx + 1, len(rows)):
        v = to_f(rows[i].get(col, 0))
        if cond(v):
            return v
    return None


# ---- Handicap-side resolvers ----------------------------------------------

def get_chap_val(row: dict[str, str]) -> tuple[Optional[float], Optional[str]]:
    """Return (value, side) for the row's favorite-side handicap (negative)."""
    hh = to_f(row.get("Home Handicap", 0))
    ah = to_f(row.get("Away Handicap", 0))
    if hh < 0:
        return hh, "home"
    if ah < 0:
        return ah, "away"
    return None, None


def pre_match_chap(rows: list[dict[str, str]]) -> tuple[Optional[float], Optional[str]]:
    """Last valid handicap line BEFORE 1H_1' kicks off (pre-match opening).
    Anchored on `Over/Under Line != 0` so we skip rows where odds reset to 0."""
    last_valid: Optional[tuple[float, str]] = None
    for r in rows:
        h = (r.get("Half") or "").strip()
        if re.match(r"^1H_", h):
            break
        ou = to_f(r.get("Over/Under Line", 0))
        if ou == 0:
            continue
        hh = to_f(r.get("Home Handicap", 0))
        ah = to_f(r.get("Away Handicap", 0))
        if hh < 0:
            last_valid = (hh, "home")
        elif ah < 0:
            last_valid = (ah, "away")
        else:
            last_valid = (0.0, "level")
    return last_valid if last_valid else (None, None)


def back_chap(rows: list[dict[str, str]], idx: int) -> tuple[Optional[float], Optional[str]]:
    """Walk backwards from idx, anchored on Over/Under Line != 0.
    Returns (handicap_value, side) for the most recent stable row.
    """
    for i in range(idx - 1, -1, -1):
        ou = to_f(rows[i].get("Over/Under Line", 0))
        if ou == 0:
            continue
        hh = to_f(rows[i].get("Home Handicap", 0))
        ah = to_f(rows[i].get("Away Handicap", 0))
        if hh < 0:
            return hh, "home"
        if ah < 0:
            return ah, "away"
        return 0.0, "level"
    return None, None


def mode_chap(rows_subset: list[dict[str, str]]) -> tuple[Optional[float], Optional[str]]:
    pairs = [get_chap_val(r) for r in rows_subset]
    pairs = [(v, s) for v, s in pairs if v is not None]
    if not pairs:
        return None, None
    return Counter(pairs).most_common(1)[0][0]


# ---- Display helpers -------------------------------------------------------

def dg_fmt(val: Optional[float]) -> tuple[str, str]:
    """Return (label, color) for d/g result fields."""
    if val is None:
        return "-", "gray"
    if abs(val + 0.25) < 1e-9:
        return "-1/2", "#333333"
    if abs(val - 0.25) < 1e-9:
        return "1/2", "#333333"
    if val == 0:
        return "Hòa", "#aa7700"
    return ("Thắng", "#1060d0") if val > 0 else ("Thua", "#cc0000")


def side_color(side: Optional[str]) -> str:
    """Color for the [a] handicap text based on which side is favored."""
    if side == "home":
        return "#006600"   # green
    if side == "level":
        return "#555555"   # gray
    return "#880088"       # purple (away or None)


# ---- Main compute ---------------------------------------------------------

def compute(
    rows: list[dict[str, str]],
    overrides: dict[int, dict[str, float]] | None = None,
    pred_fh: Optional[int] = None,
    pred_fa: Optional[int] = None,
) -> dict[str, Any]:
    """Run the full 9-row milestone analysis.

    Returns a dict with keys:
      - milestones: list of 9 entries (each either None or
        {score, a, a_side, b, c, d, e, g})
      - real_fh, real_fa: actual final score from CSV last row
      - effective_fh, effective_fa: scores actually used (prediction or real)
      - ohh, oah: opening handicap (Home, Away)
    """
    overrides = overrides or {}
    if not rows:
        return {
            "milestones": [None] * 9,
            "real_fh": 0, "real_fa": 0,
            "effective_fh": 0, "effective_fa": 0,
            "ohh": 0.0, "oah": 0.0,
        }

    real_fh = int(to_f(rows[-1].get("Home Score", 0)))
    real_fa = int(to_f(rows[-1].get("Away Score", 0)))
    fh = pred_fh if pred_fh is not None else real_fh
    fa = pred_fa if pred_fa is not None else real_fa
    fs = fh + fa
    ohh, oah = get_open_hcap(rows)

    # a1: pre-match handicap (last valid row before 1H_1')
    a1, a1_side = pre_match_chap(rows)
    if a1 is None:
        # fallback: just after kickoff
        for idx_1h, r in enumerate(rows):
            if re.match(r"^1H_", (r.get("Half") or "").strip()):
                a1, a1_side = back_chap(rows, idx_1h + 3)
                break

    # c1: mode of O/U Line in 1H_2'..1H_5' (excluding zero rows)
    early = in_range(rows, 1, 2, 5)
    c_vals = [to_f(r.get("Over/Under Line", 0)) for r in early]
    c1 = mode_of([v for v in c_vals if v != 0])

    goals = get_goals(rows)
    out: list[Optional[dict[str, Any]]] = []

    for i in range(9):
        ov = overrides.get(i, {}) if isinstance(overrides.get(i), dict) else {}

        if i == 0:
            sc = "0-0"
            gh = ga = 0
            _a = ov.get("a", a1)
            _a_side = a1_side
            _c = ov.get("c", c1)
            _e = None
        elif i - 1 < len(goals):
            gi, gh, ga = goals[i - 1]
            sc = f"{gh}-{ga}"
            _a_raw, _a_side = back_chap(rows, gi)
            _a = ov.get("a", _a_raw)
            _c = ov.get("c", back_val(rows, gi, "Over/Under Line", lambda v: v != 0))
            _e = ov.get("e", fwd_val(rows, gi, "Over/Under Line", lambda v: v != 0))
        else:
            out.append(None)
            continue

        # b: surplus goals of the favored side beyond the handicap, after the milestone
        a_abs = abs(_a) if _a is not None else 0
        if _a_side == "home":
            b = round((fh - gh) - ((fa - ga) + a_abs), 4)
        else:  # 'away' or 'level' or None
            b = round((fa - ga) - ((fh - gh) + a_abs), 4)

        d_v = round(_c - fs, 4) if _c is not None else None
        g_v = round(_e - fs, 4) if _e is not None else None

        out.append({
            "score": sc,
            "a": _a,
            "a_side": _a_side,
            "b": b,
            "c": _c,
            "d": d_v,
            "e": _e,
            "g": g_v,
        })

    return {
        "milestones": out,
        "real_fh": real_fh, "real_fa": real_fa,
        "effective_fh": fh, "effective_fa": fa,
        "ohh": ohh, "oah": oah,
    }
