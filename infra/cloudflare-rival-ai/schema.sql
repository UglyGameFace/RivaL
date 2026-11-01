CREATE TABLE IF NOT EXISTS daily_budget (
    utc_day TEXT PRIMARY KEY NOT NULL,
    used INTEGER NOT NULL DEFAULT 0 CHECK(used >= 0 AND used <= 24),
    last_at TEXT NOT NULL
);
