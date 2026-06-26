"""SQLite + FTS5 index over all Claude Code transcripts.

Builds an incremental local index (``~/.claude/ccsess.db``) used by ``search`` and
``stats``. Re-running ``build`` only re-parses files whose size or mtime changed.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Optional

from .core import PROJECTS_DIR, config_dir, iter_lines, read_session

DB_PATH = config_dir() / "ccsess.db"

# Rough list-price approximation, USD per million tokens (input, output).
# Used only for a clearly-labelled cost estimate in `stats`.
_PRICES = {
    "opus": (15.0, 75.0),
    "sonnet": (3.0, 15.0),
    "haiku": (0.80, 4.0),
}


def _price_for(model: str) -> tuple[float, float]:
    m = (model or "").lower()
    for key, price in _PRICES.items():
        if key in m:
            return price
    return (0.0, 0.0)


def connect(db_path: Optional[Path] = None) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path or DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS files (
            path TEXT PRIMARY KEY,
            size INTEGER,
            mtime REAL
        );
        CREATE TABLE IF NOT EXISTS sessions (
            id TEXT PRIMARY KEY,
            path TEXT,
            slug_dir TEXT,
            cwd TEXT,
            project TEXT,
            title TEXT,
            git_branch TEXT,
            version TEXT,
            message_count INTEGER,
            first_ts TEXT,
            last_ts TEXT,
            size INTEGER,
            orphaned INTEGER,
            input_tokens INTEGER,
            output_tokens INTEGER,
            cache_read_tokens INTEGER,
            cache_write_tokens INTEGER,
            model TEXT,
            cost_usd REAL
        );
        CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
            text, session_id UNINDEXED, role UNINDEXED, ts UNINDEXED,
            tokenize = 'porter unicode61'
        );
        """
    )


# Tool outputs are ~72% of the index and most of the bulk is a few giant dumps
# (logs, file reads). Cap each tool result so big outputs are still findable by their
# first ~2KB without bloating the index. Prose is never capped.
_TOOL_RESULT_CAP = 2000


def _extract_text(content) -> tuple[str, Optional[str]]:
    """Return (joined text, first tool name) from a message ``content`` value."""
    if isinstance(content, str):
        return content, None
    if not isinstance(content, list):
        return "", None
    parts: list[str] = []
    tool: Optional[str] = None
    for block in content:
        if not isinstance(block, dict):
            continue
        bt = block.get("type")
        if bt in ("text", "thinking"):
            parts.append(block.get("text") or block.get("thinking") or "")
        elif bt == "tool_use":
            tool = tool or block.get("name")
            parts.append(f"[tool_use:{block.get('name')}]")
        elif bt == "tool_result":
            c = block.get("content")
            if isinstance(c, str):
                s = c
            elif isinstance(c, list):
                s = "\n".join(b.get("text", "") for b in c if isinstance(b, dict))
            else:
                s = ""
            if s:
                parts.append(s[:_TOOL_RESULT_CAP])
    return "\n".join(p for p in parts if p), tool


def _index_one(conn: sqlite3.Connection, jsonl: Path) -> None:
    sess = read_session(jsonl)
    project = sess.project_name
    in_tok = out_tok = cache_r = cache_w = 0
    model = None
    fts_rows: list[tuple] = []
    for d in iter_lines(jsonl):
        t = d.get("type")
        if t not in ("user", "assistant"):
            continue
        msg = d.get("message") or {}
        text, _tool = _extract_text(msg.get("content"))
        ts = d.get("timestamp")
        if text.strip():
            fts_rows.append((text, sess.id, t, ts or ""))
        if t == "assistant":
            usage = msg.get("usage") or {}
            in_tok += usage.get("input_tokens", 0) or 0
            out_tok += usage.get("output_tokens", 0) or 0
            cache_r += usage.get("cache_read_input_tokens", 0) or 0
            cache_w += usage.get("cache_creation_input_tokens", 0) or 0
            model = model or msg.get("model")

    pin, pout = _price_for(model or "")
    # cache read ~10% of input price, cache write ~25% premium (rough).
    cost = (
        in_tok * pin
        + out_tok * pout
        + cache_r * pin * 0.1
        + cache_w * pin * 1.25
    ) / 1_000_000

    conn.execute("DELETE FROM sessions WHERE id = ?", (sess.id,))
    conn.execute("DELETE FROM messages_fts WHERE session_id = ?", (sess.id,))
    conn.execute(
        """INSERT INTO sessions VALUES
           (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            sess.id, str(sess.path), sess.slug_dir, sess.cwd, project, sess.title,
            sess.git_branch, sess.version, sess.message_count, sess.first_ts,
            sess.last_ts, sess.size, int(sess.orphaned), in_tok, out_tok,
            cache_r, cache_w, model, round(cost, 4),
        ),
    )
    conn.executemany("INSERT INTO messages_fts VALUES (?,?,?,?)", fts_rows)


def _index_all(conn: sqlite3.Connection, projects_dir: Path) -> tuple[int, int]:
    """Index every transcript into ``conn``, skipping files unchanged since last time."""
    _ensure_schema(conn)
    known = {r["path"]: (r["size"], r["mtime"]) for r in conn.execute("SELECT * FROM files")}
    indexed = skipped = 0
    for jsonl in sorted(projects_dir.glob("*/*.jsonl")):
        st = jsonl.stat()
        key = str(jsonl)
        if known.get(key) == (st.st_size, st.st_mtime):
            skipped += 1
            continue
        _index_one(conn, jsonl)
        conn.execute(
            "INSERT OR REPLACE INTO files VALUES (?,?,?)",
            (key, st.st_size, st.st_mtime),
        )
        indexed += 1
    conn.commit()
    return indexed, skipped


def build(projects_dir: Optional[Path] = None, db_path: Optional[Path] = None,
          rebuild: bool = False) -> dict:
    """Incrementally (re)build the persistent index. Returns counts."""
    projects_dir = projects_dir or PROJECTS_DIR
    db_path = db_path or DB_PATH
    if rebuild and db_path.exists():
        db_path.unlink()
    conn = connect(db_path)
    indexed, skipped = _index_all(conn, projects_dir)
    total = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
    conn.close()
    return {"indexed": indexed, "skipped": skipped, "total_sessions": total}


def query_connection(*, no_cache: bool = False, projects_dir: Optional[Path] = None,
                     db_path: Optional[Path] = None) -> tuple[sqlite3.Connection, bool]:
    """A connection to query for ``search``/``stats``, plus whether it's ephemeral.

    Uses the persistent index when it exists (and ``no_cache`` is false); otherwise
    builds a throwaway in-memory index and returns it — nothing is written to disk.
    """
    db_path = db_path or DB_PATH
    if not no_cache and db_path.exists():
        return connect(db_path), False
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    _index_all(conn, projects_dir or PROJECTS_DIR)
    return conn, True


def search_conn(conn: sqlite3.Connection, query: str, *, project: Optional[str] = None,
                since: Optional[str] = None, limit: int = 20) -> list[dict]:
    """Full-text search across all indexed messages in ``conn``, grouped by session.

    Filters (``project``/``since``) are applied in SQL alongside the FTS match — before
    the safety cap — so a narrow filter can't be starved by unrelated matches.
    ``snippet()`` runs in the same query and session metadata is joined in.
    """
    sql = [
        "SELECT m.session_id AS id, "
        "snippet(messages_fts, 0, '[', ']', ' … ', 12) AS snip, "
        "s.title AS title, s.project AS project, s.cwd AS cwd, "
        "s.orphaned AS orphaned, s.last_ts AS last_ts "
        "FROM messages_fts m JOIN sessions s ON s.id = m.session_id "
        "WHERE messages_fts MATCH ?"
    ]
    params: list = [query]
    if project:
        sql.append("AND s.project = ?")
        params.append(project)
    if since:
        sql.append("AND COALESCE(s.last_ts, '') >= ?")
        params.append(since)
    sql.append("LIMIT 2000")
    rows = conn.execute(" ".join(sql), params).fetchall()

    grouped: dict[str, dict] = {}
    for r in rows:
        g = grouped.get(r["id"])
        if g is None:
            grouped[r["id"]] = {
                "id": r["id"], "title": r["title"], "project": r["project"],
                "cwd": r["cwd"], "orphaned": r["orphaned"], "last_ts": r["last_ts"],
                "snip": r["snip"], "hits": 1,
            }
        else:
            g["hits"] += 1
    out = sorted(grouped.values(), key=lambda r: r["last_ts"] or "", reverse=True)
    return out[:limit]


def search(query: str, *, project: Optional[str] = None, since: Optional[str] = None,
           limit: int = 20, db_path: Optional[Path] = None) -> list[dict]:
    """Search the persistent index (convenience wrapper around :func:`search_conn`)."""
    conn = connect(db_path)
    try:
        return search_conn(conn, query, project=project, since=since, limit=limit)
    finally:
        conn.close()
