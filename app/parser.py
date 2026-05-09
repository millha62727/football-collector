import re
from datetime import datetime, timezone
from typing import Dict, Tuple
from .models import Match

def parse_handicap(text: str) -> Tuple[str, float, str, float]:
    """
    Parse kèo chấp từ chuỗi API
    Ví dụ: "0.25 0.92*123h 0.92*123a a" -> handicap, odds home, handicap away, odds away
    """
    parts = text.split()
    if len(parts) < 4:
        return None, 0.0, None, 0.0
    
    handicap = parts[0]
    
    # Tìm odds home (kết thúc bằng 'h') và odds away (kết thúc bằng 'a')
    home_match = re.search(r'([-\d.]+)\*\d+h', text)
    away_match = re.search(r'([-\d.]+)\*\d+a', text)
    
    home_odds = float(home_match.group(1)) if home_match else 0.0
    away_odds = float(away_match.group(1)) if away_match else 0.0
    
    # Xác định bên nào được chấp (h = home, a = away)
    side = parts[3] if len(parts) > 3 and parts[3] in ('h', 'a') else None
    
    if side == 'a':
        # Away được chấp -> home chấp away
        home_h = handicap
        away_h = f"-{handicap}" if float(handicap) != 0 else "0"
    elif side == 'h':
        # Home được chấp -> away chấp home
        home_h = f"-{handicap}" if float(handicap) != 0 else "0"
        away_h = handicap
    else:
        # Kèo đồng banh
        home_h = away_h = "0"
    
    return home_h, home_odds, away_h, away_odds

def parse_overunder(text: str) -> Tuple[str, float, float]:
    """
    Parse kèo tài xỉu
    Ví dụ: "2.5 0.95*123h 0.85*123a" -> line, over_odds, under_odds
    """
    parts = text.split()
    if len(parts) < 2:
        return None, 0.0, 0.0
    
    line = parts[0]
    
    over_match = re.search(r'([-\d.]+)\*\d+h', text)
    under_match = re.search(r'([-\d.]+)\*\d+a', text)
    
    over_odds = float(over_match.group(1)) if over_match else 0.0
    under_odds = float(under_match.group(1)) if under_match else 0.0
    
    return line, over_odds, under_odds

def parse_1x2(text: str) -> Tuple[float, float, float]:
    """
    Parse kèo 1X2
    Ví dụ: "1.12*123h 18.0*123a 6.75*123d" -> home, draw, away
    """
    home_match = re.search(r'([-\d.]+)\*\d+h', text)
    away_match = re.search(r'([-\d.]+)\*\d+a', text)
    draw_match = re.search(r'([-\d.]+)\*\d+d', text)
    
    home = float(home_match.group(1)) if home_match else 0.0
    away = float(away_match.group(1)) if away_match else 0.0
    draw = float(draw_match.group(1)) if draw_match else 0.0
    
    return home, draw, away

def get_match_status(match_data: Dict) -> str:
    """Xác định trạng thái trận đấu dựa trên dữ liệu API"""
    minute = match_data.get("6", 0) // 60000 if match_data.get("6") else 0
    status_code = match_data.get("10", 0)
    
    if minute <= 0:
        return "UPCOMING"
    elif status_code == 2:
        return "LIVE"
    elif status_code == 4:
        return "HT"
    elif status_code == 8:
        return "LIVE"
    elif match_data.get("16"):
        return "FT"
    
    return "UNKNOWN"

def parse_match(comp_name: str, match_json: Dict) -> Match:
    """Chuyển đổi JSON của một trận đấu thành đối tượng Match"""
    
    # Thời gian bắt đầu (UTC)
    start_utc = datetime.strptime(match_json["0"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    
    # Thông tin đội bóng
    home = match_json.get("2", "Unknown")
    away = match_json.get("3", "Unknown")
    
    # Tỷ số
    score_data = match_json.get("4", {})
    home_score = int(score_data.get("0", 0))
    away_score = int(score_data.get("1", 0))
    
    # Phút hiện tại (API trả về milliseconds)
    minute = match_json.get("6", 0) // 60000 if match_json.get("6") else 0
    
    # Trạng thái
    status = get_match_status(match_json)
    
    # Tạo ID duy nhất cho trận đấu
    match_id = f"{comp_name}_{home}_{away}_{start_utc.timestamp()}"
    
    # Lấy odds
    odds = match_json.get("7", {})
    
    # Handicap (kèo chấp) - lấy phần tử đầu tiên
    hc_list = odds.get("5", [])
    home_h = away_h = None
    home_h_odds = away_h_odds = None
    if hc_list:
        home_h, home_h_odds, away_h, away_h_odds = parse_handicap(hc_list[0])
    
    # Over/Under (tài xỉu) - lấy phần tử đầu tiên
    ou_list = odds.get("3", [])
    ou_line = over_odds = under_odds = None
    if ou_list:
        ou_line, over_odds, under_odds = parse_overunder(ou_list[0])
    
    # 1X2 - lấy phần tử đầu tiên
    o1x2_list = odds.get("1", [])
    o1 = oX = o2 = None
    if o1x2_list:
        o1, oX, o2 = parse_1x2(o1x2_list[0])
    
    # Tạo đối tượng Match
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
        last_seen=datetime.now(timezone.utc)
    )