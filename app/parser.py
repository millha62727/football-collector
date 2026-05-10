import re
from datetime import datetime, timezone
from typing import Dict, Optional, Tuple

from .models import Match


def _parse_handicap(text: str) -> Tuple[Optional[str], float, Optional[str], float]:
    """Parse Asian handicap string, e.g. '0.25 0.92*123h 0.92*123a a'"""
    parts = text.split()
    if len(parts) < 2:
        return None, 0.0, None, 0.0

    handicap = parts[0]
    home_m = re.search(r'([-\d.]+)\*\d+h', text)
    away_m = re.search(r'([-\d.]+)\*\d+a', text)
    home_odds = float(home_m.group(1)) if home_m else 0.0
    away_odds = float(away_m.group(1)) if away_m else 0.0

    # Side indicator: which team concedes (h = home gives away, a = away gives home)
    side = parts[3] if len(parts) > 3 and parts[3] in ('h', 'a') else None

    try:
        hval = float(handicap)
    except ValueError:
        hval = 0.0

    if side == 'a':
        home_h = handicap
        away_h = f"-{handicap}" if hval != 0 else "0"
    elif side == 'h':
        home_h = f"-{handicap}" if hval != 0 else "0"
        away_h = handicap
    else:
        home_h = away_h = "0"

    return home_h, home_odds, away_h, away_odds


def _parse_overunder(text: str) -> Tuple[Optional[str], float, float]:
    """Parse over/under string, e.g. '2.5 0.95*123h 0.85*123a'"""
    parts = text.split()
    if not parts:
        return None, 0.0, 0.0

    line = parts[0]
    over_m = re.search(r'([-\d.]+)\*\d+h', text)
    under_m = re.search(r'([-\d.]+)\*\d+a', text)
    over_odds = float(over_m.group(1)) if over_m else 0.0
    under_odds = float(under_m.group(1)) if under_m else 0.0

    return line, over_odds, under_odds


def _parse_1x2(text: str) -> Tuple[float, float, float]:
    """Parse 1X2 string, e.g. '1.12*123h 18.0*123a 6.75*123d'"""
    home_m = re.search(r'([-\d.]+)\*\d+h', text)
    away_m = re.search(r'([-\d.]+)\*\d+a', text)
    draw_m = re.search(r'([-\d.]+)\*\d+d', text)
    return (
        float(home_m.group(1)) if home_m else 0.0,
        float(draw_m.group(1)) if draw_m else 0.0,
        float(away_m.group(1)) if away_m else 0.0,
    )


def _get_status(match_json: Dict) -> str:
    """
    Determine match status from API data.
    Status code takes priority over elapsed-minute heuristic.
    """
    status_code = match_json.get("10", 0)
    minute_raw = match_json.get("6") or 0
    minute = minute_raw // 60000 if minute_raw else 0

    # Finished flag is strongest signal
    if match_json.get("16"):
        return "FT"

    # Explicit status codes
    CODE_MAP = {
        0: "UPCOMING",
        1: "UPCOMING",
        2: "LIVE",   # H1
        3: "HT",
        4: "HT",
        5: "LIVE",   # H2
        6: "LIVE",   # Extra time
        7: "LIVE",   # Penalties
        8: "LIVE",   # Another live variant
        9: "FT",
    }
    if status_code in CODE_MAP:
        return CODE_MAP[status_code]

    # Fallback: if elapsed time suggests live
    if minute > 0:
        return "LIVE"
    return "UPCOMING"


def parse_match(comp_name: str, match_json: Dict) -> Match:
    """Convert raw API match JSON into a typed Match object."""
    start_utc = datetime.strptime(
        match_json["0"], "%Y-%m-%dT%H:%M:%SZ"
    ).replace(tzinfo=timezone.utc)

    home = match_json.get("2", "Unknown")
    away = match_json.get("3", "Unknown")

    score = match_json.get("4", {})
    home_score = int(score.get("0", 0))
    away_score = int(score.get("1", 0))

    minute_raw = match_json.get("6") or 0
    minute = minute_raw // 60000 if minute_raw else 0

    status = _get_status(match_json)
    match_id = f"{comp_name}_{home}_{away}_{int(start_utc.timestamp())}"

    odds = match_json.get("7", {})

    # Handicap
    hc_list = odds.get("5", [])
    home_h = away_h = None
    home_h_odds = away_h_odds = None
    if hc_list:
        home_h, home_h_odds, away_h, away_h_odds = _parse_handicap(hc_list[0])

    # Over/Under
    ou_list = odds.get("3", [])
    ou_line = over_odds = under_odds = None
    if ou_list:
        ou_line, over_odds, under_odds = _parse_overunder(ou_list[0])

    # 1X2
    o1x2_list = odds.get("1", [])
    o1 = oX = o2 = None
    if o1x2_list:
        o1, oX, o2 = _parse_1x2(o1x2_list[0])

    return Match(
        id=match_id,
        competition=comp_name,
        home=home,
        away=away,
        start_time_utc=start_utc,
        status=status,
        minute=minute,
        home_score=home_score,
        away_score=away_score,
        home_handicap=home_h,
        home_handicap_odds=home_h_odds,
        away_handicap=away_h,
        away_handicap_odds=away_h_odds,
        ou_line=ou_line,
        over_odds=over_odds,
        under_odds=under_odds,
        odds_1=o1,
        odds_x=oX,
        odds_2=o2,
        raw_data=match_json,
        last_seen=datetime.now(timezone.utc),
    )
