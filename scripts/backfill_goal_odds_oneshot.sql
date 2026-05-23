-- One-time backfill: populate hc_before / hc_after / ou_before / ou_after
-- for goals that landed before the new collector logic was deployed.
--
-- Strategy mirrors backfill_goal_odds_after() but also reaches BACKWARDS
-- for the `before` columns. Window is unbounded (we want every goal).
--
-- Safe to run multiple times — COALESCE prevents overwriting populated values.

WITH targets AS (
    SELECT id, match_id, occurred_at
      FROM match_goals
     WHERE hc_before IS NULL OR hc_after IS NULL
        OR ou_before IS NULL OR ou_after IS NULL
),
picked AS (
    SELECT
        t.id,
        -- Last non-NULL snapshot at or before goal time
        (SELECT home_handicap
           FROM match_odds_history h
          WHERE h.match_id = t.match_id
            AND h.captured_at <= t.occurred_at
            AND h.home_handicap IS NOT NULL
          ORDER BY h.captured_at DESC LIMIT 1)         AS hc_before_pick,
        (SELECT ou_line
           FROM match_odds_history h
          WHERE h.match_id = t.match_id
            AND h.captured_at <= t.occurred_at
            AND h.ou_line IS NOT NULL
          ORDER BY h.captured_at DESC LIMIT 1)         AS ou_before_pick,
        -- First non-NULL snapshot after goal time
        (SELECT home_handicap
           FROM match_odds_history h
          WHERE h.match_id = t.match_id
            AND h.captured_at > t.occurred_at
            AND h.home_handicap IS NOT NULL
          ORDER BY h.captured_at ASC LIMIT 1)          AS hc_after_pick,
        (SELECT ou_line
           FROM match_odds_history h
          WHERE h.match_id = t.match_id
            AND h.captured_at > t.occurred_at
            AND h.ou_line IS NOT NULL
          ORDER BY h.captured_at ASC LIMIT 1)          AS ou_after_pick
      FROM targets t
)
UPDATE match_goals g
   SET hc_before = COALESCE(g.hc_before, p.hc_before_pick),
       ou_before = COALESCE(g.ou_before, p.ou_before_pick),
       hc_after  = COALESCE(g.hc_after,  p.hc_after_pick),
       ou_after  = COALESCE(g.ou_after,  p.ou_after_pick)
  FROM picked p
 WHERE g.id = p.id;

-- Audit
SELECT
    COUNT(*) AS total,
    COUNT(hc_before) AS hc_before_filled,
    COUNT(hc_after)  AS hc_after_filled,
    COUNT(ou_before) AS ou_before_filled,
    COUNT(ou_after)  AS ou_after_filled,
    COUNT(*) FILTER (WHERE hc_before IS NULL AND hc_after IS NULL
                       AND ou_before IS NULL AND ou_after IS NULL) AS still_all_null
  FROM match_goals;
