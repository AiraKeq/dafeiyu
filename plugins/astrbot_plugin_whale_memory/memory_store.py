"""Scoped, bounded-context memory for the whale bot (Python standard library only).

Importance is an enduring property of an event. Recency is calculated from its
original message timestamp and is never written back into importance.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import json
import math
from pathlib import Path
import re
import sqlite3
import time
from typing import Any


SHANGHAI = timezone(timedelta(hours=8), name="Asia/Shanghai")
DAY = 86400.0


def _epoch(value: float | datetime | None = None) -> float:
    if value is None:
        return time.time()
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=SHANGHAI)
        value = value.timestamp()
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("Timestamp must be a finite epoch number or datetime")
    value = float(value)
    if not math.isfinite(value):
        raise ValueError("Timestamp must be finite")
    return value


def _day(ts: float) -> str:
    return datetime.fromtimestamp(ts, SHANGHAI).date().isoformat()


def _text(value: Any, maximum: int, *, required: bool = False) -> str:
    if not isinstance(value, str):
        raise ValueError("Memory text must be a string")
    value = value.replace("\x00", "").strip()[:maximum]
    if required and not value:
        raise ValueError("Memory text must not be empty")
    return value


def _tokens(text: str) -> set[str]:
    text = text.casefold()
    result = set(re.findall(r"[a-z0-9_]+", text))
    for run in re.findall(r"[\u3400-\u4dbf\u4e00-\u9fff]+", text):
        if len(run) == 1:
            result.add(run)
        else:
            result.update(run[index:index + 2] for index in range(len(run) - 1))
    return result


class MemoryStore:
    def __init__(
        self,
        db_path: str | Path,
        active_days: int = 14,
        important_threshold: float = 8,
        half_life_days: float = 4,
        raw_retention_days: int = 14,
    ) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.active_days = max(1, int(active_days))
        self.important_threshold = float(important_threshold)
        self.half_life_days = float(half_life_days)
        self.raw_retention_days = max(1, int(raw_retention_days))
        if not math.isfinite(self.important_threshold) or not 0 <= self.important_threshold <= 10:
            raise ValueError("Important threshold must be between zero and ten")
        if not math.isfinite(self.half_life_days) or self.half_life_days <= 0:
            raise ValueError("Recency half-life must be positive")
        self._initialize()

    @contextmanager
    def _connection(self):
        connection = sqlite3.connect(str(self.db_path), timeout=20, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=20000")
        connection.execute("PRAGMA foreign_keys=ON")
        try:
            yield connection
        finally:
            connection.close()

    @contextmanager
    def _transaction(self):
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                yield connection
            except BaseException:
                connection.rollback()
                raise
            else:
                connection.commit()

    def _initialize(self) -> None:
        with self._connection() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version > 1:
                raise RuntimeError("This memory database was created by a newer plugin")
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY,
                    scope TEXT NOT NULL,
                    message_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    sender_id TEXT NOT NULL,
                    sender_name TEXT NOT NULL,
                    text TEXT NOT NULL,
                    ts REAL NOT NULL,
                    day TEXT NOT NULL,
                    processed INTEGER NOT NULL DEFAULT 0,
                    UNIQUE(scope, message_id, role)
                );
                CREATE INDEX IF NOT EXISTS messages_pending
                    ON messages(processed, ts, scope, day);
                CREATE INDEX IF NOT EXISTS messages_scope_time
                    ON messages(scope, ts DESC, id DESC);
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY,
                    scope TEXT NOT NULL,
                    day TEXT NOT NULL,
                    title TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    core TEXT NOT NULL,
                    importance REAL NOT NULL CHECK(importance >= 0 AND importance <= 10),
                    participants TEXT NOT NULL,
                    keywords TEXT NOT NULL,
                    ts REAL NOT NULL,
                    tier TEXT NOT NULL DEFAULT 'active',
                    created_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS events_scope_time ON events(scope, ts DESC);
                CREATE TABLE IF NOT EXISTS styles (
                    scope TEXT PRIMARY KEY,
                    day TEXT NOT NULL,
                    notes TEXT NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS proactive_replies (
                    id INTEGER PRIMARY KEY,
                    scope TEXT NOT NULL,
                    ts REAL NOT NULL,
                    day TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS proactive_scope_time ON proactive_replies(scope, ts);
                CREATE TABLE IF NOT EXISTS reply_state (
                    scope TEXT PRIMARY KEY,
                    last_reply REAL NOT NULL
                );
                PRAGMA user_version=1;
            """)

    def add_message(
        self, scope: str, message_id: str, role: str, sender_id: str,
        sender_name: str, text: str, ts: float | datetime | None = None,
    ) -> bool:
        timestamp = _epoch(ts)
        scope = _text(scope, 240, required=True)
        message_id = _text(str(message_id), 240, required=True)
        role = _text(role, 32, required=True)
        sender_id = _text(str(sender_id), 128)
        sender_name = _text(sender_name, 160)
        text = _text(text, 20000, required=True)
        with self._transaction() as connection:
            cursor = connection.execute(
                """INSERT OR IGNORE INTO messages
                   (scope,message_id,role,sender_id,sender_name,text,ts,day)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (scope, message_id, role, sender_id, sender_name, text, timestamp, _day(timestamp)),
            )
            return cursor.rowcount == 1

    def pending_batches(
        self, before_ts: float | datetime, batch_size: int = 80, max_batches: int = 20,
    ) -> list[dict]:
        before_ts = _epoch(before_ts)
        batch_size = min(500, max(1, int(batch_size)))
        max_batches = max(0, int(max_batches))
        if not max_batches:
            return []
        with self._connection() as connection:
            rows = connection.execute(
                """SELECT id,scope,day,message_id,role,sender_id,sender_name,text,ts
                   FROM messages WHERE processed=0 AND ts<?
                   ORDER BY day,scope,ts,id LIMIT ?""",
                (before_ts, batch_size * max_batches),
            ).fetchall()
        batches: list[dict] = []
        for row in rows:
            if (not batches or batches[-1]["scope"] != row["scope"]
                    or batches[-1]["day"] != row["day"]
                    or len(batches[-1]["rows"]) >= batch_size):
                if len(batches) >= max_batches:
                    break
                batches.append({"scope": row["scope"], "day": row["day"], "rows": []})
            batches[-1]["rows"].append({key: row[key] for key in (
                "id", "message_id", "role", "sender_id", "sender_name", "text", "ts",
            )})
        return batches

    @staticmethod
    def _event(value: dict) -> dict:
        if not isinstance(value, dict):
            raise ValueError("Every event must be an object")
        importance = value.get("importance")
        if (isinstance(importance, bool) or not isinstance(importance, (int, float))
                or not math.isfinite(float(importance)) or not 0 <= importance <= 10):
            raise ValueError("Event importance must be a finite number between zero and ten")
        detail = _text(value.get("detail", ""), 640)
        title = _text(value.get("title", ""), 100)
        core = _text(value.get("core", detail or title), 160, required=True)
        if not detail:
            detail = core
        if not title:
            title = core[:60]

        def labels(key: str, limit: int, width: int) -> list[str]:
            items = value.get(key, [])
            if not isinstance(items, list):
                raise ValueError(key + " must be a list")
            result = []
            for item in items[:limit]:
                item = _text(item, width)
                if item and item not in result:
                    result.append(item)
            return result

        return {"title": title, "detail": detail, "core": core, "importance": float(importance),
                "participants": labels("participants", 16, 128), "keywords": labels("keywords", 16, 48)}

    def commit_events(
        self, scope: str, day: str, message_ids: list[int], events: list[dict],
        style_notes: str = "", now: float | datetime | None = None,
    ) -> dict:
        timestamp = _epoch(now)
        if not isinstance(events, list) or len(events) > 80:
            raise ValueError("Events must be a list of at most 80 items")
        normalized = [self._event(event) for event in events]
        notes = _text(style_notes, 1000)
        if not isinstance(message_ids, list) or any(type(item) is not int for item in message_ids):
            raise ValueError("Message ids must be an integer list")
        ids = list(dict.fromkeys(message_ids))
        if not ids:
            if normalized or notes:
                raise ValueError("Events and styles must be supported by source messages")
            return {"inserted": 0, "processed": 0}
        if len(ids) > 500:
            raise ValueError("A consolidation batch may contain at most 500 messages")
        placeholders = ",".join("?" for _ in ids)
        with self._transaction() as connection:
            rows = connection.execute(
                "SELECT id,scope,day,processed,ts FROM messages WHERE id IN (" + placeholders + ")", ids,
            ).fetchall()
            if (len(rows) != len(ids) or any(row["scope"] != scope or row["day"] != day for row in rows)):
                raise ValueError("Source messages must all exist in this scope and Shanghai day")
            processed = sum(row["processed"] for row in rows)
            if processed == len(ids):
                return {"inserted": 0, "processed": 0}
            if processed:
                raise ValueError("Source batch partially processed; retry only pending messages")
            # Model-supplied occurrence dates cannot move an event forward or out of its source day.
            event_ts = max(row["ts"] for row in rows)
            for event in normalized:
                connection.execute(
                    """INSERT INTO events
                       (scope,day,title,detail,core,importance,participants,keywords,ts,tier,created_at)
                       VALUES (?,?,?,?,?,?,?,?,?,'active',?)""",
                    (scope, day, event["title"], event["detail"], event["core"], event["importance"],
                     json.dumps(event["participants"], ensure_ascii=False),
                     json.dumps(event["keywords"], ensure_ascii=False), event_ts, timestamp),
                )
            connection.execute("UPDATE messages SET processed=1 WHERE id IN (" + placeholders + ")", ids)
            if notes:
                connection.execute(
                    """INSERT INTO styles(scope,day,notes,updated_at) VALUES(?,?,?,?)
                       ON CONFLICT(scope) DO UPDATE SET day=excluded.day,notes=excluded.notes,
                       updated_at=excluded.updated_at WHERE excluded.day >= styles.day""",
                    (scope, day, notes, timestamp),
                )
            return {"inserted": len(normalized), "processed": len(ids)}

    def apply_lifecycle(self, now: float | datetime | None = None) -> dict:
        timestamp = _epoch(now)
        expired = timestamp - self.active_days * DAY
        counts = {"archived": 0, "deleted_events": 0, "compressed": 0, "deleted_messages": 0}
        with self._transaction() as connection:
            counts["deleted_events"] = connection.execute(
                "DELETE FROM events WHERE ts<=? AND importance<?", (expired, self.important_threshold),
            ).rowcount
            counts["archived"] = connection.execute(
                """UPDATE events SET tier='archive',detail=substr(core,1,160),core=substr(core,1,160)
                   WHERE ts<=? AND importance>=? AND tier!='archive'""",
                (expired, self.important_threshold),
            ).rowcount
            for cutoff_days, width in ((7, 160), (3, 320), (0, 640)):
                counts["compressed"] += connection.execute(
                    """UPDATE events SET detail=substr(detail,1,?) WHERE tier='active'
                       AND ts<=? AND length(detail)>?""",
                    (width, timestamp - cutoff_days * DAY, width),
                ).rowcount
            counts["deleted_messages"] = connection.execute(
                "DELETE FROM messages WHERE processed=1 AND ts<?",
                (timestamp - self.raw_retention_days * DAY,),
            ).rowcount
            connection.execute("DELETE FROM proactive_replies WHERE ts<?", (timestamp - 30 * DAY,))
        return counts

    def recency(self, event_ts: float | datetime, now: float | datetime | None = None) -> float:
        age = max(0.0, (_epoch(now) - _epoch(event_ts)) / DAY)
        return 10.0 * (2.0 ** (-age / self.half_life_days))

    @staticmethod
    def _relevance(query: str, query_tokens: set[str], event: sqlite3.Row) -> float:
        if not query.strip():
            return 0.0
        text = " ".join((event["title"], event["core"], event["detail"]))
        overlap = len(query_tokens & _tokens(text)) / max(1, len(query_tokens))
        keywords = json.loads(event["keywords"])
        lowered_query = query.casefold()
        hits = [word for word in keywords if len(word) >= 2 and word.casefold() in lowered_query]
        if hits:
            overlap = max(overlap, 0.65)
        return min(1.0, overlap)

    def recall(
        self, scope: str, query: str, now: float | datetime | None = None,
        max_chars: int = 2200, limit: int = 8,
        *, semantic_scores: dict | None = None, min_similarity: float = 0.70,
    ) -> str:
        # Only standard-library shared code; no optional embedding/index import.
        # The snapshot reader cannot create or change the live fact DB/WAL/SHM.
        try:
            from .semantic_state import read_events, render_recall
        except ImportError:
            try:
                from semantic_state import read_events, render_recall
            except ImportError:
                read_events = render_recall = None
        if read_events is not None:
            return render_recall(
                read_events(self.db_path, scope), scope, query, _epoch(now),
                active_days=self.active_days, important_threshold=self.important_threshold,
                half_life_days=self.half_life_days, max_chars=max_chars, limit=limit,
                semantic_scores=semantic_scores, min_similarity=min_similarity)
        timestamp = _epoch(now)
        max_chars = max(0, int(max_chars))
        limit = max(0, int(limit))
        if not max_chars or not limit:
            return ""
        if not self.db_path.is_file():
            return ""
        query_tokens = _tokens(query)
        relevant = []
        recent = []
        with self._connection() as connection:
            for stored in connection.execute("SELECT * FROM events WHERE scope=? ORDER BY ts DESC,id DESC", (scope,)):
                event = dict(stored)
                age = max(0.0, timestamp - event["ts"])
                if age >= self.active_days * DAY and event["importance"] < self.important_threshold:
                    continue
                event["core"] = event["core"][:160]
                if event["tier"] == "archive" or age >= self.active_days * DAY:
                    event["tier"] = "archive"
                    event["detail"] = event["core"]
                else:
                    width = 160 if age >= 7 * DAY else 320 if age >= 3 * DAY else 640
                    event["detail"] = event["detail"][:width]
                relevance = self._relevance(query, query_tokens, event)
                recency = self.recency(event["ts"], timestamp)
                item = (0.5 * relevance + 0.3 * event["importance"] / 10 + 0.2 * recency / 10,
                        event["ts"], event["id"], event, recency)
                # Old archived memories must match the current topic to enter the prompt.
                if relevance > 0:
                    relevant.append(item)
                if event["tier"] == "active" and age < 3 * DAY and len(recent) < 2:
                    recent.append(item)
        relevant.sort(key=lambda item: item[:3], reverse=True)
        selected = []
        seen = set()
        # Leave room for at most two recent memories without displacing all topical recall.
        relevant_count = min(len(relevant), max(1, limit - min(2, len(recent))))
        for item in relevant[:relevant_count] + recent + relevant[relevant_count:]:
            if item[2] not in seen:
                selected.append(item)
                seen.add(item[2])
            if len(selected) >= limit:
                break
        lines = []
        used = 0
        for _, _, _, event, recency in selected:
            detail = event["core"] if event["tier"] == "archive" else event["detail"]
            if event["core"] not in detail:
                detail = event["core"] + "；" + detail
            detail = re.sub(r"\s+", " ", detail)
            title = re.sub(r"\s+", " ", event["title"])
            tier = "长期核心" if event["tier"] == "archive" else "近期"
            line = (f"[事件 {event['id']}｜{event['day']}｜{tier}｜"
                    f"I={event['importance']:.1f}/10｜R={recency:.2f}/10] {title}：{detail}")
            separator = 1 if lines else 0
            remaining = max_chars - used - separator
            if remaining <= 0:
                break
            line = line[:remaining]
            lines.append(line)
            used += separator + len(line)
        return "\n".join(lines)

    def recent_messages(
        self, scope: str, limit: int = 16, max_chars: int = 2000,
        *, exclude_message_id: str | None = None,
    ) -> str:
        max_chars = max(0, int(max_chars))
        limit = min(100, max(0, int(limit)))
        if not max_chars or not limit:
            return ""
        with self._connection() as connection:
            if exclude_message_id is None:
                rows = connection.execute(
                    "SELECT * FROM messages WHERE scope=? ORDER BY ts DESC,id DESC LIMIT ?", (scope, limit),
                ).fetchall()
            else:
                rows = connection.execute(
                    """SELECT * FROM messages WHERE scope=? AND message_id!=?
                       ORDER BY ts DESC,id DESC LIMIT ?""", (scope, str(exclude_message_id), limit),
                ).fetchall()
        lines = []
        used = 0
        for row in rows:
            label = row["sender_name"] or row["sender_id"] or row["role"]
            label = re.sub(r"\s+", " ", label)
            kind = "机器人生成" if row["role"] == "assistant" else "朋友"
            timestamp = datetime.fromtimestamp(row["ts"], SHANGHAI).strftime("%m-%d %H:%M")
            content = re.sub(r"\s+", " ", row["text"])
            line = f"[{timestamp} {kind} {label}({row['sender_id']})] {content}"
            remaining = max_chars - used - (1 if lines else 0)
            if remaining <= 0:
                break
            line = line[:remaining]
            lines.append(line)
            used += len(line) + (1 if len(lines) > 1 else 0)
        return "\n".join(reversed(lines))

    def get_style(self, scope: str) -> str:
        with self._connection() as connection:
            row = connection.execute("SELECT notes FROM styles WHERE scope=?", (scope,)).fetchone()
            return row["notes"] if row else ""

    def statistics(self, scope: str | None = None) -> dict:
        where = " WHERE scope=?" if scope is not None else ""
        args = (scope,) if scope is not None else ()
        with self._connection() as connection:
            messages = connection.execute(
                "SELECT count(*) AS total,coalesce(sum(processed),0) AS processed FROM messages" + where, args,
            ).fetchone()
            events = connection.execute(
                "SELECT count(*) AS total,coalesce(sum(tier='archive'),0) AS archived FROM events" + where, args,
            ).fetchone()
            styles = connection.execute("SELECT count(*) FROM styles" + where, args).fetchone()[0]
        return {"messages": messages["total"], "pending_messages": messages["total"] - messages["processed"],
                "processed_messages": messages["processed"], "events": events["total"],
                "active_events": events["total"] - events["archived"], "archived_events": events["archived"],
                "styles": styles}

    def forget_event(self, scope: str, event_id: int) -> bool:
        with self._transaction() as connection:
            return connection.execute("DELETE FROM events WHERE scope=? AND id=?", (scope, int(event_id))).rowcount == 1

    def reserve_proactive(
        self, scope: str, now: float | datetime | None,
        min_gap_seconds: float, hour_cap: int, day_cap: int,
    ) -> bool:
        timestamp = _epoch(now)
        gap = float(min_gap_seconds)
        if not math.isfinite(gap):
            raise ValueError("Reply gap must be finite")
        hour_cap, day_cap = int(hour_cap), int(day_cap)
        with self._transaction() as connection:
            latest = connection.execute("SELECT last_reply FROM reply_state WHERE scope=?", (scope,)).fetchone()
            if latest and timestamp - latest["last_reply"] < max(0.0, gap):
                return False
            # Nonpositive caps explicitly mean unlimited; the time gap still applies.
            if hour_cap > 0:
                count = connection.execute(
                    "SELECT count(*) FROM proactive_replies WHERE scope=? AND ts>? AND ts<=?",
                    (scope, timestamp - 3600, timestamp),
                ).fetchone()[0]
                if count >= hour_cap:
                    return False
            if day_cap > 0:
                count = connection.execute(
                    "SELECT count(*) FROM proactive_replies WHERE scope=? AND day=?", (scope, _day(timestamp)),
                ).fetchone()[0]
                if count >= day_cap:
                    return False
            connection.execute(
                "INSERT INTO proactive_replies(scope,ts,day) VALUES(?,?,?)", (scope, timestamp, _day(timestamp)),
            )
            self._update_reply(connection, scope, timestamp)
            return True

    @staticmethod
    def _update_reply(connection: sqlite3.Connection, scope: str, timestamp: float) -> None:
        connection.execute(
            """INSERT INTO reply_state(scope,last_reply) VALUES(?,?) ON CONFLICT(scope)
               DO UPDATE SET last_reply=max(reply_state.last_reply,excluded.last_reply)""",
            (scope, timestamp),
        )

    def record_actual_reply(self, scope: str, now: float | datetime | None = None) -> None:
        with self._transaction() as connection:
            self._update_reply(connection, scope, _epoch(now))
