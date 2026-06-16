# Migrations — football-collector

> Idempotent SQL migrations cho Postgres 16. Mỗi file numbered (NNN_description.sql) và chạy được nhiều lần an toàn.

## Convention

- **File naming**: `NNN_short_description.sql` (3 chữ số, snake_case, ascending order)
- **Idempotent**: mỗi migration phải dùng `IF NOT EXISTS` / `IF EXISTS` / check trước khi ALTER
- **Rollback**: mỗi file có comment ở đầu ghi rollback command tương ứng
- **Run order**: từ thấp → cao, đảm bảo forward-only (rollback là manual)

## Run trên VPS

```bash
# 1. Copy file vào container
docker cp app/migrations/004_add_open_hc_side.sql football_dashboard:/tmp/

# 2. Apply qua psql
docker exec -i football_db psql -U football -d football_collector < /tmp/004_add_open_hc_side.sql

# 3. Verify
docker exec football_db psql -U football -d football_collector -c "\d match_patterns" | grep open_hc_side
```

## Backfill sau migration

Sau khi schema update xong, chạy backfill script để populate cột mới từ `raw_features` JSONB:

```bash
# Dry-run trước (xem sẽ update bao nhiêu rows)
docker exec football_dashboard python3 scripts/backfill_open_hc_side.py --dry-run

# Apply
docker exec football_dashboard python3 scripts/backfill_open_hc_side.py --apply

# Verify
docker exec football_dashboard python3 scripts/backfill_open_hc_side.py --verify
```

## Lịch sử migrations

| # | Tên | Mô tả | Ngày |
|---|---|---|---|
| 001 | (chưa có) | Initial schema | — |
| 002 | (chưa có) | — | — |
| 003 | (chưa có) | — | — |
| 004 | `add_open_hc_side` | Thêm `open_hc_side TEXT` để `tag_outcome_validate` validate cover/over rate chính xác (không cần extract JSONB mỗi query) | 2026-06-16 |
