"""Tests for ccsess.cli — formatting, classification, width fitting, resolving."""

import json

from ccsess import cli, core


def _session(**kw):
    base = dict(id="0123456789ab", path=None, slug_dir="slug", cwd=None, title=None,
                git_branch=None, version=None, message_count=0,
                first_ts=None, last_ts=None, size=0)
    base.update(kw)
    return core.Session(**base)


# --------------------------------------------------------------------------- #
# small formatters
# --------------------------------------------------------------------------- #
def test_human_units():
    assert cli.human(512) == "512B"
    assert cli.human(1536) == "1.5KB"
    assert cli.human(5 * 1024 * 1024) == "5.0MB"


def test_plural():
    assert cli._plural(1, "session") == "1 session"
    assert cli._plural(2, "session") == "2 sessions"


def test_short_ts():
    assert cli.short_ts("2026-06-24T13:17:09.123Z") == "2026-06-24 13:17"
    assert cli.short_ts(None) == ""


# --------------------------------------------------------------------------- #
# orphan vs stale-backup classification
# --------------------------------------------------------------------------- #
def test_classify_marks_duplicate_as_backup_not_orphan(tmp_path):
    live = _session(id="dup", path=tmp_path / "a.jsonl", cwd=str(tmp_path))          # resumable
    stale = _session(id="dup", path=tmp_path / "b.jsonl", cwd=str(tmp_path / "gone"))  # orphaned copy
    true_orphans, backups, _ = cli._classify([live, stale])
    assert backups == [stale]
    assert true_orphans == []


def test_classify_true_orphan_has_no_resumable_copy(tmp_path):
    only = _session(id="solo", path=tmp_path / "a.jsonl", cwd=str(tmp_path / "gone"))
    true_orphans, backups, _ = cli._classify([only])
    assert true_orphans == [only]
    assert backups == []


# --------------------------------------------------------------------------- #
# width-responsive session row (color is off under pytest, so len == cells)
# --------------------------------------------------------------------------- #
def test_session_line_fits_width_and_drops_columns_when_narrow():
    s = _session(id="abcdef012345", last_ts="2026-06-24T13:17:00", size=2048,
                 message_count=42, git_branch="ZBRANCHZ", title="Z" * 200)

    wide = cli._session_line(s, 120)
    assert len(wide) <= 120
    assert "ZBRANCHZ" in wide               # branch column present when there's room

    narrow = cli._session_line(s, 64)
    assert len(narrow) <= 64
    assert "ZBRANCHZ" not in narrow         # branch dropped first when space is tight
    assert "\x1b[" not in narrow            # no stray ANSI in non-tty output


# --------------------------------------------------------------------------- #
# resolving a target by scan index / name / id-prefix
# --------------------------------------------------------------------------- #
def test_resolve_projects_by_scan_index(tmp_path, monkeypatch):
    jsonl = tmp_path / "slug" / "sid.jsonl"
    jsonl.parent.mkdir(parents=True)
    jsonl.write_text(json.dumps({"type": "user", "cwd": "/proj",
                                 "message": {"content": "hi"}}) + "\n", encoding="utf-8")
    cache = tmp_path / "scan.json"
    cache.write_text(json.dumps({"projects": [
        {"index": 1, "name": "proj", "key": "/proj", "status": "OK",
         "sessions": [{"id": "sid", "path": str(jsonl)}]}]}), encoding="utf-8")
    monkeypatch.setattr(cli, "_SCAN_CACHE", cache)

    groups = cli._resolve_projects("1")
    assert len(groups) == 1
    assert [s.id for s in groups[0]["sessions"]] == ["sid"]

    assert cli._resolve_projects("99") == []   # unknown index → no match


def test_resolve_projects_by_name_and_id_prefix(monkeypatch):
    sessions = [_session(id="aaaa1111", cwd="/home/me/myproj"),
                _session(id="bbbb2222", cwd="/home/me/other")]
    monkeypatch.setattr(core, "iter_sessions", lambda *a, **k: sessions)

    by_name = cli._resolve_projects("myproj")
    assert len(by_name) == 1 and by_name[0]["name"] == "myproj"

    by_id = cli._resolve_projects("aaaa")
    assert len(by_id) == 1
    assert by_id[0]["sessions"][0].id == "aaaa1111"

    assert cli._resolve_projects("nope-no-match") == []
