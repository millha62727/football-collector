import asyncio
import aiohttp
import os
import sys
import logging
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv

# Thêm thư mục hiện tại vào sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Import các module của chúng ta
from app.database import init_db, upsert_match
from app.parser import parse_match

# Load cấu hình từ file .env (sẽ tạo sau)
load_dotenv()

# Cấu hình logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)

# Đọc cấu hình từ biến môi trường
API_BASE = os.getenv("API_BASE_URL", "https://be.sb21.net/api/v2")
TIMEOUT = int(os.getenv("API_TIMEOUT", 10))

async def fetch_json(session, url):
    """Gọi API và lấy dữ liệu JSON với headers đầy đủ"""
    headers = {
        "accept": "application/json",
        "content-type": "application/json",
        "lng": "vi"
    }
    try:
        async with session.get(url, headers=headers, timeout=TIMEOUT) as resp:
            if resp.status == 200:
                return await resp.json()
            else:
                logging.error(f"API error {resp.status} for {url}")
                return None
    except Exception as e:
        logging.error(f"Fetch error: {e} for {url}")
        return None

async def fetch_all_matches():
    """Lấy dữ liệu trận đấu của hôm nay và ngày mai"""
    today_url = f"{API_BASE}/getEvent?timeRange=today&sportType=1_1&sportId=1&oddsStyle=ma&pinLeague=false"
    
    tomorrow = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
    tomorrow_url = f"{API_BASE}/getEventByDate?date={tomorrow}&sportType=1_1&sportId=1&oddsStyle=ma&pinLeague=false"
    
    async with aiohttp.ClientSession() as session:
        today_data, tomorrow_data = await asyncio.gather(
            fetch_json(session, today_url),
            fetch_json(session, tomorrow_url)
        )
    
    return today_data, tomorrow_data

def extract_matches(data):
    """
    Duyệt cấu trúc JSON lồng nhau để lấy danh sách trận đấu
    Cấu trúc: [[competitions...], [competitions...]]
    Mỗi competition có: {"1": "Tên giải", "2": [matches...]}
    """
    matches = []
    if not isinstance(data, list):
        return matches
    
    for item in data:
        if isinstance(item, list):
            for comp in item:
                if isinstance(comp, dict) and "2" in comp:
                    comp_name = comp.get("1", "Unknown")
                    for match in comp.get("2", []):
                        matches.append((comp_name, match))
    
    return matches

def should_skip_league(comp_name: str) -> bool:
    """Kiểm tra xem có bỏ qua giải đấu này không (ảo, esports, ...)"""
    skip_keywords = ['ảo', 'virtual', 'esports', 'soccer battle', 'điện tử']
    comp_lower = comp_name.lower().strip()
    return any(keyword in comp_lower for keyword in skip_keywords)

async def update_loop():
    """Vòng lặp chính: lấy dữ liệu và lưu vào database"""
    
    # Khởi tạo database
    init_db()
    logging.info("Database initialized")
    
    loop_count = 0
    
    while True:
        try:
            loop_count += 1
            logging.info(f"=== Loop {loop_count} ===")
            
            # Lấy dữ liệu từ API
            today, tomorrow = await fetch_all_matches()
            
            if today is None and tomorrow is None:
                logging.warning("Failed to fetch data from API, retrying in 30 seconds...")
                await asyncio.sleep(30)
                continue
            
            # Trích xuất danh sách trận đấu
            all_matches = []
            if today:
                all_matches.extend(extract_matches(today))
            if tomorrow:
                all_matches.extend(extract_matches(tomorrow))
            
            logging.info(f"Found {len(all_matches)} matches total")
            
            # Xử lý từng trận
            saved_count = 0
            skipped_count = 0
            
            for comp_name, match_json in all_matches:
                # Bỏ qua giải ảo
                if should_skip_league(comp_name):
                    skipped_count += 1
                    continue
                
                try:
                    # Parse và lưu
                    match = parse_match(comp_name, match_json)
                    upsert_match(match)
                    saved_count += 1
                except Exception as e:
                    logging.error(f"Error parsing match {comp_name}: {e}")
            
            logging.info(f"Saved: {saved_count}, Skipped (virtual): {skipped_count}")
            
            # Đợi 30 giây trước khi lấy dữ liệu tiếp theo
            await asyncio.sleep(30)
            
        except Exception as e:
            logging.error(f"Update loop error: {e}", exc_info=True)
            await asyncio.sleep(60)

def main():
    """Hàm chính"""
    print("=" * 50)
    print("FOOTBALL DATA COLLECTOR")
    print("=" * 50)
    print(f"API URL: {API_BASE}")
    print(f"Timeout: {TIMEOUT} seconds")
    print("Starting collector...")
    print("=" * 50)
    
    # Chạy vòng lặp bất đồng bộ
    asyncio.run(update_loop())

if __name__ == "__main__":
    main()