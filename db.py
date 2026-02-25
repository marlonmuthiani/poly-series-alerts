#!/usr/bin/env python3
"""db.py — shared SQLite interface for streak-tracker"""

import sqlite3
import json
import time
import threading
from pathlib import Path
from typing import Optional

DB_PATH = Path(__file__).parent / "streak.db"

COINS = ["btc", "eth", "sol", "xrp"]
DEFAULT_MIN_STREAK = 8

# Thread-local connection pool — one connection per thread, reused
_local = threading.local()


def get_conn():
    """Get a thread-local SQLite connection (reused across calls)."""
    conn = getattr(_local, 'conn', None)
    if conn is None:
        conn = sqlite3.connect(str(DB_PATH), timeout=10)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.row_factory = sqlite3.Row
        _local.conn = conn
    return conn


def init_db():
    conn = get_conn()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            chat_id TEXT PRIMARY KEY,
            username TEXT,
            created_at REAL DEFAULT (unixepoch())
        );
        CREATE TABLE IF NOT EXISTS user_coin_prefs (
            chat_id TEXT NOT NULL,
            coin TEXT NOT NULL,
            enabled INTEGER DEFAULT 1,
            min_streak INTEGER DEFAULT 8,
            PRIMARY KEY (chat_id, coin),
            FOREIGN KEY (chat_id) REFERENCES users(chat_id)
        );
        CREATE TABLE IF NOT EXISTS asset_state (
            asset TEXT PRIMARY KEY,
            streak INTEGER DEFAULT 0,
            direction TEXT,
            last_epoch INTEGER DEFAULT 0,
            last_prices TEXT DEFAULT '[]',
            outcomes TEXT DEFAULT '[]',
            processed_epochs TEXT DEFAULT '[]'
        );
        CREATE TABLE IF NOT EXISTS alerts (
            asset TEXT PRIMARY KEY,
            streak INTEGER,
            direction TEXT,
            last_epoch INTEGER,
            timestamp REAL
        );
    """)
    # Add subscription columns (idempotent)
    for col_sql in [
        "ALTER TABLE users ADD COLUMN trial_expires REAL",
        "ALTER TABLE users ADD COLUMN is_subscriber INTEGER DEFAULT 0",
        "ALTER TABLE users ADD COLUMN sub_checked_at REAL",
        "ALTER TABLE users ADD COLUMN alerts_paused INTEGER DEFAULT 0",
    ]:
        try:
            conn.execute(col_sql)
        except sqlite3.OperationalError:
            pass  # column already exists

    # Give existing users a 24h trial from upgrade moment
    conn.execute("UPDATE users SET trial_expires = ? WHERE trial_expires IS NULL",
                 (time.time() + 86400,))
    conn.commit()


# ─── User functions ───────────────────────────────────────────────────────────

def register_user(chat_id: str, username: str = None) -> bool:
    conn = get_conn()
    try:
        cur = conn.execute("SELECT 1 FROM users WHERE chat_id=?", (chat_id,))
        is_new = cur.fetchone() is None
        if is_new:
            conn.execute("INSERT INTO users (chat_id, username, trial_expires) VALUES (?, ?, ?)",
                         (chat_id, username, time.time() + 86400))
            for coin in COINS:
                conn.execute(
                    "INSERT INTO user_coin_prefs (chat_id, coin, enabled, min_streak) VALUES (?, ?, 1, ?)",
                    (chat_id, coin, DEFAULT_MIN_STREAK))
            conn.commit()
        elif username:
            conn.execute("UPDATE users SET username=? WHERE chat_id=?", (username, chat_id))
            conn.commit()
        return is_new
    finally:
        pass  # conn reused (thread-local)


def get_user_prefs(chat_id: str) -> dict:
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT coin, enabled, min_streak FROM user_coin_prefs WHERE chat_id=?",
            (chat_id,)).fetchall()
        if not rows:
            return {"coins": {c: {"enabled": True, "min_streak": DEFAULT_MIN_STREAK} for c in COINS}}
        coins = {}
        for r in rows:
            coins[r["coin"]] = {"enabled": bool(r["enabled"]), "min_streak": r["min_streak"]}
        for c in COINS:
            if c not in coins:
                coins[c] = {"enabled": True, "min_streak": DEFAULT_MIN_STREAK}
        return {"coins": coins}
    finally:
        pass  # conn reused (thread-local)


def set_coin_pref(chat_id: str, coin: str, enabled: bool = None, min_streak: int = None):
    conn = get_conn()
    try:
        cur = conn.execute("SELECT enabled, min_streak FROM user_coin_prefs WHERE chat_id=? AND coin=?",
                           (chat_id, coin))
        row = cur.fetchone()
        if row is None:
            e = 1 if enabled is None else int(enabled)
            ms = min_streak if min_streak is not None else DEFAULT_MIN_STREAK
            conn.execute("INSERT INTO user_coin_prefs (chat_id, coin, enabled, min_streak) VALUES (?,?,?,?)",
                         (chat_id, coin, e, ms))
        else:
            if enabled is not None:
                conn.execute("UPDATE user_coin_prefs SET enabled=? WHERE chat_id=? AND coin=?",
                             (int(enabled), chat_id, coin))
            if min_streak is not None:
                conn.execute("UPDATE user_coin_prefs SET min_streak=? WHERE chat_id=? AND coin=?",
                             (min_streak, chat_id, coin))
        conn.commit()
    finally:
        pass  # conn reused (thread-local)


def toggle_alerts_paused(chat_id: str) -> bool:
    """Toggle alerts_paused. Returns new state (True = paused)."""
    conn = get_conn()
    try:
        row = conn.execute("SELECT alerts_paused FROM users WHERE chat_id=?", (chat_id,)).fetchone()
        new_val = 0 if (row and row["alerts_paused"]) else 1
        conn.execute("UPDATE users SET alerts_paused=? WHERE chat_id=?", (new_val, chat_id))
        conn.commit()
        return bool(new_val)
    finally:
        pass  # conn reused (thread-local)


def is_alerts_paused(chat_id: str) -> bool:
    conn = get_conn()
    try:
        row = conn.execute("SELECT alerts_paused FROM users WHERE chat_id=?", (chat_id,)).fetchone()
        return bool(row and row["alerts_paused"])
    finally:
        pass  # conn reused (thread-local)


def is_subscriber_cached(chat_id: str) -> bool:
    """Check if user is subscriber (from DB cache)."""
    conn = get_conn()
    try:
        row = conn.execute("SELECT is_subscriber FROM users WHERE chat_id=?", (chat_id,)).fetchone()
        return bool(row and row["is_subscriber"])
    finally:
        pass  # conn reused (thread-local)


def get_stale_subscribers(max_age_seconds: int = 86400) -> list:
    """Get subscribers not checked in max_age_seconds."""
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT chat_id FROM users WHERE is_subscriber=1 AND "
            "(sub_checked_at IS NULL OR sub_checked_at < unixepoch() - ?)",
            (max_age_seconds,)
        ).fetchall()
        return [r["chat_id"] for r in rows]
    finally:
        pass  # conn reused (thread-local)


def set_all_streaks(chat_id: str, min_streak: int):
    conn = get_conn()
    try:
        conn.execute("UPDATE user_coin_prefs SET min_streak=? WHERE chat_id=?", (min_streak, chat_id))
        conn.commit()
    finally:
        pass  # conn reused (thread-local)


def set_subscriber(chat_id: str, is_sub: bool):
    """Update subscriber status and check timestamp."""
    conn = get_conn()
    try:
        conn.execute("UPDATE users SET is_subscriber=?, sub_checked_at=? WHERE chat_id=?",
                     (int(is_sub), time.time(), chat_id))
        conn.commit()
    finally:
        pass  # conn reused (thread-local)


def is_user_active(chat_id: str) -> bool:
    """Returns True if user is subscriber OR trial hasn't expired."""
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT is_subscriber, trial_expires FROM users WHERE chat_id=?",
            (chat_id,)).fetchone()
        if not row:
            return True  # unknown user = allow (will be registered)
        if row["is_subscriber"]:
            return True
        return (row["trial_expires"] or 0) > time.time()
    finally:
        pass  # conn reused (thread-local)


def get_trial_remaining(chat_id: str) -> float:
    """Returns seconds remaining in trial. 0 if expired."""
    conn = get_conn()
    try:
        row = conn.execute("SELECT trial_expires FROM users WHERE chat_id=?",
                           (chat_id,)).fetchone()
        if not row or not row["trial_expires"]:
            return 0
        remaining = row["trial_expires"] - time.time()
        return max(remaining, 0)
    finally:
        pass  # conn reused (thread-local)


def get_all_users_for_alert(asset: str, streak: int) -> list:
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT u.chat_id FROM users u JOIN user_coin_prefs p ON u.chat_id=p.chat_id "
            "WHERE p.coin=? AND p.enabled=1 AND p.min_streak<=? "
            "AND u.alerts_paused = 0 "
            "AND (u.trial_expires > unixepoch() OR u.is_subscriber = 1)",
            (asset, streak)).fetchall()
        return [r["chat_id"] for r in rows]
    finally:
        pass  # conn reused (thread-local)


# ─── State functions ──────────────────────────────────────────────────────────

def load_state() -> dict:
    default = {"streak": 0, "direction": None, "last_epoch": 0, "last_prices": [],
               "outcomes": [], "processed_epochs": []}
    conn = get_conn()
    try:
        rows = conn.execute("SELECT * FROM asset_state").fetchall()
        state = {}
        for r in rows:
            state[r["asset"]] = {
                "streak": r["streak"],
                "direction": r["direction"],
                "last_epoch": r["last_epoch"],
                "last_prices": json.loads(r["last_prices"]),
                "outcomes": json.loads(r["outcomes"]),
                "processed_epochs": json.loads(r["processed_epochs"]),
            }
        for c in COINS:
            if c not in state:
                state[c] = default.copy()
        return state
    finally:
        pass  # conn reused (thread-local)


def save_asset_state(asset: str, state: dict):
    conn = get_conn()
    try:
        conn.execute(
            "INSERT OR REPLACE INTO asset_state (asset, streak, direction, last_epoch, last_prices, outcomes, processed_epochs) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (asset, state["streak"], state["direction"], state["last_epoch"],
             json.dumps(state.get("last_prices", [])),
             json.dumps(state.get("outcomes", [])),
             json.dumps(state.get("processed_epochs", []))))
        conn.commit()
    finally:
        pass  # conn reused (thread-local)


def save_full_state(state: dict):
    conn = get_conn()
    try:
        for asset, s in state.items():
            conn.execute(
                "INSERT OR REPLACE INTO asset_state (asset, streak, direction, last_epoch, last_prices, outcomes, processed_epochs) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (asset, s["streak"], s["direction"], s["last_epoch"],
                 json.dumps(s.get("last_prices", [])),
                 json.dumps(s.get("outcomes", [])),
                 json.dumps(s.get("processed_epochs", []))))
        conn.commit()
    finally:
        pass  # conn reused (thread-local)


def get_processed_epochs(asset: str) -> set:
    conn = get_conn()
    try:
        row = conn.execute("SELECT processed_epochs FROM asset_state WHERE asset=?", (asset,)).fetchone()
        if row:
            return set(json.loads(row["processed_epochs"]))
        return set()
    finally:
        pass  # conn reused (thread-local)


# ─── Alert dedup ──────────────────────────────────────────────────────────────

def load_alerts() -> dict:
    conn = get_conn()
    try:
        rows = conn.execute("SELECT * FROM alerts").fetchall()
        return {r["asset"]: {"streak": r["streak"], "direction": r["direction"],
                             "last_epoch": r["last_epoch"], "timestamp": r["timestamp"]} for r in rows}
    finally:
        pass  # conn reused (thread-local)


def save_alert(asset: str, data: dict):
    conn = get_conn()
    try:
        conn.execute(
            "INSERT OR REPLACE INTO alerts (asset, streak, direction, last_epoch, timestamp) VALUES (?,?,?,?,?)",
            (asset, data["streak"], data["direction"], data["last_epoch"], data["timestamp"]))
        conn.commit()
    finally:
        pass  # conn reused (thread-local)


# ─── Migration ────────────────────────────────────────────────────────────────

def migrate_from_json():
    base = Path(__file__).parent
    conn = get_conn()
    try:
        # Users
        uf = base / "users.json"
        if uf.exists():
            data = json.loads(uf.read_text())
            if isinstance(data, list):
                data = {str(uid): {} for uid in data}
            for chat_id, prefs in data.items():
                conn.execute("INSERT OR IGNORE INTO users (chat_id) VALUES (?)", (chat_id,))
                coins = prefs.get("coins", {})
                for coin in COINS:
                    cp = coins.get(coin, {"enabled": True, "min_streak": DEFAULT_MIN_STREAK})
                    conn.execute(
                        "INSERT OR REPLACE INTO user_coin_prefs (chat_id, coin, enabled, min_streak) VALUES (?,?,?,?)",
                        (chat_id, coin, int(cp.get("enabled", True)), cp.get("min_streak", DEFAULT_MIN_STREAK)))
            uf.replace(uf.with_suffix(".json.bak"))

        # State
        sf = base / "state.json"
        if sf.exists():
            data = json.loads(sf.read_text())
            for asset, s in data.items():
                conn.execute(
                    "INSERT OR REPLACE INTO asset_state (asset, streak, direction, last_epoch, last_prices, outcomes, processed_epochs) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (asset, s.get("streak", 0), s.get("direction"),
                     s.get("last_epoch", 0),
                     json.dumps(s.get("last_prices", [])),
                     json.dumps(s.get("outcomes", [])),
                     json.dumps(s.get("processed_epochs", []))))
            sf.replace(sf.with_suffix(".json.bak"))

        # Alerts
        af = base / "alerts.json"
        if af.exists():
            data = json.loads(af.read_text())
            for asset, a in data.items():
                conn.execute(
                    "INSERT OR REPLACE INTO alerts (asset, streak, direction, last_epoch, timestamp) VALUES (?,?,?,?,?)",
                    (asset, a.get("streak", 0), a.get("direction"),
                     a.get("last_epoch", 0), a.get("timestamp", 0)))
            af.replace(af.with_suffix(".json.bak"))

        conn.commit()
    finally:
        pass  # conn reused (thread-local)


def startup():
    """Call on startup from both bot.py and server.py."""
    init_db()
    uf = Path(__file__).parent / "users.json"
    sf = Path(__file__).parent / "state.json"
    af = Path(__file__).parent / "alerts.json"
    if uf.exists() or sf.exists() or af.exists():
        migrate_from_json()
