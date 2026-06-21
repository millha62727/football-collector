-- Schema for goal1_paper_sim service.
-- Persists every "paper trade" the simulator places (in real time, on goal 1)
-- so the daily report can roll them up. Paper trades never leave this table —
-- they are not bets at any bookmaker.

CREATE TABLE IF NOT EXISTS goal1_paper_trades (
    id              bigserial PRIMARY KEY,
    match_id        text        NOT NULL REFERENCES matches(id) ON DELETE CASCADE,
    -- When did goal 1 happen (derived from match_odds_history score changes)?
    goal1_minute    integer     NOT NULL,
    goal1_detected_at  timestamp with time zone NOT NULL DEFAULT NOW(),
    -- Opening odds (taken from first snapshot of match_odds_history).
    -- We use opening handicap / opening OU as the filter inputs.
    open_hc         text,
    open_ou         text,
    open_ou_value   numeric(6,3),     -- parsed float for filter; NULL if unparseable
    -- Odds at the moment we would have placed the bet (first snapshot AT or
    -- AFTER goal1_minute with both over_odds AND under_odds non-null).
    over_odds       numeric(8,4) NOT NULL,
    under_odds      numeric(8,4) NOT NULL,
    over_decimal    numeric(8,4) NOT NULL,    -- 1 + over_odds (positive malay) or 1 + 1/abs(over_odds) (negative)
    vig             numeric(6,4) NOT NULL,    -- 1/od + 1/ud - 1
    ou_line_at_bet  text,
    -- Paper-trade economics.
    stake           numeric(10,2) NOT NULL DEFAULT 10.00,
    -- Resolved when the match finishes. ft_home/ft_away/total copied from matches.
    ft_home         integer,
    ft_away         integer,
    ft_total        integer,
    will_win        boolean,                  -- (ft_home + ft_away) > 1
    pnl             numeric(10,2),            -- stake * (over_decimal - 1) if win else -stake
    resolved_at     timestamp with time zone,
    -- Day bucket (VN time, UTC+7). Matches starting < 00:00 VN go to the
    -- previous day, after 00:00 to the new day. Stored as DATE for easy grouping.
    trade_day_vn    date        NOT NULL,
    UNIQUE (match_id, goal1_minute)          -- don't double-place if loop re-detects
);

CREATE INDEX IF NOT EXISTS idx_goal1_trades_day    ON goal1_paper_trades (trade_day_vn);
CREATE INDEX IF NOT EXISTS idx_goal1_trades_resolved ON goal1_paper_trades (resolved_at)
    WHERE resolved_at IS NULL;