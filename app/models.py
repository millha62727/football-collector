from datetime import datetime
from typing import Optional

from pydantic import BaseModel


class Match(BaseModel):
    id: str
    competition: str
    home: str
    away: str
    start_time_utc: datetime
    status: str
    minute: int
    home_score: int
    away_score: int
    home_handicap: Optional[str] = None
    home_handicap_odds: Optional[float] = None
    away_handicap: Optional[str] = None
    away_handicap_odds: Optional[float] = None
    ou_line: Optional[str] = None
    over_odds: Optional[float] = None
    under_odds: Optional[float] = None
    odds_1: Optional[float] = None
    odds_x: Optional[float] = None
    odds_2: Optional[float] = None
    raw_data: Optional[dict] = None
    last_seen: datetime
