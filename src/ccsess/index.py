"""SQLite + FTS5 index over all Claude Code transcripts.

Builds an incremental local index (``~/.claude/ccsess.db``) used by ``search`` and
``stats``. Re-running ``build`` only re-parses files whose size or mtime changed.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Optional

from .core import PROJECTS_DIR, iter_lines, read_session

DB_PATH = Path.home() / ".claude" / "ccsess.db"

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


def connect(db_path: Path = DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
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
        CREATE TABLE IF NOT EXISTS messages (
            session_id TEXT,
            seq INTEGER,
            role TEXT,
            ts TEXT,
            tool TEXT,
            text TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_msg_session ON messages(session_id);
        CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
            text, session_id UNINDEXED, role UNINDEXED, ts UNINDEXED,
            tokenize = 'porter unicode61'
        );
        """
    )


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
                parts.append(c)
            elif isinstance(c, list):
                parts.extend(b.get("text", "") for b in c if isinstance(b, dict))
    return "\n".join(p for p in parts if p), tool


def _index_one(conn: sqlite3.Connection, jsonl: Path) -> None:
    sess = read_session(jsonl)
    project = sess.project_name
    in_tok = out_tok = cache_r = cache_w = 0
    model = None
    rows: list[tuple] = []
    fts_rows: list[tuple] = []
    seq = 0
    for d in iter_lines(jsonl):
        t = d.get("type")
        if t not in ("user", "assistant"):
            continue
        msg = d.get("message") or {}
        text, tool = _extract_text(msg.get("content"))
        ts = d.get("timestamp")
        rows.append((sess.id, seq, t, ts, tool, text))
        if text.strip():
            fts_rows.append((text, sess.id, t, ts or ""))
        if t == "assistant":
            usage = msg.get("usage") or {}
            in_tok += usage.get("input_tokens", 0) or 0
            out_tok += usage.get("output_tokens", 0) or 0
            cache_r += usage.get("cache_read_input_tokens", 0) or 0
            cache_w += usage.get("cache_creation_input_tokens", 0) or 0
            model = model or msg.get("model")
        seq += 1

    pin, pout = _price_for(model or "")
    # cache read ~10% of input price, cache write ~25% premium (rough).
    cost = (
        in_tok * pin
        + out_tok * pout
        + cache_r * pin * 0.1
        + cache_w * pin * 1.25
    ) / 1_000_000

    conn.execute("DELETE FROM sessions WHERE id = ?", (sess.id,))
    conn.execute("DELETE FROM messages WHERE session_id = ?", (sess.id,))
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
    conn.executemany("INSERT INTO messages VALUES (?,?,?,?,?,?)", rows)
    conn.executemany("INSERT INTO messages_fts VALUES (?,?,?,?)", fts_rows)


def build(projects_dir: Path = PROJECTS_DIR, db_path: Path = DB_PATH,
          rebuild: bool = False) -> dict:
    """Incrementally (re)build the index. Returns counts."""
    if rebuild and db_path.exists():
        db_path.unlink()
    conn = connect(db_path)
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
    total = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
    conn.close()
    return {"indexed": indexed, "skipped": skipped, "total_sessions": total}


def search(query: str, *, project: Optional[str] = None, since: Optional[str] = None,
           limit: int = 20, db_path: Path = DB_PATH) -> list[dict]:
    """Full-text search across all indexed messages, grouped by session.

    ``snippet()`` only works in a non-aggregate query against the FTS table, so we
    fetch matches with snippets first, then dedupe by session and join metadata.
    """
    conn = connect(db_path)
    matches = conn.execute(
        "SELECT session_id, snippet(messages_fts, 0, '[', ']', ' … ', 12) AS snip "
        "FROM messages_fts WHERE messages_fts MATCH ? LIMIT 2000",
        (query,),
    ).fetchall()
    order: list[str] = []
    info: dict[str, dict] = {}
    for m in matches:
        sid = m["session_id"]
        if sid not in info:
            info[sid] = {"hits": 0, "snip": m["snip"]}
            order.append(sid)
        info[sid]["hits"] += 1
    if not info:
        conn.close()
        return []
    qmarks = ",".join("?" * len(info))
    meta = {r["id"]: r for r in conn.execute(
        f"SELECT * FROM sessions WHERE id IN ({qmarks})", list(info))}
    conn.close()

    out: list[dict] = []
    for sid in order:
        s = meta.get(sid)
        if not s:
            continue
        if project and s["project"] != project:
            continue
        if since and (s["last_ts"] or "") < since:
            continue
        out.append({
            "id": sid, "title": s["title"], "project": s["project"],
            "cwd": s["cwd"], "orphaned": s["orphaned"], "last_ts": s["last_ts"],
            "snip": info[sid]["snip"], "hits": info[sid]["hits"],
        })
    out.sort(key=lambda r: r["last_ts"] or "", reverse=True)
    return out[:limit]
