"""Where conversations and what they cost are kept.

This was a directory of timestamped JSON files, which was the right call for a
one-person tool and does not extend: listing meant reading every file, there was
no search, no pagination, no way to ask what a month of advice had cost, and the
filename was doing the job of a primary key.

It is SQLite now, through the standard library, with six tables:

    conversations          one row per saved conversation, with its total cost
    messages               the turns, in order, keyed to a conversation
    usage                  one row per API call, whether or not it was ever saved
    prices                 AWS list prices, cached from the bulk Price List
    conversation_services  which services each saved architecture recommended
    tags                   what somebody called a conversation

The third is the ledger. A call costs money the moment it is made, so it is
recorded then, rather than only if the conversation it belonged to happens to be
saved. That is what the daily spend ceiling in server.py counts, and what makes
"what did this month cost" a query rather than an afternoon.

The last two, and the `region`, `tier` and `compliance` columns beside them, are
what make a saved conversation findable (F8). All but `compliance` are derived
from the reply rather than entered, so `reindex()` can rebuild them.

JSON has not gone away: `export_json` returns exactly the document the old store
wrote, and `python migrate.py --export DIR` writes one file per conversation.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import threading
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from advisor import COUNT_FIELDS, MODEL, TOKEN_FIELDS, AdvisorError, merge_usage
from schema import Compliance

# Where the database lives. Read at call time rather than captured at import, so
# pointing this somewhere else -- a test, a second instance -- points the writes
# there too.
DB_PATH = Path("advisor.db")

# The shape of a stored reply, not of the database. Version 1 held a
# recommendation as Markdown; version 2 holds it as JSON validated against
# schema.Recommendation. migrate.py turns the first into the second.
SAVE_VERSION = 2

# Cost is a daily budget, and a day is a day where the person using this works.
# The machine's own timezone would do on a laptop in London and quietly would
# not in a container set to UTC.
UK = ZoneInfo("Europe/London")

# Longest a generated title runs before it is cut on a word boundary. The front
# end used to carry its own copy of this and its own copy of the rule; titles
# come from here now (A5).
TITLE_CUT = 34
MAX_TITLE = 80

SCHEMA = """
CREATE TABLE IF NOT EXISTS conversations (
    id           INTEGER PRIMARY KEY,
    saved_at     TEXT    NOT NULL,
    title        TEXT,                      -- NULL until someone names it
    mode         TEXT    NOT NULL,          -- advise | compare
    model        TEXT    NOT NULL,
    version      INTEGER NOT NULL,
    -- The running total the client reports when it saves. Kept here rather than
    -- summed from `usage`, because a call is not attached to a conversation:
    -- most calls are made before there is a conversation to attach them to.
    calls              INTEGER NOT NULL DEFAULT 0,
    input_tokens       INTEGER NOT NULL DEFAULT 0,
    output_tokens      INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens  INTEGER NOT NULL DEFAULT 0,
    cache_write_tokens INTEGER NOT NULL DEFAULT 0,
    searches           INTEGER NOT NULL DEFAULT 0,   -- web searches, billed per request
    cost_usd           REAL    NOT NULL DEFAULT 0,
    priced             INTEGER NOT NULL DEFAULT 1,
    -- Read off the architecture when it is saved, so that "every High-cost review
    -- in Ireland" is a query rather than a walk over every stored reply (F8).
    -- The compliance profile is not derived: it is what the user asked for, so a
    -- reopened conversation can put the selector back where they left it (F5).
    region     TEXT,
    tier       TEXT,
    compliance TEXT
);

CREATE TABLE IF NOT EXISTS messages (
    conversation_id INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    position        INTEGER NOT NULL,
    role            TEXT    NOT NULL,
    content         TEXT    NOT NULL,
    PRIMARY KEY (conversation_id, position)
);

CREATE TABLE IF NOT EXISTS usage (
    id                 INTEGER PRIMARY KEY,
    at                 TEXT    NOT NULL,
    day                TEXT    NOT NULL,   -- UK date, for the daily ceiling
    model              TEXT    NOT NULL,
    kind               TEXT    NOT NULL,   -- recommendation | comparison | revision | follow_up
    calls              INTEGER NOT NULL DEFAULT 0,
    input_tokens       INTEGER NOT NULL DEFAULT 0,
    output_tokens      INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens  INTEGER NOT NULL DEFAULT 0,
    cache_write_tokens INTEGER NOT NULL DEFAULT 0,
    searches           INTEGER NOT NULL DEFAULT 0,
    cost_usd           REAL    NOT NULL DEFAULT 0
);

-- AWS list prices, kept here rather than fetched each time because the file one
-- rate comes out of is 200 MB. `offer` is the price list file it came from, so a
-- stale service can be refetched without touching the others; `key` is whatever
-- identifies a rate within its meter -- an instance type, an instance type and
-- an engine, or nothing at all for a service with a single price.
CREATE TABLE IF NOT EXISTS prices (
    meter      TEXT NOT NULL,
    region     TEXT NOT NULL,
    key        TEXT NOT NULL,
    offer      TEXT NOT NULL,
    price_usd  REAL NOT NULL,
    fetched_at TEXT NOT NULL,
    PRIMARY KEY (meter, region, key)
);

-- Which services a saved architecture recommended, one row each, so a search can
-- ask for the conversations that mentioned RDS without reading their replies (F8).
-- Derived, like `region` and `tier`, and rebuilt by `migrate.py --reindex`.
CREATE TABLE IF NOT EXISTS conversation_services (
    conversation_id INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    name            TEXT    NOT NULL,
    PRIMARY KEY (conversation_id, name)
);

-- What somebody called this conversation, as opposed to anything read off it.
CREATE TABLE IF NOT EXISTS tags (
    conversation_id INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    tag             TEXT    NOT NULL,
    PRIMARY KEY (conversation_id, tag)
);

CREATE INDEX IF NOT EXISTS conversations_saved_at ON conversations(saved_at DESC);
CREATE INDEX IF NOT EXISTS usage_day ON usage(day);
CREATE INDEX IF NOT EXISTS prices_region ON prices(region);
CREATE INDEX IF NOT EXISTS prices_offer ON prices(offer, region);
CREATE INDEX IF NOT EXISTS conversation_services_name ON conversation_services(name);
CREATE INDEX IF NOT EXISTS tags_tag ON tags(tag);
"""

# The columns a usage total maps onto, in the order TOKEN_FIELDS names them.
USAGE_COLUMNS = (
    "calls",
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "searches",
)

# The SUM list every rollup of the ledger selects: one column per USAGE_COLUMNS
# entry, in that order, and then the cost. COALESCE because SUM over no rows is
# null, and a quiet month should report zero rather than blank.
_USAGE_SUMS = ", ".join(
    (
        *(f"COALESCE(SUM({column}), 0) AS {column}" for column in USAGE_COLUMNS),
        "COALESCE(SUM(cost_usd), 0.0) AS cost_usd",
    )
)

_ready: set[Path] = set()
_ready_lock = threading.Lock()


# --------------------------------------------------------------------------- #
# Connections
# --------------------------------------------------------------------------- #


@contextmanager
def connect(write: bool = False) -> Iterator[sqlite3.Connection]:
    """Open the database for one operation, and close it again.

    A connection per operation rather than one shared around: sqlite3 objects
    belong to the thread that made them, the web app serves on several, and
    opening a local file is measured in microseconds. WAL is what lets a read
    run while a write is in progress.
    """
    path = DB_PATH
    if path not in _ready:
        _prepare(path)

    connection = sqlite3.connect(path, timeout=10.0)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    try:
        yield connection
        if write:
            connection.commit()
    finally:
        connection.close()


# Columns added to SCHEMA after the first release. CREATE TABLE IF NOT EXISTS
# does nothing to a table that already exists, so a database written before one
# of these was added needs it put on: `searches` arrived with web search (F2),
# where a call costs money that is not a token.
ADDED_COLUMNS: dict[str, dict[str, str]] = {
    "conversations": {
        "searches": "INTEGER NOT NULL DEFAULT 0",
        # F5 and F8. Nullable rather than defaulted: an existing row genuinely
        # does not know its region yet, and "" would claim it had none. What
        # fills them in for old rows is `migrate.py --reindex`.
        "region": "TEXT",
        "tier": "TEXT",
        "compliance": "TEXT",
    },
    "usage": {"searches": "INTEGER NOT NULL DEFAULT 0"},
}


def _add_missing_columns(connection: sqlite3.Connection) -> None:
    """Bring an older database up to the current schema. Safe to run every time."""
    for table, columns in ADDED_COLUMNS.items():
        present = {
            row["name"] for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
        }
        for column, definition in columns.items():
            if column not in present:
                connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def _prepare(path: Path) -> None:
    """Create the schema, once per database file per process."""
    with _ready_lock:
        if path in _ready:
            return
        if path.parent != Path():
            path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(path, timeout=10.0)
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(SCHEMA)
            _add_missing_columns(connection)
            connection.commit()
        finally:
            connection.close()
        _ready.add(path)


def reset_for_tests() -> None:
    """Forget which files have been prepared. Only a test should need this."""
    with _ready_lock:
        _ready.clear()


# --------------------------------------------------------------------------- #
# Titles (A5)
# --------------------------------------------------------------------------- #


def title_from(text: str) -> str:
    """Name a conversation after the question that started it.

    Cut on a word boundary where there is one worth cutting on, so a title reads
    as a phrase rather than as a truncated word. This rule used to exist twice,
    here and in static/app.js, kept in step by a comment.
    """
    collapsed = " ".join(str(text or "").split())
    if len(collapsed) <= TITLE_CUT:
        return collapsed
    cut = collapsed[:TITLE_CUT]
    boundary = cut.rfind(" ")
    return f"{cut[: boundary if boundary > 16 else TITLE_CUT]}…"


def clean_title(title: str | None) -> str | None:
    """A title as it is stored: trimmed, capped, or nothing at all."""
    cleaned = str(title or "").strip()[:MAX_TITLE]
    return cleaned or None


# --------------------------------------------------------------------------- #
# Reading and writing conversations
# --------------------------------------------------------------------------- #


def _usage_values(usage: Mapping[str, Any] | None) -> tuple:
    """A usage total as the columns it is stored in."""
    total = merge_usage(dict(usage) if usage else None)
    return (
        *(int(total[field]) for field in COUNT_FIELDS),
        float(total["costUsd"]),
        int(bool(total["priced"])),
    )


def _usage_of(row: sqlite3.Row) -> dict[str, Any] | None:
    """Read a usage total back off a row, or None if nothing was ever spent."""
    if not row["calls"]:
        return None
    total = {field: row[column] for field, column in zip(COUNT_FIELDS, USAGE_COLUMNS, strict=True)}
    return {**total, "costUsd": row["cost_usd"], "priced": bool(row["priced"])}


def _display_title(row: sqlite3.Row, first_question: str) -> str:
    return row["title"] or title_from(first_question) or "Untitled conversation"


# How many service names one conversation contributes to the facet table. A
# comparison of four architectures is the widest real case; the cap is here so a
# malformed reply cannot write a row per hallucinated service.
MAX_FACET_SERVICES = 60


def facets_of(messages: Sequence[Mapping[str, Any]]) -> tuple[str, str, list[str]]:
    """The region, cost tier and service names to file a conversation under (F8).

    Read off the last assistant turn that parses as a recommendation, because
    that is the architecture as it now stands: a revision supersedes what it
    revised, and a thread of follow-ups after it changes nothing.

    Called from both save() and import_json(). They are separate insert paths --
    import does not go through save -- and a facet derived in only one of them
    would leave migrated conversations quietly unsearchable.

    Everything here degrades to empty rather than raising. A conversation that
    cannot be read is still a conversation worth keeping.
    """
    region = ""
    tier = ""
    services: list[str] = []

    for message in reversed(list(messages)):
        if message.get("role") != "assistant":
            continue
        try:
            data = json.loads(str(message.get("content") or ""))
        except (TypeError, ValueError):
            continue
        if not isinstance(data, dict) or "services" not in data:
            continue

        region = str(data.get("region") or "")
        cost = data.get("cost")
        if isinstance(cost, Mapping):
            tier = str(cost.get("tier") or "")
        for service in data.get("services") or []:
            name = str(service.get("name") or "").strip() if isinstance(service, Mapping) else ""
            if name and name not in services:
                services.append(name)
        break

    return region, tier, services[:MAX_FACET_SERVICES]


def _write_facets(
    connection: sqlite3.Connection,
    conversation_id: int,
    messages: Sequence[Mapping[str, Any]],
    compliance: str | None = None,
    keep_compliance: bool = False,
) -> None:
    """Put a conversation's derived facets on it, replacing whatever was there.

    `keep_compliance` leaves the stored profile alone. That is what reindex()
    needs: the region, the tier and the services are all readable back off the
    reply, and the compliance profile is not -- it is what the user asked for, so
    re-deriving it would blank something nothing can recover. Everything else
    here is derived and safe to rebuild.
    """
    region, tier, services = facets_of(messages)
    if keep_compliance:
        connection.execute(
            "UPDATE conversations SET region = ?, tier = ? WHERE id = ?",
            (region or None, tier or None, conversation_id),
        )
    else:
        connection.execute(
            "UPDATE conversations SET region = ?, tier = ?, compliance = ? WHERE id = ?",
            (region or None, tier or None, clean_compliance(compliance), conversation_id),
        )
    connection.execute(
        "DELETE FROM conversation_services WHERE conversation_id = ?", (conversation_id,)
    )
    connection.executemany(
        "INSERT INTO conversation_services (conversation_id, name) VALUES (?, ?)",
        [(conversation_id, name) for name in services],
    )


def clean_compliance(value: Any) -> str | None:
    """A compliance profile as it is stored, or None if none was chosen.

    NONE is stored as nothing rather than as the string "none": the column
    answers "what did they pick", and "they did not" is what NULL is for.
    """
    text = str(value or "").strip()
    if not text or text == Compliance.NONE:
        return None
    return text if text in set(Compliance) else None


def save(
    messages: Sequence[Mapping[str, Any]],
    mode: str = "advise",
    title: str | None = None,
    usage: Mapping[str, Any] | None = None,
    model: str = MODEL,
    compliance: str | None = None,
) -> int:
    """Store a conversation and return its id.

    Nothing here can collide the way two saves inside one second used to: the id
    comes from the database, and the title is a column rather than a filename.

    `compliance` is what the user asked for, and the region, tier and services
    are read off the reply. See facets_of().
    """
    if not messages:
        raise AdvisorError("There is nothing to save yet.", kind="empty")

    now = datetime.now(UK)
    with connect(write=True) as connection:
        cursor = connection.execute(
            "INSERT INTO conversations "
            "(saved_at, title, mode, model, version, calls, input_tokens, output_tokens, "
            " cache_read_tokens, cache_write_tokens, searches, cost_usd, priced) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (now.isoformat(), clean_title(title), mode, model, SAVE_VERSION, *_usage_values(usage)),
        )
        conversation_id = int(cursor.lastrowid or 0)
        connection.executemany(
            "INSERT INTO messages (conversation_id, position, role, content) VALUES (?, ?, ?, ?)",
            [
                (conversation_id, position, message["role"], message["content"])
                for position, message in enumerate(messages)
            ],
        )
        _write_facets(connection, conversation_id, messages, compliance)
    return conversation_id


# The columns and correlated subqueries every listed conversation carries. One
# string, because listing() and search() differ only in what they filter on, and
# two copies of this would drift into two different row shapes.
#
# Split at the FROM so search() can add one more column between them. Anything
# that wants the plain row shape uses _LISTING_SELECT and does not have to know.
_LISTING_COLUMNS = (
    "SELECT c.*, ("
    "  SELECT m.content FROM messages m"
    "   WHERE m.conversation_id = c.id AND m.role = 'user'"
    "   ORDER BY m.position LIMIT 1"
    " ) AS first_question, ("
    "  SELECT COUNT(*) FROM messages m WHERE m.conversation_id = c.id"
    " ) AS message_count, ("
    "  SELECT GROUP_CONCAT(t.tag) FROM ("
    "    SELECT tag FROM tags WHERE conversation_id = c.id ORDER BY tag"
    "  ) t"
    " ) AS tag_list"
)

_LISTING_FROM = " FROM conversations c"

_LISTING_SELECT = _LISTING_COLUMNS + _LISTING_FROM

# The first message a text search matched, carried on the row rather than fetched
# per result. This used to be one extra query for every conversation found -- up
# to MAX_SEARCH_RESULTS of them on each debounced keystroke -- for a string that
# the row was already being read to produce.
_MATCH_COLUMN = (
    ", ("
    "  SELECT m.content FROM messages m"
    "   WHERE m.conversation_id = c.id AND m.content LIKE ? ESCAPE '\\'"
    "   ORDER BY m.position LIMIT 1"
    " ) AS match_content"
)

_LISTING_ORDER = " ORDER BY c.saved_at DESC, c.id DESC"


def _listed(row: sqlite3.Row) -> dict[str, Any]:
    """One row of the saved list. Additive: the browser reads what it knows."""
    return {
        "id": row["id"],
        "title": _display_title(row, row["first_question"] or ""),
        "titled": bool(row["title"]),
        "mode": row["mode"],
        "model": row["model"],
        "savedAt": row["saved_at"],
        "messageCount": row["message_count"],
        "usage": _usage_of(row),
        "region": row["region"] or "",
        "tier": row["tier"] or "",
        "compliance": row["compliance"] or "",
        "tags": sorted(set((row["tag_list"] or "").split(","))) if row["tag_list"] else [],
    }


def listing() -> list[dict[str, Any]]:
    """Every saved conversation, newest first, without loading the messages.

    One query, where the JSON store read and parsed every file on disk. The
    first question comes along for the ride so an unnamed conversation still has
    something to show.
    """
    with connect() as connection:
        rows = connection.execute(_LISTING_SELECT + _LISTING_ORDER).fetchall()
    return [_listed(row) for row in rows]


# How much of a matching message is shown under a search result.
SNIPPET_CHARS = 160

MAX_SEARCH_RESULTS = 200


def _like(text: str) -> str:
    """A search box's contents as a LIKE pattern that means what was typed.

    A search box is not a query language: somebody looking for "100%" wants the
    two characters, not "anything at all". Every query here pairs this with
    ESCAPE '\\', and the backslash has to be doubled first or it would escape
    whatever followed it.
    """
    escaped = text.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _snippet(content: str, needle: str) -> str:
    """The words around a match, so a result says why it matched."""
    text = " ".join(content.split())
    at = text.lower().find(needle.lower())
    if at < 0:
        return text[:SNIPPET_CHARS]
    start = max(0, at - SNIPPET_CHARS // 3)
    piece = text[start : start + SNIPPET_CHARS]
    return ("…" if start else "") + piece + ("…" if start + SNIPPET_CHARS < len(text) else "")


def search(
    text: str = "",
    tier: str = "",
    service: str = "",
    tag: str = "",
    region: str = "",
) -> list[dict[str, Any]]:
    """Saved conversations matching any combination of the filters (F8).

    LIKE rather than FTS5. FTS5 would rank better and is not obviously
    available in every build this has to run on; more to the point its index
    starts empty, which would turn the backfill from a convenience into a
    release step. At a store of this size the scan is not what anyone waits for.

    Every filter is optional and they are combined with AND, which is what a
    person filling in two boxes expects. An empty call is listing().
    """
    clauses: list[str] = []
    filters: list[Any] = []

    needle = _like(text) if text.strip() else ""
    if needle:
        clauses.append(
            "(c.title LIKE ? ESCAPE '\\' OR EXISTS ("
            "  SELECT 1 FROM messages m"
            "   WHERE m.conversation_id = c.id AND m.content LIKE ? ESCAPE '\\'"
            "))"
        )
        filters += [needle, needle]
    if tier.strip():
        clauses.append("c.tier = ?")
        filters.append(tier.strip())
    if region.strip():
        clauses.append("c.region = ?")
        filters.append(region.strip())
    if service.strip():
        clauses.append(
            "EXISTS (SELECT 1 FROM conversation_services s"
            "         WHERE s.conversation_id = c.id AND s.name LIKE ? ESCAPE '\\')"
        )
        filters.append(_like(service))
    if tag.strip():
        clauses.append("EXISTS (SELECT 1 FROM tags t WHERE t.conversation_id = c.id AND t.tag = ?)")
        filters.append(tag.strip())

    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    columns = _LISTING_COLUMNS + (_MATCH_COLUMN if needle else "")
    query = f"{columns}{_LISTING_FROM}{where}{_LISTING_ORDER} LIMIT {MAX_SEARCH_RESULTS}"
    # SQLite binds `?` in the order they appear in the statement, and the match
    # column sits in the select list ahead of every filter, so its needle goes
    # first. Getting this the wrong way round searches for a cost tier.
    params = ([needle] if needle else []) + filters

    with connect() as connection:
        rows = connection.execute(query, params).fetchall()

    results = [_listed(row) for row in rows]
    if needle:
        for row, result in zip(rows, results, strict=True):
            matched = row["match_content"]
            result["snippet"] = _snippet(matched, text.strip()) if matched else ""
    return results


def all_tags() -> list[str]:
    """Every tag in use, for offering them as filters."""
    with connect() as connection:
        rows = connection.execute("SELECT DISTINCT tag FROM tags ORDER BY tag").fetchall()
    return [row["tag"] for row in rows]


MAX_TAG_CHARS = 30


def clean_tag(tag: Any) -> str:
    """A tag as it is stored: trimmed, collapsed, lowercased, length-capped.

    Lowercased so "NHS" and "nhs" are one tag rather than two that look alike in
    a list and filter differently.
    """
    return " ".join(str(tag or "").split()).lower()[:MAX_TAG_CHARS]


def _exists(connection: sqlite3.Connection, conversation_id: int) -> bool:
    """Whether there is a conversation to hang a tag on."""
    found = connection.execute(
        "SELECT 1 FROM conversations WHERE id = ?", (conversation_id,)
    ).fetchone()
    return found is not None


def add_tag(conversation_id: int, tag: Any) -> list[str] | None:
    """Tag a conversation. Returns its tags, or None if there is no such id."""
    cleaned = clean_tag(tag)
    if not cleaned:
        raise AdvisorError("A tag needs some text in it.", kind="empty")

    with connect(write=True) as connection:
        if not _exists(connection, conversation_id):
            return None
        connection.execute(
            "INSERT OR IGNORE INTO tags (conversation_id, tag) VALUES (?, ?)",
            (conversation_id, cleaned),
        )
    return tags_for(conversation_id)


def remove_tag(conversation_id: int, tag: Any) -> list[str] | None:
    """Untag a conversation. Returns its remaining tags, or None if no such id."""
    with connect(write=True) as connection:
        if not _exists(connection, conversation_id):
            return None
        connection.execute(
            "DELETE FROM tags WHERE conversation_id = ? AND tag = ?",
            (conversation_id, clean_tag(tag)),
        )
    return tags_for(conversation_id)


def tags_for(conversation_id: int) -> list[str]:
    """One conversation's tags, in the order they are shown."""
    with connect() as connection:
        rows = connection.execute(
            "SELECT tag FROM tags WHERE conversation_id = ? ORDER BY tag", (conversation_id,)
        ).fetchall()
    return [row["tag"] for row in rows]


def reindex() -> int:
    """Re-derive every conversation's facets. Returns how many were rebuilt.

    For conversations saved before the facet columns existed, and as the repair
    for anything that drifts. Reads each conversation's messages and puts the
    region, tier and services back on it; the compliance profile is left alone,
    because it was never in the reply to be read off.

    Goes through _write_facets like save() and import_json() do, rather than
    repeating its three statements. Three copies of the facet-writing SQL is how
    one of them ends up deriving something the others do not.
    """
    with connect(write=True) as connection:
        ids = [
            row["id"]
            for row in connection.execute("SELECT id FROM conversations ORDER BY id").fetchall()
        ]
        for conversation_id in ids:
            messages = connection.execute(
                "SELECT role, content FROM messages WHERE conversation_id = ? ORDER BY position",
                (conversation_id,),
            ).fetchall()
            _write_facets(
                connection,
                conversation_id,
                [{"role": row["role"], "content": row["content"]} for row in messages],
                keep_compliance=True,
            )
    return len(ids)


def read(conversation_id: int) -> dict[str, Any] | None:
    """One conversation and its messages, or None if there is no such id."""
    with connect() as connection:
        row = connection.execute(
            "SELECT * FROM conversations WHERE id = ?", (conversation_id,)
        ).fetchone()
        if row is None:
            return None
        messages = connection.execute(
            "SELECT role, content FROM messages WHERE conversation_id = ? ORDER BY position",
            (conversation_id,),
        ).fetchall()

    turns = [{"role": message["role"], "content": message["content"]} for message in messages]
    first_question = next((turn["content"] for turn in turns if turn["role"] == "user"), "")
    return {
        "id": row["id"],
        "title": _display_title(row, first_question),
        "titled": bool(row["title"]),
        "mode": row["mode"],
        "model": row["model"],
        "version": row["version"],
        "savedAt": row["saved_at"],
        "messages": turns,
        "usage": _usage_of(row),
        "region": row["region"] or "",
        "tier": row["tier"] or "",
        # So reopening a conversation puts the selector back where it was, rather
        # than showing a review built for NHS DSPT as though nobody had asked.
        "compliance": row["compliance"] or "",
        "tags": tags_for(conversation_id),
    }


def rename(conversation_id: int, title: Any) -> str | None:
    """Give a conversation a name. Returns the stored name, or None if unknown."""
    cleaned = clean_title(title)
    if not cleaned:
        raise AdvisorError("A conversation needs a name.", kind="empty")

    with connect(write=True) as connection:
        changed = connection.execute(
            "UPDATE conversations SET title = ? WHERE id = ?", (cleaned, conversation_id)
        ).rowcount
    return cleaned if changed else None


def delete(conversation_id: int) -> bool:
    """Remove a conversation and its messages. False if it was not there."""
    with connect(write=True) as connection:
        changed = connection.execute(
            "DELETE FROM conversations WHERE id = ?", (conversation_id,)
        ).rowcount
    return bool(changed)


def export_json(conversation_id: int) -> dict[str, Any] | None:
    """One conversation as the document the old JSON store wrote.

    Byte for byte the same shape, so anything that read those files still can,
    and so a conversation can leave here without leaving the format behind.
    """
    conversation = read(conversation_id)
    if conversation is None:
        return None

    payload: dict[str, Any] = {
        "timestamp": conversation["savedAt"],
        "version": conversation["version"],
        "model": conversation["model"],
        "mode": conversation["mode"],
        "messages": conversation["messages"],
    }
    if conversation["titled"]:
        payload["title"] = conversation["title"]
    if conversation["usage"]:
        payload["usage"] = conversation["usage"]
    # Only when there is something to say, so a conversation from before F5 comes
    # out byte for byte the document it went in as. The region and the tier are
    # left out on purpose: both are read off the messages, so writing them would
    # be duplicating what import_json is about to derive again anyway.
    if conversation["compliance"]:
        payload["compliance"] = conversation["compliance"]
    if conversation["tags"]:
        payload["tags"] = conversation["tags"]
    return payload


def export_all(directory: Path) -> list[Path]:
    """Write every conversation out as JSON, one file each."""
    directory.mkdir(parents=True, exist_ok=True)
    written = []
    for item in listing():
        payload = export_json(item["id"])
        if payload is None:  # deleted between the listing and now
            continue
        stamp = payload["timestamp"][:19].replace("-", "").replace(":", "").replace("T", "_")
        path = directory / f"conversation_{stamp}-{item['id']}.json"
        path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        written.append(path)
    return written


def import_json(payload: Mapping[str, Any]) -> int:
    """Take in one of the documents export_json writes. Used by migrate.py."""
    messages = [
        {"role": message["role"], "content": str(message.get("content") or "")}
        for message in payload.get("messages") or []
        if isinstance(message, dict) and message.get("role") in ("user", "assistant")
    ]
    if not messages:
        raise AdvisorError("That conversation has no messages.", kind="empty")

    saved_at = str(payload.get("timestamp") or datetime.now(UK).isoformat())
    usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else None

    with connect(write=True) as connection:
        cursor = connection.execute(
            "INSERT INTO conversations "
            "(saved_at, title, mode, model, version, calls, input_tokens, output_tokens, "
            " cache_read_tokens, cache_write_tokens, searches, cost_usd, priced) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                saved_at,
                clean_title(payload.get("title")),
                "compare" if payload.get("mode") == "compare" else "advise",
                str(payload.get("model") or MODEL),
                int(payload.get("version") or 1),
                *_usage_values(usage),
            ),
        )
        conversation_id = int(cursor.lastrowid or 0)
        connection.executemany(
            "INSERT INTO messages (conversation_id, position, role, content) VALUES (?, ?, ?, ?)",
            [
                (conversation_id, position, message["role"], message["content"])
                for position, message in enumerate(messages)
            ],
        )
        _write_facets(connection, conversation_id, messages, payload.get("compliance"))
        connection.executemany(
            "INSERT OR IGNORE INTO tags (conversation_id, tag) VALUES (?, ?)",
            [
                (conversation_id, clean_tag(tag))
                for tag in payload.get("tags") or []
                if clean_tag(tag)
            ],
        )
    return conversation_id


def set_version(conversation_id: int, version: int) -> None:
    """Record which reply format a conversation is stored in."""
    with connect(write=True) as connection:
        connection.execute(
            "UPDATE conversations SET version = ? WHERE id = ?", (version, conversation_id)
        )


def replace_message(conversation_id: int, position: int, content: str) -> None:
    """Rewrite one turn in place. Used by the migration, and by nothing else."""
    with connect(write=True) as connection:
        connection.execute(
            "UPDATE messages SET content = ? WHERE conversation_id = ? AND position = ?",
            (content, conversation_id, position),
        )


# --------------------------------------------------------------------------- #
# Cached AWS prices
# --------------------------------------------------------------------------- #


def save_prices(offer: str, region: str, prices: Mapping[tuple[str, str], float]) -> None:
    """Replace everything cached from one price list file.

    Replaced rather than merged: a rate that has gone from AWS's file should go
    from here too, instead of lingering as the answer to a question AWS has
    stopped answering.
    """
    now = datetime.now(UK).isoformat()
    with connect(write=True) as connection:
        connection.execute("DELETE FROM prices WHERE offer = ? AND region = ?", (offer, region))
        connection.executemany(
            "INSERT OR REPLACE INTO prices (meter, region, key, offer, price_usd, fetched_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            [(meter, region, key, offer, price, now) for (meter, key), price in prices.items()],
        )


def prices_for(region: str) -> dict[tuple[str, str], float]:
    """Every cached price for a region, keyed the way pricing.py looks them up."""
    with connect() as connection:
        rows = connection.execute(
            "SELECT meter, key, price_usd FROM prices WHERE region = ?", (region,)
        ).fetchall()
    return {(row["meter"], row["key"]): row["price_usd"] for row in rows}


def prices_fetched_at(offer: str, region: str) -> datetime | None:
    """When one price list file was last pulled, or None if it never was."""
    with connect() as connection:
        row = connection.execute(
            "SELECT MAX(fetched_at) AS at FROM prices WHERE offer = ? AND region = ?",
            (offer, region),
        ).fetchone()
    if row is None or not row["at"]:
        return None
    try:
        return datetime.fromisoformat(row["at"])
    except ValueError:
        return None


def prices_are_fresh(offer: str, region: str, max_age: timedelta) -> bool:
    """True if the prices for one file are recent enough to use as they are."""
    fetched = prices_fetched_at(offer, region)
    return fetched is not None and datetime.now(UK) - fetched < max_age


def forget_prices(region: str | None = None) -> int:
    """Drop cached prices, so the next estimate fetches them again."""
    with connect(write=True) as connection:
        if region is None:
            return connection.execute("DELETE FROM prices").rowcount
        return connection.execute("DELETE FROM prices WHERE region = ?", (region,)).rowcount


# --------------------------------------------------------------------------- #
# The spend ledger
# --------------------------------------------------------------------------- #


def record_call(
    usage: Mapping[str, Any] | None, kind: str = "follow_up", model: str = MODEL
) -> None:
    """Write down what one API call cost, as soon as it is known.

    Called for failures as well as successes: a reply cut off at the token cap
    is billed like any other, and a ceiling that ignored those would not be one.
    """
    if not usage or not usage.get("calls"):
        return

    now = datetime.now(UK)
    total = merge_usage(dict(usage))
    with connect(write=True) as connection:
        connection.execute(
            "INSERT INTO usage (at, day, model, kind, calls, input_tokens, output_tokens, "
            " cache_read_tokens, cache_write_tokens, searches, cost_usd) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                now.isoformat(),
                now.date().isoformat(),
                model,
                kind,
                *(int(total[field]) for field in COUNT_FIELDS),
                float(total["costUsd"]),
            ),
        )


def _totals_of(row: sqlite3.Row) -> dict[str, Any]:
    """One aggregated row of the ledger, in the shape a usage total has.

    `tokens` is what the daily ceiling counts, which is neither everything nor
    the token columns alone: `calls` is not a token, and a web search is billed
    per request rather than per token (F2). Derived here rather than at each
    caller so the ceiling and the report cannot disagree about the arithmetic.

    `priced` is true because `usage` has no column for it: a call with no rate
    for its model is still a call, and it was recorded either way. It is on every
    row rather than only on the report, because that is where the front end reads
    it -- a row without one renders as an unpriced model.
    """
    total = {field: row[column] for field, column in zip(COUNT_FIELDS, USAGE_COLUMNS, strict=True)}
    tokens = sum(total[field] for field in TOKEN_FIELDS if field != "calls")
    return {**total, "costUsd": row["cost_usd"], "tokens": tokens, "priced": True}


def _added(*totals: Mapping[str, Any]) -> dict[str, Any]:
    """Add aggregated rows together, re-deriving the tokens as _totals_of does.

    Called with nothing, this is the zero total, which is what a month with no
    calls in it should report.
    """
    summed = {field: sum(int(total[field]) for total in totals) for field in COUNT_FIELDS}
    tokens = sum(summed[field] for field in TOKEN_FIELDS if field != "calls")
    cost = sum(float(total["costUsd"]) for total in totals)
    return {**summed, "costUsd": cost, "tokens": tokens, "priced": True}


def spent_on(day: date | None = None) -> dict[str, Any]:
    """What has been spent on a given day, UK time. Today by default."""
    when = (day or datetime.now(UK).date()).isoformat()
    with connect() as connection:
        row = connection.execute(
            f"SELECT {_USAGE_SUMS} FROM usage WHERE day = ?",
            (when,),
        ).fetchone()

    return {**_totals_of(row), "day": when}


# --------------------------------------------------------------------------- #
# Reading the ledger back (F14)
# --------------------------------------------------------------------------- #
#
# The ledger has recorded every call since the first release and nothing has ever
# read it except the daily ceiling, which only ever looks at today. That is a
# month of billing history nobody could see, and a ceiling that announced itself
# only by refusing a request.


def daily_ceiling() -> int:
    """Tokens, all kinds counted together, per UK day across the CLI and the web app.

    500,000 is roughly 120 recommendations, which is a busy week rather than a
    busy day. Set ADVISOR_DAILY_TOKENS=0 to turn the ceiling off.

    Beside the ledger it bounds rather than in server.py, because the report has
    to name the number the web app enforces and two readings of one environment
    variable is one too many.
    """
    return int(os.environ.get("ADVISOR_DAILY_TOKENS") or 500_000)


# A month as the report addresses one: four digits, a hyphen, and a real month.
# Checked rather than trusted because a query parameter reaches this.
MONTH_PATTERN = re.compile(r"\A\d{4}-(0[1-9]|1[0-2])\Z")


def _month_bounds(month: str) -> tuple[str, str]:
    """The first day of a month and the first day of the next, as ISO dates.

    A half-open range on `day` rather than substr(day, 1, 7), so the usage_day
    index is what answers the query, and so the turn of the month needs no
    special case.
    """
    if not MONTH_PATTERN.match(month):
        raise ValueError(f"{month!r} is not a month written as YYYY-MM")

    year, number = (int(part) for part in month.split("-"))
    # December rolls to January of the next year; every other month is the next
    # number along. // and % do both without a branch.
    return date(year, number, 1).isoformat(), date(
        year + number // 12, number % 12 + 1, 1
    ).isoformat()


def spend_report(month: str = "") -> dict[str, Any]:
    """What a month of advice cost, by day and by kind. This month by default.

    One GROUP BY answers both rollups: the per-day-per-kind rows are the query,
    and the per-kind figures and the month's total are folded out of them rather
    than asked for again.

    Deliberately not grouped by model. No caller passes `model` to record_call,
    so every row says MODEL whatever actually served the call, and a split on
    that column would be confident and wrong.

    Raises ValueError on a month it cannot read, which both callers turn into
    their own refusal.
    """
    this_month = month or datetime.now(UK).strftime("%Y-%m")
    start, after = _month_bounds(this_month)

    with connect() as connection:
        rows = connection.execute(
            f"SELECT day, kind, {_USAGE_SUMS} FROM usage"
            " WHERE day >= ? AND day < ? GROUP BY day, kind ORDER BY day, kind",
            (start, after),
        ).fetchall()

    days = [{"day": row["day"], "kind": row["kind"], **_totals_of(row)} for row in rows]

    by_kind: dict[str, list[dict[str, Any]]] = {}
    for entry in days:
        by_kind.setdefault(entry["kind"], []).append(entry)

    return {
        "month": this_month,
        "days": days,
        "kinds": [{"kind": kind, **_added(*rows)} for kind, rows in sorted(by_kind.items())],
        "totals": _added(*days),
        # Today's figure comes from the same place the ceiling reads it, rather
        # than being picked out of `days`: the report may be of another month.
        "today": spent_on(),
    }


# --------------------------------------------------------------------------- #
# The command line
# --------------------------------------------------------------------------- #

REPORT_DESCRIPTION = """What the advisor has cost, read back out of the ledger.

Every API call is written down as it is made, by the CLI and the web app alike.
This reports a month of that: by kind, by day, and against the daily ceiling the
web app enforces. Figures are list price in US dollars, before any discount.
"""


def _report_lines(report: Mapping[str, Any], ceiling: int) -> list[str]:
    """The report as a list of lines, so a test can read it without a terminal."""

    def head(title: str) -> str:
        return f"{title:<22}{'calls':>7}{'tokens':>12}{'cost':>12}"

    def row(name: str, total: Mapping[str, Any]) -> str:
        cost = f"${total['costUsd']:,.4f}"
        return f"  {name:<20}{total['calls']:>7}{total['tokens']:>12,}{cost:>12}"

    lines = [f"Spend for {report['month']}, list price in US dollars", "", head("By kind")]
    lines += [row(entry["kind"], entry) for entry in report["kinds"]]
    if not report["kinds"]:
        lines.append("  nothing was spent this month")
    lines.append(row("Total", report["totals"]))

    # Only if there is one: an empty month should not print a header over nothing.
    if report["days"]:
        lines += ["", head("By day")]
        for when in sorted({entry["day"] for entry in report["days"]}):
            lines.append(row(when, _added(*(e for e in report["days"] if e["day"] == when))))

    today = report["today"]
    spent = f"{today['tokens']:,} tokens across {today['calls']} calls, ${today['costUsd']:,.4f}"
    lines.append("")
    if ceiling:
        lines.append(
            f"Today: {spent}, against a ceiling of {ceiling:,} tokens "
            f"({today['tokens'] * 100 // ceiling}% used). It resets at midnight, UK time."
        )
    else:
        lines.append(f"Today: {spent}. No ceiling is set (ADVISOR_DAILY_TOKENS=0).")
    return lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python store.py",
        description=REPORT_DESCRIPTION,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--report", action="store_true", help="print what a month of advice cost")
    parser.add_argument(
        "--month",
        metavar="YYYY-MM",
        default="",
        help="which month to report on (this month by default)",
    )
    args = parser.parse_args(argv)

    # Nothing to do is not a failure: the store is a library first, and running
    # it with no arguments should say what it can do rather than touch the disk.
    if not args.report:
        parser.print_help()
        return 0

    try:
        report = spend_report(args.month)
    except ValueError as e:
        print(e)
        print("Months are written YYYY-MM, as in 2026-08.")
        return 1
    except sqlite3.Error as e:
        print(f"The ledger in {DB_PATH} could not be read: {e}")
        return 1

    for line in _report_lines(report, daily_ceiling()):
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
