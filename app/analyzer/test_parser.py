"""Eyeball-verification test: run compute() on the bundled sample CSV
and print the 9 milestone rows in a layout similar to the original Tkinter tool.

Run:
    python -m app.analyzer.test_parser
or
    python app/analyzer/test_parser.py [path/to/file.csv]
"""
from __future__ import annotations

import glob
import os
import sys

# Force UTF-8 output on Windows consoles
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

# Allow `python app/analyzer/test_parser.py`
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from app.analyzer.parser import (  # noqa: E402
    compute,
    get_open_hcap,
    parse_fname,
    read_csv_path,
    validate,
)


def fmt(v, w=8):
    if v is None:
        return "-".rjust(w)
    if isinstance(v, float):
        s = f"{v:.4g}"
    else:
        s = str(v)
    return s.rjust(w)


def main(path: str) -> None:
    rows = read_csv_path(path)
    meta = parse_fname(path)
    errs = validate(rows)

    print("=" * 84)
    print(f"File   : {os.path.basename(path)}")
    print(f"Meta   : league={meta['league']!r}  date={meta['date']}  time={meta['time']}")
    print(f"Teams  : {meta['home']!r}  vs  {meta['away']!r}")
    print(f"Rows   : {len(rows)}   (header has {len(rows[0]) if rows else 0} columns)")
    if errs:
        print(f"Warn   : {errs}")
    ohh, oah = get_open_hcap(rows)
    print(f"Open HC: Home={ohh}  Away={oah}   "
          f"=> Được chấp = {meta['home'] if ohh > 0 else meta['away']}, "
          f"Chấp = {meta['home'] if ohh < 0 else meta['away']}")

    result = compute(rows, overrides={})
    print(f"Final  : real {result['real_fh']}-{result['real_fa']}   "
          f"effective {result['effective_fh']}-{result['effective_fa']}")
    print("=" * 84)
    print(f"{'#':>2} {'Score':>7} | {'[a]':>7} {'side':>5} {'b':>8} | "
          f"{'[c]':>6} {'d':>8} | {'[e]':>6} {'g':>8}")
    print("-" * 84)
    for i, m in enumerate(result["milestones"]):
        if m is None:
            print(f"{i:>2} {'-':>7} | {'-':>7} {'-':>5} {'-':>8} | "
                  f"{'-':>6} {'-':>8} | {'-':>6} {'-':>8}")
            continue
        print(
            f"{i:>2} {m['score']:>7} | "
            f"{fmt(m['a'], 7)} {(m['a_side'] or '-'):>5} {fmt(m['b'], 8)} | "
            f"{fmt(m['c'], 6)} {fmt(m['d'], 8)} | {fmt(m['e'], 6)} {fmt(m['g'], 8)}"
        )
    print("=" * 84)


if __name__ == "__main__":
    if len(sys.argv) > 1:
        target = sys.argv[1]
    else:
        here = os.path.dirname(os.path.abspath(__file__))
        candidates = glob.glob(os.path.join(here, "*.csv"))
        if not candidates:
            print("No CSV file found. Pass a path as argument.", file=sys.stderr)
            sys.exit(1)
        target = candidates[0]
    main(target)
