from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from datetime import datetime
import sqlite3
import os

app = FastAPI(title="Football Data Dashboard")

# Đường dẫn database
DB_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "football.db")

def get_db_connection():
    """Kết nối database"""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

@app.get("/", response_class=HTMLResponse)
async def home():
    """Trang chủ hiển thị danh sách trận đấu"""
    conn = get_db_connection()
    
    # Lấy tất cả trận đấu
    matches = conn.execute("""
        SELECT * FROM matches 
        ORDER BY start_time_utc DESC 
        LIMIT 100
    """).fetchall()
    
    # Đếm số lượng theo trạng thái
    stats = conn.execute("""
        SELECT 
            status,
            COUNT(*) as count 
        FROM matches 
        GROUP BY status
    """).fetchall()
    
    conn.close()
    
    # Tạo HTML
    html = """
    <!DOCTYPE html>
    <html>
    <head>
        <title>Football Data Dashboard</title>
        <meta http-equiv="refresh" content="30">
        <style>
            * { margin: 0; padding: 0; box-sizing: border-box; }
            body { 
                font-family: Arial, sans-serif; 
                background: #f0f2f5; 
                padding: 20px;
            }
            .container { max-width: 1400px; margin: 0 auto; }
            h1 { color: #1a73e8; margin-bottom: 20px; }
            .stats {
                display: grid;
                grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
                gap: 15px;
                margin-bottom: 30px;
            }
            .stat-card {
                background: white;
                padding: 15px;
                border-radius: 10px;
                box-shadow: 0 2px 4px rgba(0,0,0,0.1);
                text-align: center;
            }
            .stat-card h3 { font-size: 14px; color: #666; margin-bottom: 5px; }
            .stat-card .number { font-size: 28px; font-weight: bold; color: #1a73e8; }
            table {
                width: 100%;
                background: white;
                border-radius: 10px;
                overflow: hidden;
                box-shadow: 0 2px 4px rgba(0,0,0,0.1);
            }
            th, td {
                padding: 12px;
                text-align: left;
                border-bottom: 1px solid #eee;
            }
            th {
                background: #1a73e8;
                color: white;
                font-weight: 600;
            }
            tr:hover { background: #f5f5f5; }
            .live { color: #e53935; font-weight: bold; }
            .upcoming { color: #fb8c00; }
            .ft { color: #43a047; }
            .badge {
                display: inline-block;
                padding: 4px 8px;
                border-radius: 4px;
                font-size: 12px;
                font-weight: bold;
            }
            .badge-live { background: #e53935; color: white; }
            .badge-upcoming { background: #fb8c00; color: white; }
            .badge-ft { background: #43a047; color: white; }
            .badge-other { background: #757575; color: white; }
        </style>
    </head>
    <body>
        <div class="container">
            <h1>⚽ Football Data Dashboard</h1>
            
            <div class="stats">
    """
    
    for stat in stats:
        status = stat['status']
        count = stat['count']
        html += f"""
                <div class="stat-card">
                    <h3>{status}</h3>
                    <div class="number">{count}</div>
                </div>
        """
    
    html += """
            </div>
            
            <table>
                <thead>
                    <tr>
                        <th>Competition</th>
                        <th>Home</th>
                        <th>Away</th>
                        <th>Score</th>
                        <th>Status</th>
                        <th>Minute</th>
                        <th>Last Update</th>
                    </tr>
                </thead>
                <tbody>
    """
    
    for match in matches:
        status = match['status']
        status_class = ""
        badge_class = ""
        
        if "LIVE" in status or "H1" in status or "H2" in status:
            badge_class = "badge-live"
        elif "UPCOMING" in status:
            badge_class = "badge-upcoming"
        elif "FT" in status:
            badge_class = "badge-ft"
        else:
            badge_class = "badge-other"
        
        html += f"""
                    <tr>
                        <td>{match['competition'][:40]}</td>
                        <td><strong>{match['home']}</strong></td>
                        <td><strong>{match['away']}</strong></td>
                        <td style="font-weight: bold; text-align: center;">
                            {match['home_score']} - {match['away_score']}
                        </td>
                        <td><span class="badge {badge_class}">{match['status']}</span></td>
                        <td>{match['minute']}'</td>
                        <td>{match['last_seen'][11:19] if match['last_seen'] else '-'}</td>
                    </tr>
        """
    
    html += """
                </tbody>
            </table>
            <p style="margin-top: 20px; color: #666; text-align: center;">
                Auto-refresh every 30 seconds | Last update: """ + datetime.now().strftime("%Y-%m-%d %H:%M:%S") + """
            </p>
        </div>
    </body>
    </html>
    """
    
    return HTMLResponse(content=html)

@app.get("/api/matches")
async def get_matches():
    """API trả về danh sách trận đấu dạng JSON"""
    conn = get_db_connection()
    matches = conn.execute("SELECT * FROM matches ORDER BY start_time_utc DESC LIMIT 50").fetchall()
    conn.close()
    return [dict(match) for match in matches]

@app.get("/api/live")
async def get_live():
    """API trả về trận đang diễn ra"""
    conn = get_db_connection()
    matches = conn.execute("""
        SELECT * FROM matches 
        WHERE status IN ('LIVE', 'H1', 'H2') 
        ORDER BY start_time_utc
    """).fetchall()
    conn.close()
    return [dict(match) for match in matches]