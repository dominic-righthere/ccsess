"""Tests for ccsess.index — incremental build, FTS search, schema."""

import json

from ccsess import index as idx


def _build(tmp_path):
    projects = tmp_path / "projects"
    slug = projects / "-proj"
    slug.mkdir(parents=True)
    (slug / "sid.jsonl").write_text("\n".join([
        json.dumps({"type": "user", "cwd": "/proj", "timestamp": "2026-01-01T00:00:00",
                    "message": {"content": "hello stripe webhook signature"}}),
        json.dumps({"type": "assistant", "timestamp": "2026-01-01T00:00:01",
                    "message": {"content": [{"type": "text", "text": "verify the stripe signature"}],
                                "usage": {"input_tokens": 10, "output_tokens": 5},
                                "model": "claude-opus-4"}}),
    ]) + "\n", encoding="utf-8")
    db = tmp_path / "i.db"
    res = idx.build(projects_dir=projects, db_path=db)
    return projects, db, res


def test_build_indexes_sessions(tmp_path):
    _, _, res = _build(tmp_path)
    assert res == {"indexed": 1, "skipped": 0, "total_sessions": 1}


def test_build_is_incremental(tmp_path):
    projects, db, _ = _build(tmp_path)
    res2 = idx.build(projects_dir=projects, db_path=db)
    assert res2["indexed"] == 0 and res2["skipped"] == 1


def test_search_finds_and_filters(tmp_path):
    _, db, _ = _build(tmp_path)
    hits = idx.search("stripe", db_path=db)
    assert len(hits) == 1
    assert hits[0]["project"] == "proj"
    assert hits[0]["hits"] >= 1                                  # both messages mention stripe
    assert idx.search("stripe", project="nope", db_path=db) == []
    assert idx.search("stripe", since="2027-01-01", db_path=db) == []
    assert idx.search("zzznotfound", db_path=db) == []


def test_messages_table_dropped(tmp_path):
    _, db, _ = _build(tmp_path)
    conn = idx.connect(db)
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    conn.close()
    assert "messages" not in tables          # dead table removed
    assert "messages_fts" in tables          # FTS is the text store
    assert "sessions" in tables


def test_extract_text_caps_tool_output_not_prose():
    big = "x" * 5000
    text, _ = idx._extract_text([{"type": "tool_result", "content": big}])
    assert len(text) <= idx._TOOL_RESULT_CAP          # giant tool output is trimmed
    prose, _ = idx._extract_text([{"type": "text", "text": "y" * 5000}])
    assert len(prose) == 5000                          # prose is never capped


def test_query_connection_ephemeral_writes_nothing(tmp_path):
    projects, _, _ = _build(tmp_path)
    missing = tmp_path / "no-such.db"
    conn, ephemeral = idx.query_connection(projects_dir=projects, db_path=missing)
    try:
        assert ephemeral is True
        rows = idx.search_conn(conn, "stripe")
        assert len(rows) == 1                          # full-quality search, no file
    finally:
        conn.close()
    assert not missing.exists()                        # nothing persisted


def test_query_connection_uses_persistent_cache(tmp_path):
    projects, db, _ = _build(tmp_path)
    conn, ephemeral = idx.query_connection(projects_dir=projects, db_path=db)
    conn.close()
    assert ephemeral is False                          # reuses the built index
    # --no-cache forces ephemeral even when the cache exists
    conn2, eph2 = idx.query_connection(no_cache=True, projects_dir=projects, db_path=db)
    conn2.close()
    assert eph2 is True
