"""Persistent, scoped pagination leases using only the Python standard library.

The caller supplies an already lossless page plan; this module never segments,
rewrites, sends, or infers delivery.  ``prepare`` reserves page zero.  Only an
explicit, valid ``ack`` advances the next unconfirmed index.  Failed sends must
``release`` their lease and retry that same index.  ``renew`` is an optional
heartbeat for a send that is still running.

All indices are zero based.  A page has content/display_text/segments/char_limit,
continuation/index/source_start/source_end, and optionally a JSON score_plan.
Segments concatenate to display_text; display_text starts with the exact content
and may append a continuation hint on nonfinal pages.  Each page has 1..3
nonempty segments, each within char_limit (an integer in 1..60).  Source slices
must cover full_text exactly, in order.  No whitespace or punctuation is removed.

metadata() returns {schema_version, count, entries}.  Entries contain only scope,
sender_id/user, index, pages, remaining, active, busy, expiry, lease_expiry, and
generation: never reply text, page data, or lease tokens.  peek_metadata() returns
one such entry or None.  Completed entries remain until their original TTL;
metadata and all lease operations reap expired entries and expired lease tokens.
A time boundary is exclusive: at expiry, ack/release/renew fail and reserve may
issue a new token only if the response TTL has not expired.  Reaped tokens and
entries cannot revive if the injected clock subsequently moves backwards.

Each operation owns a short-lived SQLite connection and an IMMEDIATE transaction
so both threads and independent instances/processes share the same reservation
lock.  WAL, a 5-second busy timeout, and foreign-key cascades are enabled.  Use a
separate deployment-data path, not a chat/memory database; incompatible, damaged,
and newer schemas fail closed without migration.  Methods are synchronous; the
async integration should call them via asyncio.to_thread, including construction.
"""
from __future__ import annotations

from collections.abc import Callable
from contextlib import contextmanager
import json
import math
from numbers import Real
import os
from pathlib import Path
import re
import secrets
import sqlite3
from typing import Any

__all__ = ["PageStore", "PageStoreError"]

_SCHEMA_VERSION = 1
_APPLICATION_ID = 0x57504731  # WPG1: a dedicated whale-pagination database.
_BUSY_TIMEOUT_MS = 5000
_REQUIRED_PAGE_KEYS = frozenset({
    "content", "display_text", "segments", "char_limit", "continuation", "index",
    "source_start", "source_end",
})
_ALLOWED_PAGE_KEYS = _REQUIRED_PAGE_KEYS | {"score_plan"}
_SCHEMA_SQL = {
    "page_responses": """CREATE TABLE page_responses (
        scope TEXT NOT NULL,
        sender_id TEXT NOT NULL,
        generation TEXT NOT NULL UNIQUE,
        source_id TEXT NOT NULL,
        full_text TEXT NOT NULL,
        next_index INTEGER NOT NULL CHECK(next_index >= 0),
        page_count INTEGER NOT NULL CHECK(page_count > 0 AND next_index <= page_count),
        created_at REAL NOT NULL,
        expires_at REAL NOT NULL CHECK(expires_at > created_at),
        lease_token TEXT,
        lease_index INTEGER,
        lease_expires_at REAL,
        PRIMARY KEY(scope, sender_id),
        CHECK((lease_token IS NULL AND lease_index IS NULL AND lease_expires_at IS NULL)
           OR (lease_token IS NOT NULL AND lease_index IS NOT NULL
               AND lease_expires_at IS NOT NULL AND lease_index = next_index
               AND next_index < page_count AND lease_expires_at <= expires_at))
    )""",
    "page_items": """CREATE TABLE page_items (
        generation TEXT NOT NULL,
        page_index INTEGER NOT NULL CHECK(page_index >= 0),
        page_json TEXT NOT NULL,
        PRIMARY KEY(generation, page_index),
        FOREIGN KEY(generation) REFERENCES page_responses(generation) ON DELETE CASCADE
    )""",
    "page_responses_expiry": "CREATE INDEX page_responses_expiry ON page_responses(expires_at)",
}
_META_COLUMNS = (
    "scope,sender_id,generation,next_index,page_count,created_at,expires_at,"
    "lease_token,lease_index,lease_expires_at"
)


class PageStoreError(RuntimeError):
    """An incompatible schema or invalid persisted page state; no repair is attempted."""


def _number(value: Any, name: str, *, positive: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, Real):
        raise TypeError(name + " must be a real number, not bool")
    try:
        result = float(value)
    except (OverflowError, ValueError):
        raise ValueError(name + " must be finite") from None
    if not math.isfinite(result):
        raise ValueError(name + " must be finite")
    if positive and result <= 0:
        raise ValueError(name + " must be positive")
    return result


def _text(value: Any, name: str, *, nonempty: bool = True) -> str:
    if not isinstance(value, str):
        raise TypeError(name + " must be str")
    if nonempty and not value:
        raise ValueError(name + " must not be empty")
    try:
        value.encode("utf-8")
    except UnicodeError:
        raise ValueError(name + " must contain valid Unicode") from None
    return value


def _identifier(value: Any, name: str) -> str:
    result = _text(value, name)
    if not result.strip() or "\x00" in result:
        raise ValueError(name + " must be a nonblank identifier without NUL")
    # Never trim, coerce, or truncate identifiers: those operations merge owners.
    return result


def _integer(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(name + " must be int, not bool")
    return value


def _json_value(value: Any, depth: int = 0) -> None:
    if depth > 64:
        raise ValueError("score_plan nesting is too deep")
    if value is None or type(value) in (str, bool, int):
        if isinstance(value, str):
            _text(value, "score_plan string", nonempty=False)
        return
    if type(value) is float:
        _number(value, "score_plan number")
        return
    if type(value) is list:
        for child in value:
            _json_value(child, depth + 1)
        return
    if type(value) is dict:
        for key, child in value.items():
            if type(key) is not str:
                raise TypeError("score_plan keys must be str")
            _text(key, "score_plan key", nonempty=False)
            _json_value(child, depth + 1)
        return
    raise TypeError("score_plan must contain only JSON values")


def _json_dump(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=True, allow_nan=False, separators=(",", ":"))
    except (TypeError, ValueError, OverflowError, RecursionError):
        raise ValueError("Page data must be finite, serializable JSON") from None


def _unique_object(pairs: list[tuple[str, Any]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate stored JSON key")
        result[key] = value
    return result


def _bad_constant(value: str) -> None:
    raise ValueError("Nonfinite stored JSON number")


def _page(value: Any, index: int, total: int, full_text: str) -> dict:
    if type(value) is not dict:
        raise TypeError("Each page must be dict")
    if not _REQUIRED_PAGE_KEYS <= value.keys() or not value.keys() <= _ALLOWED_PAGE_KEYS:
        raise ValueError("Page fields do not match the page schema")
    actual_index = _integer(value["index"], "page index")
    if actual_index != index:
        raise ValueError("Page indices must be consecutive and zero based")
    limit = _integer(value["char_limit"], "char_limit")
    if not 1 <= limit <= 60:
        raise ValueError("char_limit must be in 1..60")
    continuation = value["continuation"]
    if type(continuation) is not bool:
        raise TypeError("continuation must be bool")
    if continuation != (index < total - 1):
        raise ValueError("Only nonfinal pages may have continuation=True")
    content = _text(value["content"], "page content")
    display = _text(value["display_text"], "display_text")
    if not display.startswith(content) or (not continuation and display != content):
        raise ValueError("display_text must preserve content with only a nonfinal suffix")
    segments = value["segments"]
    if type(segments) is not list:
        raise TypeError("segments must be list")
    if not 1 <= len(segments) <= 3:
        raise ValueError("Each page must contain 1..3 segments")
    copied_segments = []
    for segment in segments:
        segment = _text(segment, "segment")
        if len(segment) > limit:
            raise ValueError("A segment exceeds char_limit")
        copied_segments.append(segment)
    if "".join(copied_segments) != display:
        raise ValueError("Segments must concatenate exactly to display_text")
    start = _integer(value["source_start"], "source_start")
    end = _integer(value["source_end"], "source_end")
    if not 0 <= start < end <= len(full_text) or end - start != len(content):
        raise ValueError("Page source offsets must describe its exact content")
    if full_text[start:end] != content:
        raise ValueError("Page content must equal the original source slice")
    result = {
        "content": content, "display_text": display, "segments": copied_segments,
        "char_limit": limit, "continuation": continuation, "index": actual_index,
        "source_start": start, "source_end": end,
    }
    if "score_plan" in value:
        _json_value(value["score_plan"])
        result["score_plan"] = json.loads(_json_dump(value["score_plan"]))
    return result


def _pages(full_text: Any, pages: Any) -> list[str]:
    full_text = _text(full_text, "full_text")
    if type(pages) is not list:
        raise TypeError("pages must be list")
    if not pages:
        raise ValueError("pages must not be empty")
    encoded, contents, cursor = [], [], 0
    for index, raw in enumerate(pages):
        page = _page(raw, index, len(pages), full_text)
        if page["source_start"] != cursor:
            raise ValueError("Page source slices must be contiguous and ordered")
        cursor = page["source_end"]
        contents.append(page["content"])
        encoded.append(_json_dump(page))
    if cursor != len(full_text) or "".join(contents) != full_text:
        raise ValueError("Page content must concatenate exactly to full_text")
    return encoded


def _hex_identity(value: Any, length: int) -> bool:
    return (type(value) is str and len(value) == length
            and all(char in "0123456789abcdef" for char in value))


def _lease_identity(lease: Any) -> tuple[str, str, str, str, int] | None:
    if type(lease) is not dict:
        return None
    try:
        scope = _identifier(lease.get("scope"), "scope")
        sender = _identifier(lease.get("sender_id"), "sender_id")
        index = _integer(lease.get("index"), "lease index")
    except (TypeError, ValueError):
        return None
    generation, token = lease.get("generation"), lease.get("token")
    if index < 0 or not _hex_identity(generation, 32) or not _hex_identity(token, 64):
        return None
    return scope, sender, generation, token, index


def _sql_normalized(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip().rstrip(";").casefold()


class PageStore:
    def __init__(
        self, path: str | os.PathLike[str], *, now: Callable[[], Real],
        ttl_seconds: Real = 600, lease_seconds: Real = 90,
    ) -> None:
        if not callable(now):
            raise TypeError("now must be callable")
        self._now = now
        self.ttl_seconds = _number(ttl_seconds, "ttl_seconds", positive=True)
        self.lease_seconds = _number(lease_seconds, "lease_seconds", positive=True)
        timestamp = self._timestamp()
        if not isinstance(path, (str, os.PathLike)):
            raise TypeError("path must be str or PathLike[str]")
        raw_path = os.fspath(path)
        if not isinstance(raw_path, str):
            raise TypeError("path must resolve to str")
        if not raw_path.strip() or raw_path == ":memory:" or raw_path.startswith("file:"):
            raise ValueError("path must name a persistent SQLite file, not a URI")
        self.path = Path(raw_path).expanduser().resolve()
        self._closed = False
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize(timestamp)

    def _timestamp(self) -> float:
        return _number(self._now(), "now()")

    @staticmethod
    def _deadline(timestamp: float, seconds: Real, cap: float | None = None) -> float:
        result = timestamp + _number(seconds, "duration", positive=True)
        if not math.isfinite(result) or result <= timestamp:
            raise ValueError("The expiry must be finite and later than now()")
        if cap is not None:
            result = min(result, cap)
        if result <= timestamp:
            raise ValueError("The expiry must be later than now()")
        return result

    @contextmanager
    def _connection(self):
        if self._closed:
            raise RuntimeError("PageStore is closed")
        connection = sqlite3.connect(str(self.path), timeout=5, isolation_level=None)
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA busy_timeout=5000")
            connection.execute("PRAGMA foreign_keys=ON")
            yield connection
        finally:
            connection.close()

    @contextmanager
    def _transaction(self):
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                self._verify_schema(connection)
                yield connection
                connection.commit()
            except BaseException:
                connection.rollback()
                raise

    @staticmethod
    def _verify_schema(connection: sqlite3.Connection) -> None:
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        application = connection.execute("PRAGMA application_id").fetchone()[0]
        if version > _SCHEMA_VERSION:
            raise PageStoreError("Page database was created by a newer PageStore")
        if version != _SCHEMA_VERSION or application != _APPLICATION_ID:
            raise PageStoreError("Not a compatible PageStore database")
        for name, expected in _SCHEMA_SQL.items():
            row = connection.execute(
                "SELECT type,sql FROM sqlite_master WHERE name=?", (name,),
            ).fetchone()
            kind = "index" if name == "page_responses_expiry" else "table"
            if (row is None or row["type"] != kind or not isinstance(row["sql"], str)
                    or _sql_normalized(row["sql"]) != _sql_normalized(expected)):
                raise PageStoreError("Page database schema is damaged or incompatible")

    def _initialize(self, timestamp: float) -> None:
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                version = connection.execute("PRAGMA user_version").fetchone()[0]
                application = connection.execute("PRAGMA application_id").fetchone()[0]
                objects = connection.execute(
                    "SELECT name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'",
                ).fetchall()
                if version == 0 and application == 0 and not objects:
                    for statement in _SCHEMA_SQL.values():
                        connection.execute(statement)
                    connection.execute("PRAGMA application_id=1464878897")
                    connection.execute("PRAGMA user_version=1")
                self._verify_schema(connection)
                self._check_contents(connection)
                self._reap(connection, timestamp)
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
            mode = connection.execute("PRAGMA journal_mode=WAL").fetchone()[0]
            if mode.lower() != "wal":
                raise PageStoreError("PageStore requires a persistent WAL database")

    @staticmethod
    def _row_valid(row: sqlite3.Row) -> None:
        try:
            _identifier(row["scope"], "stored scope")
            _identifier(row["sender_id"], "stored sender_id")
            if not _hex_identity(row["generation"], 32):
                raise ValueError("Invalid generation")
            index = _integer(row["next_index"], "stored next_index")
            count = _integer(row["page_count"], "stored page_count")
            if not 0 <= index <= count or count <= 0:
                raise ValueError("Invalid progress")
            created = _number(row["created_at"], "stored created_at")
            expiry = _number(row["expires_at"], "stored expires_at")
            if expiry <= created:
                raise ValueError("Invalid TTL")
            lease_values = (row["lease_token"], row["lease_index"], row["lease_expires_at"])
            if not all(value is None for value in lease_values):
                if (any(value is None for value in lease_values)
                        or not _hex_identity(row["lease_token"], 64)
                        or _integer(row["lease_index"], "stored lease_index") != index
                        or index >= count
                        or _number(row["lease_expires_at"], "stored lease expiry") > expiry):
                    raise ValueError("Invalid reservation")
        except (TypeError, ValueError, KeyError, IndexError):
            raise PageStoreError("Page database contains invalid stored metadata") from None

    @staticmethod
    def _decoded_page(encoded: Any, index: int, count: int, full_text: str) -> dict:
        try:
            if type(encoded) is not str:
                raise TypeError("Invalid stored JSON type")
            value = json.loads(encoded, object_pairs_hook=_unique_object,
                               parse_constant=_bad_constant)
            return _page(value, index, count, full_text)
        except (TypeError, ValueError, OverflowError, RecursionError):
            raise PageStoreError("Page database contains invalid stored page data") from None

    def _check_contents(self, connection: sqlite3.Connection) -> None:
        for row in connection.execute("SELECT * FROM page_responses").fetchall():
            self._row_valid(row)
            try:
                full_text = _text(row["full_text"], "stored full_text")
                _identifier(row["source_id"], "stored source_id")
            except (TypeError, ValueError):
                raise PageStoreError("Page database contains invalid stored response data") from None
            items = connection.execute(
                "SELECT page_index,page_json FROM page_items WHERE generation=? ORDER BY page_index",
                (row["generation"],),
            ).fetchall()
            if len(items) != row["page_count"]:
                raise PageStoreError("Page database has missing or extra pages")
            cursor, contents = 0, []
            for index, item in enumerate(items):
                if item["page_index"] != index:
                    raise PageStoreError("Page database has unordered pages")
                page = self._decoded_page(item["page_json"], index, len(items), full_text)
                if page["source_start"] != cursor:
                    raise PageStoreError("Page database has noncontiguous source slices")
                cursor = page["source_end"]
                contents.append(page["content"])
            if cursor != len(full_text) or "".join(contents) != full_text:
                raise PageStoreError("Page database does not preserve the complete response")
        if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise PageStoreError("Page database has orphaned pages")

    @staticmethod
    def _reap(connection: sqlite3.Connection, timestamp: float) -> None:
        connection.execute("DELETE FROM page_responses WHERE expires_at<=?", (timestamp,))
        # Clearing expired tokens is durable fencing, even before a retry occurs.
        connection.execute(
            """UPDATE page_responses SET lease_token=NULL,lease_index=NULL,lease_expires_at=NULL
               WHERE lease_expires_at<=?""", (timestamp,),
        )

    def _current_page(self, connection: sqlite3.Connection, row: sqlite3.Row) -> dict:
        try:
            full_text = _text(row["full_text"], "stored full_text")
            _identifier(row["source_id"], "stored source_id")
        except (TypeError, ValueError):
            raise PageStoreError("Page database contains invalid stored response data") from None
        count, index = row["page_count"], row["next_index"]
        summary = connection.execute(
            "SELECT COUNT(*),MIN(page_index),MAX(page_index) FROM page_items WHERE generation=?",
            (row["generation"],),
        ).fetchone()
        if tuple(summary) != (count, 0, count - 1):
            raise PageStoreError("Page database has missing or unordered pages")
        neighbors = connection.execute(
            """SELECT page_index,page_json FROM page_items
               WHERE generation=? AND page_index BETWEEN ? AND ? ORDER BY page_index""",
            (row["generation"], max(0, index - 1), min(count - 1, index + 1)),
        ).fetchall()
        decoded = {
            item["page_index"]: self._decoded_page(item["page_json"], item["page_index"], count, full_text)
            for item in neighbors
        }
        page = decoded[index]
        if ((index == 0 and page["source_start"] != 0)
                or (index == count - 1 and page["source_end"] != len(full_text))
                or (index > 0 and decoded[index - 1]["source_end"] != page["source_start"])
                or (index < count - 1 and page["source_end"] != decoded[index + 1]["source_start"])):
            raise PageStoreError("Page database has noncontiguous source slices")
        return page

    @staticmethod
    def _lease(row: sqlite3.Row, page: dict, token: str) -> dict:
        return {
            "token": token, "generation": row["generation"], "scope": row["scope"],
            "sender_id": row["sender_id"], "index": row["next_index"], "page": page,
        }

    def prepare(self, scope: str, sender_id: str, full_text: str,
                pages: list[dict], source_id: str) -> dict:
        scope = _identifier(scope, "scope")
        sender_id = _identifier(sender_id, "sender_id")
        source_id = _identifier(source_id, "source_id")
        encoded = _pages(full_text, pages)
        generation, token = secrets.token_hex(16), secrets.token_hex(32)
        with self._transaction() as connection:
            timestamp = self._timestamp()
            expiry = self._deadline(timestamp, self.ttl_seconds)
            lease_expiry = self._deadline(timestamp, self.lease_seconds, expiry)
            self._reap(connection, timestamp)
            connection.execute(
                "DELETE FROM page_responses WHERE scope=? AND sender_id=?", (scope, sender_id),
            )
            connection.execute(
                """INSERT INTO page_responses
                   (scope,sender_id,generation,source_id,full_text,next_index,page_count,
                    created_at,expires_at,lease_token,lease_index,lease_expires_at)
                   VALUES (?,?,?,?,?,0,?,?,?,?,0,?)""",
                (scope, sender_id, generation, source_id, full_text, len(encoded),
                 timestamp, expiry, token, lease_expiry),
            )
            for index, page_json in enumerate(encoded):
                connection.execute(
                    "INSERT INTO page_items(generation,page_index,page_json) VALUES (?,?,?)",
                    (generation, index, page_json),
                )
            return {
                "token": token, "generation": generation, "scope": scope,
                "sender_id": sender_id, "index": 0, "page": json.loads(encoded[0]),
            }

    def reserve_next(self, scope: str, sender_id: str) -> dict | None:
        scope = _identifier(scope, "scope")
        sender_id = _identifier(sender_id, "sender_id")
        with self._transaction() as connection:
            timestamp = self._timestamp()
            self._reap(connection, timestamp)
            row = connection.execute(
                "SELECT * FROM page_responses WHERE scope=? AND sender_id=?", (scope, sender_id),
            ).fetchone()
            if row is None:
                return None
            self._row_valid(row)
            if row["next_index"] == row["page_count"] or row["lease_token"] is not None:
                return None
            page = self._current_page(connection, row)
            token = secrets.token_hex(32)
            lease_expiry = self._deadline(timestamp, self.lease_seconds, row["expires_at"])
            connection.execute(
                """UPDATE page_responses SET lease_token=?,lease_index=next_index,lease_expires_at=?
                   WHERE scope=? AND sender_id=? AND generation=? AND lease_token IS NULL""",
                (token, lease_expiry, scope, sender_id, row["generation"]),
            )
            return self._lease(row, page, token)

    def _finish_lease(self, lease: Any, action: str) -> bool:
        identity = _lease_identity(lease)
        with self._transaction() as connection:
            timestamp = self._timestamp()
            self._reap(connection, timestamp)
            if identity is None:
                return False
            scope, sender_id, generation, token, index = identity
            row = connection.execute(
                "SELECT * FROM page_responses WHERE scope=? AND sender_id=?", (scope, sender_id),
            ).fetchone()
            if row is None:
                return False
            self._row_valid(row)
            if (row["generation"] != generation or row["lease_token"] != token
                    or row["next_index"] != index or row["lease_index"] != index):
                return False
            self._current_page(connection, row)
            parameters = (scope, sender_id, generation, token, index, index, timestamp, timestamp)
            if action == "renew":
                deadline = max(row["lease_expires_at"],
                               self._deadline(timestamp, self.lease_seconds, row["expires_at"]))
                cursor = connection.execute(
                    """UPDATE page_responses SET lease_expires_at=?
                       WHERE scope=? AND sender_id=? AND generation=? AND lease_token=?
                         AND lease_index=? AND next_index=? AND expires_at>? AND lease_expires_at>?""",
                    (deadline, *parameters),
                )
            elif action == "ack":
                cursor = connection.execute(
                    """UPDATE page_responses SET next_index=next_index+1,
                       lease_token=NULL,lease_index=NULL,lease_expires_at=NULL
                       WHERE scope=? AND sender_id=? AND generation=? AND lease_token=?
                         AND lease_index=? AND next_index=? AND expires_at>? AND lease_expires_at>?""",
                    parameters,
                )
            else:  # release: leave the unconfirmed index untouched.
                cursor = connection.execute(
                    """UPDATE page_responses SET lease_token=NULL,lease_index=NULL,lease_expires_at=NULL
                       WHERE scope=? AND sender_id=? AND generation=? AND lease_token=?
                         AND lease_index=? AND next_index=? AND expires_at>? AND lease_expires_at>?""",
                    parameters,
                )
            return cursor.rowcount == 1

    def ack(self, lease: dict) -> bool:
        """Confirm exactly the current reserved page; never infer partial delivery."""
        return self._finish_lease(lease, "ack")

    def release(self, lease: dict) -> bool:
        """Fence a failed send's token without advancing its page index."""
        return self._finish_lease(lease, "release")

    def renew(self, lease: dict) -> bool:
        """Extend only a still-valid current lease, capped by the original response TTL."""
        return self._finish_lease(lease, "renew")

    def discard(self, scope: str, sender_id: str) -> bool:
        scope = _identifier(scope, "scope")
        sender_id = _identifier(sender_id, "sender_id")
        with self._transaction() as connection:
            timestamp = self._timestamp()
            self._reap(connection, timestamp)
            cursor = connection.execute(
                "DELETE FROM page_responses WHERE scope=? AND sender_id=?", (scope, sender_id),
            )
            return cursor.rowcount == 1

    def metadata(self, scope: str | None = None, sender_id: str | None = None) -> dict:
        if scope is not None:
            scope = _identifier(scope, "scope")
        if sender_id is not None:
            sender_id = _identifier(sender_id, "sender_id")
        with self._transaction() as connection:
            timestamp = self._timestamp()
            self._reap(connection, timestamp)
            rows = connection.execute(
                "SELECT " + _META_COLUMNS + " FROM page_responses "
                "WHERE (? IS NULL OR scope=?) AND (? IS NULL OR sender_id=?) ORDER BY scope,sender_id",
                (scope, scope, sender_id, sender_id),
            ).fetchall()
            entries = []
            for row in rows:
                self._row_valid(row)
                remaining = row["page_count"] - row["next_index"]
                entries.append({
                    "scope": row["scope"], "sender_id": row["sender_id"], "user": row["sender_id"],
                    "index": row["next_index"], "pages": row["page_count"], "remaining": remaining,
                    "active": remaining > 0, "busy": row["lease_token"] is not None,
                    "expiry": row["expires_at"], "lease_expiry": row["lease_expires_at"],
                    "generation": row["generation"],
                })
            return {"schema_version": _SCHEMA_VERSION, "count": len(entries), "entries": entries}

    def peek_metadata(self, scope: str, sender_id: str) -> dict | None:
        entries = self.metadata(scope, sender_id)["entries"]
        return entries[0] if entries else None

    def close(self) -> None:
        """Idempotently reject future operations; no connection stays open between calls."""
        self._closed = True
