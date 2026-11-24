"""Shared chat history and delivery queue used by the web app and Telegram bot."""

from contextlib import closing
from datetime import datetime, timezone
from os import getenv
from pathlib import Path
import sqlite3
from time import time

from dotenv import dotenv_values

from dice_parser import DESCRIPTION_SEPARATOR, MAX_EXPRESSION_LENGTH, FormulaError, find_formulas
from service import MAX_DESCRIPTION_LENGTH, evaluate_formula


ENV_PATH = Path(__file__).with_name(".env")
FEED_SIZE = 100
MAX_PENDING = 100
MAX_NAME_LENGTH = 60
MAX_PLAIN_TEXT = 3800
MAX_CHAT_INPUT = MAX_EXPRESSION_LENGTH + len(DESCRIPTION_SEPARATOR) + MAX_DESCRIPTION_LENGTH
FORMULA_PREVIEW = 160
RESULT_PREVIEW = 1600
MAX_SQLITE_ID = 2**63 - 1


class QueueFull(Exception):
    """The Telegram worker has too many unsent messages."""


class TargetChanged(Exception):
    """The composer was open for a different chat."""


def parse_target(raw_id):
    raw_id = (raw_id or "").strip()
    if not raw_id:
        return None
    if not raw_id.removeprefix("-").isascii() or not raw_id.removeprefix("-").isdecimal() or len(raw_id) > 20:
        raise ValueError("Укажите числовой ID чата Telegram.")
    target = int(raw_id)
    if target == 0 or not -MAX_SQLITE_ID <= target <= MAX_SQLITE_ID:
        raise ValueError("Укажите числовой ID чата Telegram.")
    return target


def chat_settings():
    local = dotenv_values(ENV_PATH)
    raw_id = getenv("TELEGRAM_CHAT_ID", local.get("TELEGRAM_CHAT_ID") or "")
    try:
        chat_id = parse_target(raw_id)
    except ValueError:
        raise ValueError("TELEGRAM_CHAT_ID должен содержать числовой ID чата Telegram.") from None
    path = getenv("WEB_DB_PATH", local.get("WEB_DB_PATH") or "data/rolls.sqlite3")
    return chat_id, path


def detect_roll(text):
    """Only an entire single-line formula triggers a roll in the shared chat."""
    if len(text.splitlines()) != 1:
        return None
    formulas = find_formulas(text)
    expression = text.partition(DESCRIPTION_SEPARATOR)[0].strip()
    if len(formulas) == 1 and formulas[0].text == expression:
        return evaluate_formula(formulas[0])
    return None


def connect(path):
    db = sqlite3.connect(path, timeout=10)
    db.row_factory = sqlite3.Row
    return db


class ChatStore:
    def __init__(self, path, default_target=None):
        self.path = Path(path)
        self.default_target = default_target
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(connect(self.path)) as db, db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("BEGIN IMMEDIATE")
            db.execute("""
                CREATE TABLE IF NOT EXISTS chat_entries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source TEXT NOT NULL,
                    character_id TEXT NOT NULL,
                    name TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    text TEXT NOT NULL,
                    total TEXT,
                    rendered TEXT,
                    group_id INTEGER,
                    incoming_message_id INTEGER,
                    incoming_thread_id INTEGER,
                    delivery TEXT,
                    retry_at REAL NOT NULL DEFAULT 0
                )
            """)
            db.execute("""
                CREATE TABLE IF NOT EXISTS telegram_receipts (
                    group_id INTEGER NOT NULL,
                    message_id INTEGER NOT NULL,
                    PRIMARY KEY (group_id, message_id)
                ) WITHOUT ROWID
            """)
            db.execute("""
                CREATE TABLE IF NOT EXISTS chat_settings (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    target_id INTEGER
                )
            """)
            db.execute("CREATE INDEX IF NOT EXISTS chat_feed_scope ON chat_entries (group_id, id)")
            db.execute("""
                CREATE INDEX IF NOT EXISTS pending_chat_entries ON chat_entries (group_id, id)
                WHERE delivery IN ('queued', 'sending')
            """)
            legacy = db.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'rolls'").fetchone()
            if legacy:
                db.execute("""
                    INSERT INTO chat_entries (source, character_id, name, created_at, text, total, rendered)
                    SELECT 'web', character_id, character_name, created_at, formula, total, rendered
                    FROM rolls ORDER BY id
                """)
                db.execute("DROP TABLE rolls")

    def _active_target(self, db):
        row = db.execute("SELECT target_id FROM chat_settings WHERE id = 1").fetchone()
        return row["target_id"] if row is not None else self.default_target

    def active_target(self):
        with closing(connect(self.path)) as db:
            return self._active_target(db)

    def set_target(self, target_id):
        with closing(connect(self.path)) as db, db:
            db.execute("""
                INSERT INTO chat_settings (id, target_id) VALUES (1, ?)
                ON CONFLICT (id) DO UPDATE SET target_id = excluded.target_id
            """, (target_id,))

    def _has_room(self, db, group_id):
        count = db.execute(
            "SELECT COUNT(*) FROM chat_entries WHERE group_id = ? AND delivery IN ('queued', 'sending')", (group_id,)
        ).fetchone()[0]
        return count < MAX_PENDING

    def _insert(self, db, source, character_id, name, text, roll, group_id, message_id, thread_id, delivery):
        result = db.execute("""
            INSERT INTO chat_entries
                (source, character_id, name, created_at, text, total, rendered,
                 group_id, incoming_message_id, incoming_thread_id, delivery)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            source, character_id, name, datetime.now(timezone.utc).isoformat(timespec="seconds"), text,
            roll.total if roll else None, "".join(roll.parts) if roll else None, group_id, message_id, thread_id, delivery,
        ))
        db.execute("""
            DELETE FROM chat_entries
            WHERE group_id IS ? AND delivery IS NOT 'queued' AND delivery IS NOT 'sending'
                AND id < (SELECT id FROM chat_entries WHERE group_id IS ? ORDER BY id DESC LIMIT 1 OFFSET ?)
        """, (group_id, group_id, FEED_SIZE - 1))
        return result.lastrowid

    def add_web(self, character_id, name, text, roll, group_id):
        with closing(connect(self.path)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            if self._active_target(db) != group_id:
                raise TargetChanged
            if group_id is not None and not self._has_room(db, group_id):
                raise QueueFull
            delivery = "queued" if group_id is not None else None
            return self._insert(db, "web", character_id, name, text, roll, group_id, None, None, delivery)

    def add_telegram(self, group_id, message_id, author_id, name, text, thread_id=None):
        with closing(connect(self.path)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            if self._active_target(db) != group_id:
                return None
            inserted = db.execute("INSERT OR IGNORE INTO telegram_receipts VALUES (?, ?)", (group_id, message_id))
            if not inserted.rowcount:
                return None
            try:
                roll = detect_roll(text)
            except FormulaError:
                # Telegram has already accepted the text, so mirror it as a regular message.
                roll = None
            delivery = ("queued" if self._has_room(db, group_id) else "failed") if roll else None
            return self._insert(db, "telegram", author_id, name, text, roll, group_id, message_id, thread_id, delivery)

    def feed(self):
        with closing(connect(self.path)) as db:
            db.execute("BEGIN")
            target = self._active_target(db)
            rows = db.execute("""
                SELECT id, source, character_id, name, created_at, total, delivery,
                    CASE WHEN total IS NULL THEN text ELSE substr(text, 1, ?) END AS text,
                    (total IS NOT NULL AND length(text) > ?) AS text_truncated,
                    CASE WHEN length(rendered) <= ? THEN rendered END AS result_html,
                    CASE WHEN total IS NOT NULL THEN (length(text) > ? OR length(rendered) > ?) ELSE 0 END AS has_download
                FROM chat_entries WHERE group_id IS ? ORDER BY id DESC LIMIT ?
            """, (FORMULA_PREVIEW, FORMULA_PREVIEW, RESULT_PREVIEW, FORMULA_PREVIEW, RESULT_PREVIEW, target, FEED_SIZE)).fetchall()
        return target, [dict(row) for row in reversed(rows)]

    def full_message(self, entry_id):
        if not 0 < entry_id <= MAX_SQLITE_ID:
            return None
        with closing(connect(self.path)) as db:
            db.execute("BEGIN")
            return db.execute(
                "SELECT text, rendered FROM chat_entries WHERE id = ? AND group_id IS ?",
                (entry_id, self._active_target(db)),
            ).fetchone()

    def recover_delivery(self):
        # A send interrupted during the HTTP call might already exist in Telegram.
        with closing(connect(self.path)) as db, db:
            db.execute("""
                UPDATE chat_entries SET delivery = 'uncertain' WHERE delivery = 'sending'
            """)

    def claim_delivery(self, now=None):
        with closing(connect(self.path)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            group_id = self._active_target(db)
            if group_id is None:
                return None
            row = db.execute("""
                SELECT * FROM chat_entries
                WHERE group_id = ? AND delivery IN ('queued', 'sending') ORDER BY id LIMIT 1
            """, (group_id,)).fetchone()
            if row is None or row["delivery"] != "queued" or row["retry_at"] > (time() if now is None else now):
                return None
            db.execute("UPDATE chat_entries SET delivery = 'sending' WHERE id = ?", (row["id"],))
            return row

    def finish_delivery(self, entry_id, status, *, retry_at=0, telegram_id=None):
        with closing(connect(self.path)) as db, db:
            updated = db.execute("""
                UPDATE chat_entries SET delivery = ?, retry_at = ?
                WHERE id = ? AND delivery = 'sending'
            """, (status, retry_at, entry_id))
            if updated.rowcount and telegram_id is not None:
                db.execute("""
                    INSERT OR IGNORE INTO telegram_receipts (group_id, message_id)
                    SELECT group_id, ? FROM chat_entries WHERE id = ?
                """, (telegram_id, entry_id))
