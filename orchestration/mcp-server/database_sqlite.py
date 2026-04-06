"""
SQLite async database adapter for SPEAKMAN.AI.
Activated when USE_SQLITE=true in the environment.

Schema
------
sessions  — top-level session metadata (one row per workflow run)
events    — append-only event log (many rows per session)
kv_store  — JSON blob store for workflows, agents, projects, settings
"""
import json
import logging
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import aiosqlite

log = logging.getLogger(__name__)

_DEFAULT_DB_PATH = str(Path.home() / ".speakmanai" / "speakmanai.db")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    session_id     TEXT PRIMARY KEY,
    owner_id       TEXT NOT NULL DEFAULT 'local_user',
    session_title  TEXT,
    current_status TEXT,
    workflow_id    TEXT,
    created_at     TEXT,
    updated_at     TEXT,
    error_message  TEXT
);
CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  TEXT NOT NULL,
    event_data  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS kv_store (
    collection  TEXT NOT NULL,
    doc_id      TEXT NOT NULL,
    doc_data    TEXT NOT NULL,
    PRIMARY KEY (collection, doc_id)
);
CREATE INDEX IF NOT EXISTS idx_events_session ON events(session_id);
CREATE INDEX IF NOT EXISTS idx_sessions_created ON sessions(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_sessions_workflow ON sessions(workflow_id);
CREATE INDEX IF NOT EXISTS idx_kv_collection ON kv_store(collection);
"""

# Maps collection name → document primary-key field
_PK = {
    "workflows": "workflowId",
    "agents":    "agentId",
    "projects":  "session_id",
    "settings":  "_id",
}

# Shared single connection (single-user desktop — no pool needed)
_conn: Optional[aiosqlite.Connection] = None


async def init_sqlite(db_path: str = None) -> None:
    global _conn
    path = db_path or os.environ.get("SPEAKMANAI_DB_PATH", _DEFAULT_DB_PATH)
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    _conn = await aiosqlite.connect(path)
    _conn.row_factory = aiosqlite.Row
    await _conn.executescript(_SCHEMA)
    await _conn.commit()
    log.info(f"SQLite database initialized: {path}")


async def close_sqlite() -> None:
    global _conn
    if _conn:
        await _conn.close()
        _conn = None


async def _ensure_conn() -> aiosqlite.Connection:
    global _conn
    if _conn is None:
        await init_sqlite()
    return _conn


# ─────────────────────────────────────────────
# Python-side filter evaluation
# ─────────────────────────────────────────────

def _matches(doc: dict, filter: dict) -> bool:
    """Evaluate a subset of filter operators against a Python dict."""
    for key, value in filter.items():
        # Dot-notation traversal: "events.0.attributes.workflow_id"
        if "." in key:
            parts = key.split(".")
            v: Any = doc
            for part in parts:
                if v is None:
                    break
                if isinstance(v, list):
                    try:
                        v = v[int(part)]
                    except (ValueError, IndexError):
                        v = None
                elif isinstance(v, dict):
                    v = v.get(part)
                else:
                    v = None
            doc_val = v
        else:
            doc_val = doc.get(key)

        if isinstance(value, dict):
            for op, op_val in value.items():
                if op == "$in":
                    if doc_val not in op_val:
                        return False
                elif op == "$eq":
                    if doc_val != op_val:
                        return False
                elif op == "$ne":
                    if doc_val == op_val:
                        return False
        elif isinstance(doc_val, list) and not isinstance(value, list):
            # MongoDB: scalar filter vs array field → membership check
            if value not in doc_val:
                return False
        else:
            if doc_val != value:
                return False
    return True


def _deep_set(obj: Any, dot_path: str, value: Any) -> None:
    """Set a nested value using dot-notation (no positional $ support needed here)."""
    parts = dot_path.split(".")
    for part in parts[:-1]:
        if isinstance(obj, dict):
            obj = obj.setdefault(part, {})
        else:
            return
    if isinstance(obj, dict):
        obj[parts[-1]] = value


# ─────────────────────────────────────────────
# Async cursor
# ─────────────────────────────────────────────

class SQLiteCursor:
    """Async cursor with .sort(), .limit(), .to_list(), and async iteration."""

    def __init__(self, collection: "SQLiteCollection", filter: dict, projection: dict):
        self._col = collection
        self._filter = filter
        self._projection = projection
        self._sort_key: Optional[str] = None
        self._sort_reverse = False
        self._limit_n: Optional[int] = None

    def sort(self, field, direction=1):
        self._sort_key = field
        self._sort_reverse = (direction == -1)
        return self

    def limit(self, n: int):
        self._limit_n = n
        return self

    async def _fetch(self) -> list[dict]:
        docs = await self._col._fetch_all(self._filter)
        if self._sort_key:
            docs.sort(
                key=lambda d: (d.get(self._sort_key) or ""),
                reverse=self._sort_reverse,
            )
        if self._limit_n is not None:
            docs = docs[: self._limit_n]
        return docs

    async def to_list(self, length: int = None) -> list[dict]:
        docs = await self._fetch()
        return docs[:length] if length is not None else docs

    def __aiter__(self):
        return self._aiter()

    async def _aiter(self):
        for doc in await self._fetch():
            yield doc


# ─────────────────────────────────────────────
# Collection
# ─────────────────────────────────────────────

class SQLiteCollection:
    """Async collection backed by SQLite."""

    def __init__(self, name: str):
        self._name = name

    # ── Public interface ──────────────────────────────────────────

    async def find_one(self, filter: dict, projection: dict = None) -> Optional[dict]:
        if self._name == "events_raw":
            return await self._events_find_one(filter)
        return await self._kv_find_one(filter)

    def find(self, filter: dict = None, projection: dict = None) -> SQLiteCursor:
        return SQLiteCursor(self, filter or {}, projection)

    async def _fetch_all(self, filter: dict) -> list[dict]:
        """Used internally by SQLiteCursor."""
        if self._name == "events_raw":
            return await self._events_fetch_all(filter)
        return await self._kv_fetch_all(filter)

    async def update_one(self, filter: dict, update: dict, upsert: bool = False) -> None:
        if self._name == "events_raw":
            await self._events_update_one(filter, update, upsert)
        else:
            await self._kv_update_one(filter, update, upsert)

    # ── events_raw: normalized sessions + events tables ──────────

    async def _events_find_one(self, filter: dict) -> Optional[dict]:
        session_id = filter.get("session_id")
        if not session_id:
            return None
        conn = await _ensure_conn()
        async with conn.execute(
            "SELECT * FROM sessions WHERE session_id = ?", [session_id]
        ) as cur:
            srow = await cur.fetchone()
        if not srow:
            return None
        async with conn.execute(
            "SELECT event_data FROM events WHERE session_id = ? ORDER BY id",
            [session_id],
        ) as cur:
            erows = await cur.fetchall()
        return self._reconstruct(srow, erows)

    async def _events_fetch_all(self, filter: dict) -> list[dict]:
        conn = await _ensure_conn()
        where, params = [], []
        if "owner_id" in filter and not isinstance(filter["owner_id"], dict):
            where.append("owner_id = ?")
            params.append(filter["owner_id"])

        sql = "SELECT * FROM sessions"
        if where:
            sql += " WHERE " + " AND ".join(where)

        async with conn.execute(sql, params) as cur:
            srows = await cur.fetchall()

        docs = []
        for srow in srows:
            async with conn.execute(
                "SELECT event_data FROM events WHERE session_id = ? ORDER BY id",
                [srow["session_id"]],
            ) as cur:
                erows = await cur.fetchall()
            doc = self._reconstruct(srow, erows)
            if _matches(doc, filter):
                docs.append(doc)
        return docs

    @staticmethod
    def _reconstruct(srow, erows) -> dict:
        events = [json.loads(r["event_data"]) for r in erows]
        created = srow["created_at"]
        updated = srow["updated_at"]
        return {
            "session_id":    srow["session_id"],
            "owner_id": srow["owner_id"] or "local_user",
            "session_title": srow["session_title"] or "",
            "current_status": srow["current_status"] or "UNKNOWN",
            "workflow_id":   srow["workflow_id"] or "",
            "created_at":    datetime.fromisoformat(created) if created else datetime.now(timezone.utc),
            "updated_at":    datetime.fromisoformat(updated) if updated else None,
            "error_message": srow["error_message"],
            "events":        events,
        }

    async def _events_update_one(self, filter: dict, update: dict, upsert: bool) -> None:
        conn = await _ensure_conn()
        session_id = filter.get("session_id")
        if not session_id:
            return

        set_fields      = update.get("$set", {})
        push_fields     = update.get("$push", {})
        set_on_insert   = update.get("$setOnInsert", {})
        now_str         = datetime.now(timezone.utc).isoformat()

        # ── Positional update: update workflow_definition on the initial event ──
        # filter key pattern: "events.attributes.current_step_index" (or similar)
        if any(k.startswith("events.") for k in filter):
            # Find the field to update: "events.$.data.workflow_definition"
            for path, value in set_fields.items():
                if not (path.startswith("events.$") or path.startswith("events.0")):
                    continue
                # Inner path after "events.$/events.0/"
                inner = path.split(".", 2)[2]
                async with conn.execute(
                    "SELECT id, event_data FROM events WHERE session_id = ? ORDER BY id LIMIT 1",
                    [session_id],
                ) as cur:
                    erow = await cur.fetchone()
                if erow:
                    event = json.loads(erow["event_data"])
                    _deep_set(event, inner, value)
                    await conn.execute(
                        "UPDATE events SET event_data = ? WHERE id = ?",
                        [json.dumps(event), erow["id"]],
                    )
            await conn.commit()
            return

        # ── Standard upsert ──────────────────────────────────────────────────
        async with conn.execute(
            "SELECT session_id FROM sessions WHERE session_id = ?", [session_id]
        ) as cur:
            exists = await cur.fetchone()

        if not exists:
            if not upsert:
                return
            new_event  = push_fields.get("events", {})
            attrs      = new_event.get("attributes", {}) if new_event else {}
            owner      = set_on_insert.get("owner_id") or attrs.get("owner_id") or "local_user"
            title      = set_on_insert.get("session_title") or attrs.get("session_title") or set_fields.get("session_title")
            wf_id      = attrs.get("workflow_id")
            status     = attrs.get("status") or set_fields.get("current_status") or "STARTING"
            created    = (set_on_insert.get("created_at") or datetime.now(timezone.utc)).isoformat()
            await conn.execute(
                """INSERT INTO sessions
                   (session_id, owner_id, session_title, workflow_id, current_status, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                [session_id, owner, title, wf_id, status, created, now_str],
            )

        # Apply $set to session columns
        col_map = {
            "current_status": "current_status",
            "session_title":  "session_title",
            "owner_id": "owner_id",
            "error_message":  "error_message",
        }
        set_pairs = [(col_map[k], v) for k, v in set_fields.items() if k in col_map]
        set_pairs.append(("updated_at", now_str))
        if set_pairs:
            cols = ", ".join(f"{c} = ?" for c, _ in set_pairs)
            vals = [v.isoformat() if isinstance(v, datetime) else v for _, v in set_pairs]
            await conn.execute(
                f"UPDATE sessions SET {cols} WHERE session_id = ?",
                vals + [session_id],
            )

        # Push new event
        if "events" in push_fields:
            await conn.execute(
                "INSERT INTO events (session_id, event_data) VALUES (?, ?)",
                [session_id, json.dumps(push_fields["events"])],
            )

        await conn.commit()

    # ── kv_store: workflows, agents, projects, settings ──────────

    def _pk_field(self) -> str:
        return _PK.get(self._name, "id")

    def _doc_id_from(self, filter: dict, doc: dict = None) -> Optional[str]:
        pk = self._pk_field()
        if pk in filter and not isinstance(filter[pk], dict):
            return str(filter[pk])
        if doc and pk in doc:
            return str(doc[pk])
        return None

    async def _kv_find_one(self, filter: dict) -> Optional[dict]:
        conn = await _ensure_conn()
        doc_id = self._doc_id_from(filter)
        if doc_id:
            async with conn.execute(
                "SELECT doc_data FROM kv_store WHERE collection = ? AND doc_id = ?",
                [self._name, doc_id],
            ) as cur:
                row = await cur.fetchone()
            return json.loads(row["doc_data"]) if row else None

        # Full scan + Python filter
        async with conn.execute(
            "SELECT doc_data FROM kv_store WHERE collection = ?", [self._name]
        ) as cur:
            rows = await cur.fetchall()
        for row in rows:
            doc = json.loads(row["doc_data"])
            if _matches(doc, filter):
                return doc
        return None

    async def _kv_fetch_all(self, filter: dict) -> list[dict]:
        conn = await _ensure_conn()
        async with conn.execute(
            "SELECT doc_data FROM kv_store WHERE collection = ?", [self._name]
        ) as cur:
            rows = await cur.fetchall()
        results = []
        for row in rows:
            doc = json.loads(row["doc_data"])
            if not filter or _matches(doc, filter):
                results.append(doc)
        return results

    async def _kv_update_one(self, filter: dict, update: dict, upsert: bool) -> None:
        conn = await _ensure_conn()
        doc = await self._kv_find_one(filter)
        is_new = doc is None

        if is_new:
            if not upsert:
                return
            doc = {}
            pk = self._pk_field()
            if pk in filter and not isinstance(filter[pk], dict):
                doc[pk] = filter[pk]

        # $setOnInsert — only on new docs
        if is_new:
            for k, v in update.get("$setOnInsert", {}).items():
                if k not in doc:
                    doc[k] = v.isoformat() if isinstance(v, datetime) else v

        # $set
        for k, v in update.get("$set", {}).items():
            doc[k] = v.isoformat() if isinstance(v, datetime) else v

        # $addToSet
        for key, value in update.get("$addToSet", {}).items():
            current = doc.get(key, [])
            if not isinstance(current, list):
                current = []
            if isinstance(value, dict) and "$each" in value:
                for item in value["$each"]:
                    if item not in current:
                        current.append(item)
            elif value not in current:
                current.append(value)
            doc[key] = current

        doc_id = self._doc_id_from(filter, doc) or str(uuid.uuid4())
        await conn.execute(
            "INSERT OR REPLACE INTO kv_store (collection, doc_id, doc_data) VALUES (?, ?, ?)",
            [self._name, doc_id, json.dumps(doc)],
        )
        await conn.commit()

    async def count(self) -> int:
        """Return the number of documents in this collection (kv_store only)."""
        conn = await _ensure_conn()
        async with conn.execute(
            "SELECT COUNT(*) FROM kv_store WHERE collection = ?", [self._name]
        ) as cur:
            row = await cur.fetchone()
        return row[0] if row else 0


# ─────────────────────────────────────────────
# Database façade
# ─────────────────────────────────────────────

class SQLiteDatabase:
    """Database facade. db["collection_name"] returns a SQLiteCollection."""

    def __getitem__(self, collection_name: str) -> SQLiteCollection:
        return SQLiteCollection(collection_name)


_sqlite_db = SQLiteDatabase()


def get_sqlite_db(_name: str = None) -> SQLiteDatabase:
    """Returns the shared SQLite database adapter. The name parameter is ignored."""
    return _sqlite_db
