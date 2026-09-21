"""SQLite storage layer for the qpool quota ledger.

Deliberately uses ONE unified table for all providers (agent is just a column).
This avoids onWatch's schema anti-pattern of three tables per provider
(`{provider}_snapshots / _quota_values / _reset_cycles`, 40+ tables for 16 providers).
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS quota_entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    agent TEXT NOT NULL,                -- codex, claude-code, gemini-cli, cursor, kiro, ...
    account TEXT NOT NULL,              -- account label: work, personal, email, ...
    kind TEXT NOT NULL CHECK (kind IN ('subscription', 'credit_pack', 'payg')),
    capability_tier INTEGER NOT NULL DEFAULT 3 CHECK (capability_tier BETWEEN 1 AND 5),
    unit TEXT NOT NULL DEFAULT 'tokens', -- tokens / requests / credits / usd
    total REAL,                          -- quota per period (NULL for payg)
    remaining REAL,                      -- NULL for payg (metered, no fixed quota)
    cost_per_unit REAL NOT NULL DEFAULT 0,  -- marginal cost in USD per unit
    expires_at TEXT,                     -- ISO 8601 UTC; credit packs may expire
    reset_period TEXT CHECK (reset_period IN ('daily', 'weekly', 'monthly')),
    period_start TEXT,                   -- current billing period start (subscriptions)
    window_key TEXT,                     -- sync match key: five_hour/seven_day/current_period/credits
    window_seconds INTEGER,              -- rolling window length in seconds (overrides reset_period)
    used_percent REAL,                   -- last API reading, 0-100 (NULL for manual entries)
    last_synced_at TEXT,                 -- last successful `quota sync` for this entry
    notes TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- Append-only event log (event sourcing, borrowed from oh-my-codex's replay model).
-- Cost composition fields follow the model: total = input + output + tool + cache.
CREATE TABLE IF NOT EXISTS usage_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    entry_id INTEGER NOT NULL REFERENCES quota_entries(id) ON DELETE CASCADE,
    amount REAL NOT NULL,
    input_tokens INTEGER,
    output_tokens INTEGER,
    cached_tokens INTEGER,
    tool_calls INTEGER,
    est_cost_usd REAL NOT NULL DEFAULT 0,
    task_ref TEXT,
    note TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_usage_events_entry ON usage_events(entry_id);
CREATE INDEX IF NOT EXISTS idx_usage_events_created ON usage_events(created_at);
"""


def default_db_path() -> Path:
    env = os.environ.get("QPOOL_DB")
    if env:
        return Path(env).expanduser()
    return Path.home() / ".qpool" / "qpool.db"


class Database:
    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = path or default_db_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path))
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.executescript(SCHEMA)
        self._migrate()
        self.conn.commit()

    def _migrate(self) -> None:
        """Add columns introduced after v0.1 to pre-existing databases."""
        cols = {row[1] for row in self.conn.execute("PRAGMA table_info(quota_entries)")}
        for name, ddl in (
            ("window_key", "ALTER TABLE quota_entries ADD COLUMN window_key TEXT"),
            ("window_seconds", "ALTER TABLE quota_entries ADD COLUMN window_seconds INTEGER"),
            ("used_percent", "ALTER TABLE quota_entries ADD COLUMN used_percent REAL"),
            ("last_synced_at", "ALTER TABLE quota_entries ADD COLUMN last_synced_at TEXT"),
        ):
            if name not in cols:
                self.conn.execute(ddl)

    def close(self) -> None:
        self.conn.close()
