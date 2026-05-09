import sqlite3
import json
import os
from datetime import datetime
from .models import Match

# Tự động lấy đường dẫn đúng cho Windows và Linux
def get_db_path():
    """Trả về đường dẫn database phù hợp với hệ điều hành"""
    # Lấy thư mục gốc của project
    base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    # Tạo đường dẫn đến file database
    db_path = os.path.join(base_dir, "data", "football.db")
    return db_path

DB_PATH = get_db_path()

def init_db():
    """Khởi tạo bảng matches nếu chưa tồn tại"""
    # Đảm bảo thư mục data tồn tại (cả Windows và Linux)
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    
    print(f"📁 Database path: {DB_PATH}")
    
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute('''
            CREATE TABLE IF NOT EXISTS matches (
                id TEXT PRIMARY KEY,
                competition TEXT,
                home TEXT,
                away TEXT,
                start_time_utc TEXT,
                status TEXT,
                minute INTEGER,
                home_score INTEGER,
                away_score INTEGER,
                home_handicap TEXT,
                home_handicap_odds REAL,
                away_handicap TEXT,
                away_handicap_odds REAL,
                ou_line TEXT,
                over_odds REAL,
                under_odds REAL,
                odds_1 REAL,
                odds_x REAL,
                odds_2 REAL,
                raw_data TEXT,
                last_seen TEXT
            )
        ''')
        # Tạo index để truy vấn nhanh hơn
        conn.execute('CREATE INDEX IF NOT EXISTS idx_status ON matches(status)')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_start_time ON matches(start_time_utc)')
        
        print("✅ Database initialized successfully!")

def upsert_match(match: Match):
    """Thêm mới hoặc cập nhật trận đấu"""
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute('''
            INSERT OR REPLACE INTO matches VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
            )
        ''', (
            match.id, match.competition, match.home, match.away,
            match.start_time_utc.isoformat(), match.status, match.minute,
            match.home_score, match.away_score,
            match.home_handicap, match.home_handicap_odds,
            match.away_handicap, match.away_handicap_odds,
            match.ou_line, match.over_odds, match.under_odds,
            match.odds_1, match.odds_x, match.odds_2,
            json.dumps(match.raw_data) if match.raw_data else None,
            match.last_seen.isoformat()
        ))

def get_live_matches():
    """Lấy danh sách các trận đang diễn ra"""
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        cur = conn.execute("SELECT * FROM matches WHERE status IN ('LIVE', 'H1', 'H2', 'INJURY_TIME_H1', 'INJURY_TIME_H2')")
        return [dict(row) for row in cur.fetchall()]

def get_all_matches():
    """Lấy tất cả trận đấu"""
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        cur = conn.execute("SELECT * FROM matches ORDER BY start_time_utc DESC")
        return [dict(row) for row in cur.fetchall()]