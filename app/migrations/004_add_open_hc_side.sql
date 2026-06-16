-- Migration 004: add open_hc_side column to match_patterns
-- Date: 2026-06-16
-- Reason: tag_outcome_validate() cần biết favorite là home hay away để tính
--         cover rate chính xác. open_hc (TEXT) chỉ lưu magnitude string
--         ('-0.5'), không có side. raw_features->>'opening_hc_side' có sẵn
--         nhưng không index được → add column denormalized.
--
-- Rollback:
--   ALTER TABLE match_patterns DROP COLUMN IF EXISTS open_hc_side;
--
-- Idempotent: dùng IF NOT EXISTS, có thể chạy nhiều lần an toàn.

BEGIN;

-- Add column nếu chưa có. NULL allowed cho pattern cũ (sẽ backfill sau).
ALTER TABLE match_patterns
    ADD COLUMN IF NOT EXISTS open_hc_side TEXT;

-- Document constraints (chỉ là comment, không enforce — pattern cũ có thể NULL)
COMMENT ON COLUMN match_patterns.open_hc_side IS
    'Side của favorite: home (home handicap < 0) | away (away handicap < 0) | level (line = 0). NULL = pattern cũ trước migration 004, cần backfill từ raw_features->>''opening_hc_side''.';

COMMIT;
