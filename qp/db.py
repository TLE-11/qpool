"""SQLite storage layer for the qpool quota ledger.

Deliberately uses ONE unified table for all providers (agent is just a column).
This avoids onWatch's schema anti-pattern of three tables per provider
(`{provider}_snapshots / _quota_values / _reset_cycles`, 40+ tables for 16 providers).
"""

from __future__ import annotations

import os
import sqlite3
import os
import tempfile
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
        version = self.conn.execute("PRAGMA user_version").fetchone()[0]
        if version > 2:
            self.conn.close()
            raise RuntimeError("database is newer than this qpool; use a matching version")
        existing = self.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name = 'quota_entries'").fetchone()
        if existing and version < 2:
            backup = self.path.with_name(self.path.name + ".pre-pools-v1.bak")
            if not backup.exists():
                fd, temporary = tempfile.mkstemp(prefix=self.path.name + ".backup-", dir=self.path.parent)
                os.close(fd)
                destination = sqlite3.connect(temporary)
                try:
                    self.conn.backup(destination)
                    destination.close()
                    try:
                        os.link(temporary, backup)  # publish only a complete backup, without overwriting
                    except FileExistsError:
                        pass
                finally:
                    destination.close()
                    os.unlink(temporary)
        self.conn.executescript(SCHEMA)
        try:
            self.conn.execute("BEGIN IMMEDIATE")
            self._migrate()
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            self.conn.close()
            raise

    def _migrate(self) -> None:
        """Add columns introduced after v0.1 to pre-existing databases."""
        cols = {row[1] for row in self.conn.execute("PRAGMA table_info(quota_entries)")}
        for name, ddl in (
            ("window_key", "ALTER TABLE quota_entries ADD COLUMN window_key TEXT"),
            ("window_seconds", "ALTER TABLE quota_entries ADD COLUMN window_seconds INTEGER"),
            ("used_percent", "ALTER TABLE quota_entries ADD COLUMN used_percent REAL"),
            ("last_synced_at", "ALTER TABLE quota_entries ADD COLUMN last_synced_at TEXT"),
            ("pool_id", "ALTER TABLE quota_entries ADD COLUMN pool_id TEXT REFERENCES quota_pools(id)"),
            ("scope", "ALTER TABLE quota_entries ADD COLUMN scope TEXT NOT NULL DEFAULT '*'"),
            ("role", "ALTER TABLE quota_entries ADD COLUMN role TEXT NOT NULL DEFAULT 'limit'"),
            ("source_kind", "ALTER TABLE quota_entries ADD COLUMN source_kind TEXT NOT NULL DEFAULT 'legacy'"),
            ("resets_at", "ALTER TABLE quota_entries ADD COLUMN resets_at TEXT"),
        ):
            if name not in cols:
                self.conn.execute(ddl)
        self.conn.execute("""CREATE TABLE IF NOT EXISTS quota_pools (
            id TEXT PRIMARY KEY, agent TEXT NOT NULL, account_id TEXT NOT NULL,
            account_label TEXT NOT NULL, subject TEXT NOT NULL,
            kind TEXT NOT NULL, capability_tier INTEGER NOT NULL,
            unit TEXT NOT NULL, cost_per_unit REAL NOT NULL DEFAULT 0,
            expires_at TEXT, windows TEXT NOT NULL, max_age_seconds REAL NOT NULL,
            last_batch_at TEXT, last_attempt_at TEXT
        )""")
        pool_cols = {r[1] for r in self.conn.execute("PRAGMA table_info(quota_pools)")}
        if "last_attempt_at" not in pool_cols:
            self.conn.execute("ALTER TABLE quota_pools ADD COLUMN last_attempt_at TEXT")
        self.conn.execute("""CREATE UNIQUE INDEX IF NOT EXISTS idx_pool_window
            ON quota_entries(pool_id, scope, window_key) WHERE pool_id IS NOT NULL""")
        self.conn.execute("""CREATE TABLE IF NOT EXISTS imported_usage (
            id INTEGER PRIMARY KEY, namespace TEXT NOT NULL, request_id TEXT,
            provider TEXT, source TEXT, model TEXT, pool_id TEXT REFERENCES quota_pools(id),
            account_id TEXT, amount REAL, input_tokens INTEGER, output_tokens INTEGER,
            cached_tokens INTEGER, est_cost_usd REAL, status TEXT NOT NULL,
            reason TEXT, occurred_at TEXT NOT NULL, created_at TEXT NOT NULL
        )""")
        self.conn.execute("""CREATE UNIQUE INDEX IF NOT EXISTS idx_imported_request
            ON imported_usage(namespace, request_id) WHERE request_id IS NOT NULL""")
        self.conn.execute("""CREATE TABLE IF NOT EXISTS cpa_pauses (
            name TEXT PRIMARY KEY, binding TEXT NOT NULL
        )""")
        self.conn.execute("PRAGMA user_version = 2")

    def close(self) -> None:
        self.conn.close()
