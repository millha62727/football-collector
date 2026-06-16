-- Phase 3.6: Verify tag_outcome_validate trên data thật.
--
-- Chạy trên VPS qua `docker exec football_db psql -U football -d football_collector`
-- rồi paste output cho tôi phân tích.
--
-- Mục đích: xem top 20 tags có đủ valid_n >= 15 để hiển thị metric không,
-- và nếu đủ thì fav_cover_rate_actual trông có hợp lý không.

-- 1. Overall: bao nhiêu pattern có parse_ok=TRUE + open_hc + side + scores?
SELECT
    COUNT(*)                                                  AS total_patterns,
    COUNT(*) FILTER (WHERE open_hc IS NOT NULL)               AS has_open_hc,
    COUNT(*) FILTER (
        WHERE open_hc IS NOT NULL AND open_hc_side IS NOT NULL
    )                                                         AS has_side,
    COUNT(*) FILTER (
        WHERE open_hc IS NOT NULL AND open_hc_side IN ('home','away')
    )                                                         AS cover_eligible
FROM match_patterns p
JOIN matches m ON m.id = p.match_id
WHERE p.parse_ok = TRUE
  AND m.home_score IS NOT NULL
  AND m.away_score IS NOT NULL;

-- 2. Top 20 tags (chưa tính outcome) — chỉ để biết tag nào phổ biến
SELECT tag, COUNT(*) AS n
FROM match_patterns p, unnest(p.tags) AS tag
JOIN matches m ON m.id = p.match_id
WHERE p.parse_ok = TRUE
GROUP BY tag
ORDER BY n DESC
LIMIT 20;

-- 3. Per-tag outcome (chạy Python sau khi có output)
-- Bước này làm trong code, không SQL được. Nhưng có thể pre-aggregate trong
-- SQL bằng cách tính margin + cover cho từng pattern, rồi group theo tag.
-- Xem file `app/test_tag_outcome.py` để biết formula.
