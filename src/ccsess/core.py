"""Core helpers for reading, relinking, and locating Claude Code sessions.

Claude Code stores every session as ``~/.claude/projects/<slug>/<id>.jsonl`` where
``<slug>`` is the working-directory path with ``/`` replaced by ``-`` (dots and
underscores are preserved). Because the storage location is derived purely from the
path, moving/renaming a project directory orphans its sessions. These helpers read
the transcripts, compute the slug, and safely relink files into a new home.
"""

from __future__ import annotations

import json
import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Optional


def config_dir() -> Path:
    """Base Claude Code config directory.

    Honors ``$CLAUDE_CONFIG_DIR`` (the same override Claude Code itself uses to
    relocate session storage) and falls back to ``~/.claude``."""
    env = os.environ.get("CLAUDE_CONFIG_DIR")
    return Path(env).expanduser() if env else Path.home() / ".claude"


CONFIG_DIR = config_dir()
PROJECTS_DIR = CONFIG_DIR / "projects"

# Directories we never descend into while hunting for an orphan's new home.
_PRUNE = {
    ".git", "node_modules", ".venv", "venv", "__pycache__", ".next", "dist",
    "build", ".cache", "target", ".turbo", "vendor", ".pnpm-store", ".gradle",
}
# Default roots to search for a moved directory (first existing ones are used).
_DEFAULT_SEARCH_ROOTS = [
    Path.home() / "work",
    Path.home() / "dev",
    Path.home() / "code",
    Path.home() / "projects",
    Path.home() / "src",
]


def slug_for(path: str | Path) -> str:
    """Encode a filesystem path the way Claude Code names its project folders."""
    return str(path).replace("/", "-")


def project_dir_for(path: str | Path) -> Path:
    """The ``~/.claude/projects`` folder a session for ``path`` would live in."""
    return PROJECTS_DIR / slug_for(path)


# --------------------------------------------------------------------------- #
# Reading sessions
# --------------------------------------------------------------------------- #

@dataclass
class Session:
    id: str
    path: Path
    slug_dir: str
    cwd: Optional[str]
    title: Optional[str]
    git_branch: Optional[str]
    version: Optional[str]
    message_count: int
    first_ts: Optional[str]
    last_ts: Optional[str]
    size: int

    @property
    def orphaned(self) -> bool:
        """True when the session can't be resumed from its recorded cwd as-is.

        That means the cwd is gone — or it isn't an absolute path. Claude Code always
        records an *absolute* working directory, so a relative cwd (e.g. ``../vane``
        from a botched relink) is a corrupted transcript that no `claude --resume`
        will ever find, even though the relative path may resolve from some cwd."""
        if not self.cwd:
            return False
        p = Path(self.cwd)
        return not (p.is_absolute() and p.exists())

    @property
    def project_name(self) -> str:
        return Path(self.cwd).name if self.cwd else self.slug_dir


def iter_lines(path: Path) -> Iterator[dict]:
    """Yield parsed JSON objects from a transcript, skipping unparseable lines."""
    with path.open(encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def first_user_text(path: Path) -> Optional[str]:
    """First human message text in a session (used as a title fallback)."""
    for d in iter_lines(path):
        if d.get("type") != "user":
            continue
        msg = d.get("message") or {}
        content = msg.get("content")
        if isinstance(content, str) and content.strip():
            return " ".join(content.split())
        if isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and part.get("type") == "text" and part.get("text", "").strip():
                    return " ".join(part["text"].split())
    return None


def read_session(path: Path) -> Session:
    """Build a lightweight :class:`Session` summary by scanning a transcript once."""
    cwd = git_branch = version = title = None
    first_ts = last_ts = None
    msg_count = 0
    for d in iter_lines(path):
        t = d.get("type")
        if cwd is None and d.get("cwd"):
            cwd = d["cwd"]
        if git_branch is None and d.get("gitBranch"):
            git_branch = d["gitBranch"]
        if version is None and d.get("version"):
            version = d["version"]
        if d.get("aiTitle"):  # latest ai-title line wins
            title = " ".join(d["aiTitle"].split())
        ts = d.get("timestamp")
        if ts:
            if first_ts is None:
                first_ts = ts
            last_ts = ts
        if t in ("user", "assistant"):
            msg_count += 1
    if not title:
        ut = first_user_text(path)
        if ut:
            title = ut[:80] + ("…" if len(ut) > 80 else "")
    return Session(
        id=path.stem,
        path=path,
        slug_dir=path.parent.name,
        cwd=cwd,
        title=title,
        git_branch=git_branch,
        version=version,
        message_count=msg_count,
        first_ts=first_ts,
        last_ts=last_ts,
        size=path.stat().st_size,
    )


def iter_sessions(projects_dir: Optional[Path] = None) -> Iterator[Session]:
    """Yield a :class:`Session` for every transcript under ``projects_dir``."""
    projects_dir = projects_dir or PROJECTS_DIR
    for jsonl in sorted(projects_dir.glob("*/*.jsonl")):
        try:
            yield read_session(jsonl)
        except OSError:
            continue


def find_session(session_id: str, projects_dir: Optional[Path] = None) -> Optional[Path]:
    """Locate a transcript file by session id, wherever it lives."""
    projects_dir = projects_dir or PROJECTS_DIR
    matches = list(projects_dir.glob(f"*/{session_id}.jsonl"))
    return matches[0] if matches else None


def empty_slug_dirs(projects_dir: Optional[Path] = None) -> list[Path]:
    """Project folders that contain no ``.jsonl`` transcript (stale leftovers)."""
    projects_dir = projects_dir or PROJECTS_DIR
    if not projects_dir.is_dir():
        return []
    out = []
    for d in sorted(projects_dir.iterdir()):
        if d.is_dir() and not any(d.glob("*.jsonl")):
            out.append(d)
    return out


# --------------------------------------------------------------------------- #
# Relinking (the proven rescue routine, generalized)
# --------------------------------------------------------------------------- #

@dataclass
class RelinkPlan:
    src: Path
    dest: Path
    old_cwd: Optional[str]
    new_cwd: Optional[str]
    rewrite_paths: bool
    move: bool


def plan_relink(
    src: Path,
    target_dir: str | Path,
    *,
    move: bool = False,
    rewrite_paths: bool = True,
) -> RelinkPlan:
    """Describe relinking ``src`` so the session resumes from ``target_dir``.

    ``target_dir`` is normalized to an absolute path (expanding ``~`` and resolving
    ``.``/``..`` against the current directory) because Claude Code derives the storage
    slug from the *absolute* working directory — a relative ``--to`` like ``../vane``
    would otherwise produce a bogus ``..-vane`` slug that can never resume.
    """
    target_dir = str(Path(target_dir).expanduser().resolve())
    sess = read_session(src)
    dest = project_dir_for(target_dir) / src.name
    return RelinkPlan(
        src=src,
        dest=dest,
        old_cwd=sess.cwd,
        new_cwd=target_dir if rewrite_paths else None,
        rewrite_paths=rewrite_paths and bool(sess.cwd) and sess.cwd != target_dir,
        move=move,
    )


def apply_relink(plan: RelinkPlan) -> dict:
    """Execute a :class:`RelinkPlan`. Copies (or moves) then optionally rewrites
    the old cwd prefix to the new one, validating every line still parses."""
    plan.dest.parent.mkdir(parents=True, exist_ok=True)
    if plan.move:
        shutil.move(str(plan.src), str(plan.dest))
    else:
        shutil.copy2(str(plan.src), str(plan.dest))

    replacements = lines = 0
    if plan.rewrite_paths and plan.old_cwd and plan.new_cwd:
        backup = plan.dest.with_suffix(plan.dest.suffix + ".bak")
        shutil.copy2(str(plan.dest), str(backup))
        out: list[str] = []
        with plan.dest.open(encoding="utf-8", errors="replace") as f:
            for raw in f:
                line = raw.rstrip("\n")
                if not line:
                    out.append(raw)
                    continue
                lines += 1
                json.loads(line)  # must parse before
                if plan.old_cwd in line:
                    line = line.replace(plan.old_cwd, plan.new_cwd)
                    json.loads(line)  # must still parse after
                    replacements += line.count(plan.new_cwd)
                out.append(line + "\n")
        with plan.dest.open("w", encoding="utf-8") as f:
            f.writelines(out)
    return {"dest": plan.dest, "lines": lines, "replacements": replacements}


# --------------------------------------------------------------------------- #
# Finding an orphan's new home
# --------------------------------------------------------------------------- #

@dataclass
class Candidate:
    path: Path
    score: int
    hits: int
    branch_match: bool


def _referenced_subpaths(src: Path, old_cwd: str, limit: int = 40) -> list[str]:
    """Relative paths the transcript referenced under the old working dir."""
    pat = re.compile(re.escape(old_cwd.rstrip("/")) + r"/([A-Za-z0-9._\-/]+)")
    counts: dict[str, int] = {}
    with src.open(encoding="utf-8", errors="replace") as f:
        for line in f:
            for m in pat.findall(line):
                rel = m.split('"')[0].split("\\")[0].rstrip("/.,)")
                if rel:
                    counts[rel] = counts.get(rel, 0) + 1
    return [p for p, _ in sorted(counts.items(), key=lambda kv: -kv[1])[:limit]]


def _git_branch_of(path: Path) -> Optional[str]:
    """The checked-out branch of a git repo at ``path`` (None if not a repo/branch)."""
    try:
        head = (path / ".git" / "HEAD").read_text(encoding="utf-8").strip()
    except OSError:
        return None
    prefix = "ref: refs/heads/"
    return head[len(prefix):] if head.startswith(prefix) else None


def _walk_dirs(roots: list[Path], target_basename: str, max_depth: int = 6) -> Iterator[Path]:
    for root in roots:
        if not root.is_dir():
            continue
        root_depth = len(root.parts)
        for dirpath, dirnames, _ in os.walk(root):
            p = Path(dirpath)
            if len(p.parts) - root_depth >= max_depth:
                dirnames[:] = []
                continue
            dirnames[:] = [d for d in dirnames if d not in _PRUNE and not d.startswith(".")]
            if p.name == target_basename:
                yield p


def candidate_dirs(
    src: Path,
    old_cwd: str,
    *,
    search_roots: Optional[list[Path]] = None,
    git_branch: Optional[str] = None,
    limit: int = 5,
) -> list[Candidate]:
    """Rank existing directories as the likely new home of an orphaned session.

    Scores each candidate by how many of the transcript's referenced subpaths
    exist under it, with a boost for matching basename and git branch.
    """
    roots = search_roots or [r for r in _DEFAULT_SEARCH_ROOTS if r.is_dir()]
    basename = Path(old_cwd).name
    subpaths = _referenced_subpaths(src, old_cwd)

    seen: set[Path] = set()
    results: list[Candidate] = []
    for cand in _walk_dirs(roots, basename):
        if cand in seen or str(cand) == old_cwd:
            continue
        seen.add(cand)
        # Every walked dir already matches the basename, so rank by how many of the
        # transcript's referenced subpaths exist here, with a boost for a real branch match.
        hits = sum(1 for rel in subpaths if (cand / rel).exists())
        branch_match = bool(git_branch) and _git_branch_of(cand) == git_branch
        score = hits * 10 + (2 if branch_match else 0)
        results.append(Candidate(cand, score, hits, branch_match))

    results.sort(key=lambda c: (-c.score, len(str(c.path))))
    return results[:limit]
