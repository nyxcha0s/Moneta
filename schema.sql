-- MONETA DATABASE SCHEMA
-- Every record has a date. No exceptions.

CREATE TABLE IF NOT EXISTS config (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Pay events - entered manually when payday hits
CREATE TABLE IF NOT EXISTS paydays (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    expected_date TEXT NOT NULL,        -- the Wednesday
    actual_date TEXT,                   -- when it actually hit
    amount REAL,                        -- actual take-home entered by user
    period_type TEXT NOT NULL,          -- 'rent' or 'bill'
    notes TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Balance snapshots - "right now I have $X"
CREATE TABLE IF NOT EXISTS balance_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    amount REAL NOT NULL,
    snapshot_date TEXT NOT NULL,
    notes TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Rent - Flex-aware structure
CREATE TABLE IF NOT EXISTS rent (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    month TEXT NOT NULL,                -- 'YYYY-MM'
    base_amount REAL,                   -- total rent before Flex fees (unknown until ~25th)
    amount_confirmed INTEGER DEFAULT 0, -- 0=unknown, 1=confirmed
    flex_payment1 REAL,                 -- bulk portion + 1% fee
    flex_payment1_date TEXT,            -- always around the 1st
    flex_payment1_paid INTEGER DEFAULT 0,
    flex_payment2 REAL,                 -- $300 + $3 fee
    flex_payment2_date TEXT,            -- user-chosen payday-aligned date
    flex_payment2_paid INTEGER DEFAULT 0,
    flex_membership REAL DEFAULT 6.99,
    flex_membership_date TEXT,
    flex_membership_paid INTEGER DEFAULT 0,
    mystery_charges REAL DEFAULT 0,     -- landlord addons discovered late
    notes TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Bills - variable and fixed
CREATE TABLE IF NOT EXISTS bills (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    creditor TEXT,
    amount_type TEXT NOT NULL DEFAULT 'variable', -- 'fixed', 'variable', 'variable_seasonal'
    amount_current REAL,                -- this month's known/estimated amount
    amount_last REAL,                   -- last month actual
    amount_high REAL,                   -- historical high (Moneta budgets to this)
    due_day INTEGER,                    -- day of month (1-31)
    grace_period_days INTEGER DEFAULT 0,
    late_fee REAL DEFAULT 0,
    autopay INTEGER DEFAULT 0,          -- 0=manual, 1=autopay (DANGER FLAG near payday)
    hardship_program INTEGER DEFAULT 0,
    creditor_phone TEXT,
    due_date_moveable INTEGER DEFAULT 0, -- can we call and change this?
    due_date_moved INTEGER DEFAULT 0,    -- have we already done it?
    active INTEGER DEFAULT 1,
    priority INTEGER DEFAULT 5,         -- 1=critical (rent), 2=utilities, 3=car, 4=food, 5=debt, 6=optional
    notes TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Bill payment history
CREATE TABLE IF NOT EXISTS bill_payments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    bill_id INTEGER NOT NULL REFERENCES bills(id),
    amount REAL NOT NULL,
    payment_date TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'paid', -- 'paid', 'failed', 'partial', 'skipped'
    notes TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Subscriptions - with uncertainty states
CREATE TABLE IF NOT EXISTS subscriptions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    amount REAL,                        -- NULL if unknown
    billing_day INTEGER,                -- day of month, NULL if unknown
    status TEXT NOT NULL DEFAULT 'suspected', -- 'confirmed', 'suspected', 'bounced', 'cancelled'
    last_confirmed_date TEXT,
    last_bounce_date TEXT,
    autopay INTEGER DEFAULT 1,
    notes TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- One-off expenses
CREATE TABLE IF NOT EXISTS expenses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    description TEXT NOT NULL,
    amount REAL NOT NULL,
    expense_date TEXT NOT NULL,
    category TEXT,                      -- 'food', 'transport', 'medical', 'other'
    pre_approved INTEGER DEFAULT 0,     -- did user ask Moneta first?
    notes TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Pre-purchase checks - the ask-before-you-spend log
CREATE TABLE IF NOT EXISTS purchase_checks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    description TEXT NOT NULL,
    amount REAL NOT NULL,
    check_date TEXT NOT NULL,
    decision TEXT NOT NULL,             -- 'approved', 'denied', 'user_overrode'
    balance_at_check REAL,
    runway_days_at_check INTEGER,
    safe_amount_at_check REAL,
    reasoning TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Drift tracking - the exit ramp metric
CREATE TABLE IF NOT EXISTS drift_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    period_start TEXT NOT NULL,
    period_end TEXT NOT NULL,
    day_offset INTEGER NOT NULL,        -- days since payday
    balance REAL NOT NULL,
    prev_period_balance REAL,           -- same day-offset last period
    drift REAL,                         -- positive = ahead, negative = behind
    logged_date TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Triage events - when there isn't enough
CREATE TABLE IF NOT EXISTS triage_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_date TEXT NOT NULL,
    shortfall REAL NOT NULL,            -- how much short
    total_due REAL NOT NULL,
    available REAL NOT NULL,
    triage_plan TEXT NOT NULL,          -- JSON: what to pay, what to defer, why
    outcome TEXT,                       -- filled in later: what actually happened
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Conversation history
CREATE TABLE IF NOT EXISTS conversations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    session_id TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Seed config defaults
INSERT OR IGNORE INTO config (key, value, updated_at) VALUES
    ('payday_weekday', 'wednesday', datetime('now')),
    ('payday_interval_days', '14', datetime('now')),
    ('payday_time', '19:00', datetime('now')),
    ('next_payday', '2025-04-22', datetime('now')),
    ('flex_membership_amount', '6.99', datetime('now')),
    ('flex_payment2_amount', '300', datetime('now')),
    ('flex_payment2_fee', '3', datetime('now')),
    ('income_range_low', '2300', datetime('now')),
    ('income_range_high', '2500', datetime('now')),
    ('setup_complete', '0', datetime('now'));
