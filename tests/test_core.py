"""Tests for ccsess.core — reading, classifying, and relinking sessions."""

import json
from pathlib import Path

from ccsess import core


def _write_jsonl(path, *objs):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(o) + "\n" for o in objs), encoding="utf-8")


def _session(**kw):
    """Build a Session with sensible defaults, overriding the fields a test cares about."""
    base = dict(id="0123456789ab", path=None, slug_dir="slug", cwd=None, title=None,
                git_branch=None, version=None, message_count=0,
                first_ts=None, last_ts=None, size=0)
    base.update(kw)
    return core.Session(**base)


# --------------------------------------------------------------------------- #
# slug encoding
# --------------------------------------------------------------------------- #
def test_slug_for_replaces_slashes():
    assert core.slug_for("/Users/dom/work/vane") == "-Users-dom-work-vane"


def test_project_dir_for_lives_under_projects_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(core, "PROJECTS_DIR", tmp_path)
    got = core.project_dir_for("/a/b")
    assert got == tmp_path / "-a-b"


# --------------------------------------------------------------------------- #
# title derivation collapses internal whitespace (the "Changed fi" bug)
# --------------------------------------------------------------------------- #
def test_title_from_first_user_message_is_single_line(tmp_path):
    p = tmp_path / "s.jsonl"
    _write_jsonl(p, {"type": "user", "cwd": "/x",
                     "message": {"content": "Review this change.\n\nChanged files: a.py"}})
    sess = core.read_session(p)
    assert sess.title == "Review this change. Changed files: a.py"
    assert "\n" not in (sess.title or "")


def test_title_from_aititle_is_collapsed(tmp_path):
    p = tmp_path / "s.jsonl"
    _write_jsonl(p, {"type": "user", "cwd": "/x", "aiTitle": "My  Title\nwith   gaps",
                     "message": {"content": "hi"}})
    assert core.read_session(p).title == "My Title with gaps"


# --------------------------------------------------------------------------- #
# orphan detection (including the relative-cwd hardening)
# --------------------------------------------------------------------------- #
def test_orphaned_false_when_cwd_absolute_and_exists(tmp_path):
    assert _session(cwd=str(tmp_path)).orphaned is False


def test_orphaned_true_when_absolute_cwd_missing(tmp_path):
    assert _session(cwd=str(tmp_path / "gone")).orphaned is True


def test_orphaned_true_for_relative_cwd_even_if_it_resolves(tmp_path, monkeypatch):
    # '../something' may resolve from some cwd, but Claude only records absolute paths,
    # so a relative cwd is a corrupted/unresumable transcript.
    (tmp_path / "vane").mkdir()
    monkeypatch.chdir(tmp_path / "vane")
    assert _session(cwd="../vane").orphaned is True


def test_orphaned_false_when_no_cwd():
    assert _session(cwd=None).orphaned is False


# --------------------------------------------------------------------------- #
# scanning the projects directory
# --------------------------------------------------------------------------- #
def test_iter_sessions_reads_top_level_only(tmp_path):
    _write_jsonl(tmp_path / "slugA" / "a.jsonl", {"type": "user", "message": {"content": "x"}})
    _write_jsonl(tmp_path / "slugB" / "b.jsonl", {"type": "user", "message": {"content": "y"}})
    # nested files (e.g. subagents) must be ignored
    _write_jsonl(tmp_path / "slugA" / "subagents" / "c.jsonl", {"type": "user"})
    ids = sorted(s.id for s in core.iter_sessions(tmp_path))
    assert ids == ["a", "b"]


def test_empty_slug_dirs(tmp_path):
    (tmp_path / "empty").mkdir()
    _write_jsonl(tmp_path / "full" / "s.jsonl", {"type": "user"})
    assert core.empty_slug_dirs(tmp_path) == [tmp_path / "empty"]


def test_empty_slug_dirs_missing_dir_returns_empty(tmp_path):
    # a fresh / relocated config dir may not have a projects/ folder yet
    assert core.empty_slug_dirs(tmp_path / "does-not-exist") == []


# --------------------------------------------------------------------------- #
# config dir honors CLAUDE_CONFIG_DIR
# --------------------------------------------------------------------------- #
def test_config_dir_honors_env(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "cfg"))
    assert core.config_dir() == tmp_path / "cfg"
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    assert core.config_dir() == Path.home() / ".claude"


# --------------------------------------------------------------------------- #
# git branch detection + candidate ranking
# --------------------------------------------------------------------------- #
def test_git_branch_of(tmp_path):
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    (repo / ".git" / "HEAD").write_text("ref: refs/heads/feature/x\n", encoding="utf-8")
    assert core._git_branch_of(repo) == "feature/x"
    assert core._git_branch_of(tmp_path / "nope") is None
    (repo / ".git" / "HEAD").write_text("0123abc\n", encoding="utf-8")  # detached
    assert core._git_branch_of(repo) is None


def test_candidate_dirs_ranks_by_referenced_paths(tmp_path):
    old = "/old/myproj"
    src = tmp_path / "src.jsonl"
    _write_jsonl(src, {"type": "user", "cwd": old,
                       "message": {"content": "edit /old/myproj/src/app.py twice: /old/myproj/src/app.py"}})
    roots = tmp_path / "roots"
    good = roots / "a" / "myproj"
    (good / "src").mkdir(parents=True)
    (good / "src" / "app.py").write_text("x", encoding="utf-8")
    bad = roots / "b" / "myproj"
    bad.mkdir(parents=True)

    cands = core.candidate_dirs(src, old, search_roots=[roots])
    assert cands and cands[0].path == good        # the dir that actually has the file wins
    assert cands[0].hits >= 1
    assert {c.path: c.hits for c in cands}[bad] == 0


# --------------------------------------------------------------------------- #
# relink planning — the relative --to bug fix
# --------------------------------------------------------------------------- #
def test_plan_relink_resolves_relative_target_to_absolute(tmp_path, monkeypatch):
    projects = tmp_path / "projects"
    monkeypatch.setattr(core, "PROJECTS_DIR", projects)
    old = tmp_path / "oldproj"
    src = projects / core.slug_for(str(old)) / "sid.jsonl"
    _write_jsonl(src, {"type": "user", "cwd": str(old), "message": {"content": "hi"}})

    work = tmp_path / "wd"
    (work / "sub").mkdir(parents=True)
    newhome = work / "newhome"
    newhome.mkdir()
    monkeypatch.chdir(work / "sub")

    plan = core.plan_relink(src, "../newhome")
    expected = str(newhome.resolve())

    assert plan.new_cwd == expected                       # absolute, not "../newhome"
    assert ".." not in plan.new_cwd
    assert plan.dest == projects / core.slug_for(expected) / "sid.jsonl"
    assert plan.rewrite_paths is True


def test_apply_relink_copies_backs_up_and_rewrites(tmp_path, monkeypatch):
    projects = tmp_path / "projects"
    monkeypatch.setattr(core, "PROJECTS_DIR", projects)
    old = "/old/proj"
    src = projects / core.slug_for(old) / "sid.jsonl"
    _write_jsonl(src, {"type": "user", "cwd": old,
                       "message": {"content": "open /old/proj/main.py"}})
    new = tmp_path / "newhome"
    new.mkdir()

    plan = core.plan_relink(src, str(new))
    res = core.apply_relink(plan)
    dest = plan.dest
    text = dest.read_text(encoding="utf-8")

    assert dest.exists()
    assert dest.with_suffix(dest.suffix + ".bak").exists()   # original backed up first
    assert old not in text                                   # old cwd rewritten away
    assert plan.new_cwd in text
    assert res["replacements"] >= 1
    assert src.exists()                                      # copy, not move
    for line in text.splitlines():                           # every line still valid JSON
        json.loads(line)
