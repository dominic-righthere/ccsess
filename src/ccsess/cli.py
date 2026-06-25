"""ccsess — rescue, search, resume, and mine Claude Code sessions."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import textwrap
from collections import defaultdict
from pathlib import Path

from . import core
from . import render
from . import index as idx

# --------------------------------------------------------------------------- #
# tiny output helpers (zero deps)
# --------------------------------------------------------------------------- #
_COLOR = sys.stdout.isatty() and not os.environ.get("NO_COLOR")


def _c(code: str, s: str) -> str:
    return f"\033[{code}m{s}\033[0m" if _COLOR else s


def bold(s): return _c("1", s)
def dim(s): return _c("2", s)
def red(s): return _c("31", s)
def green(s): return _c("32", s)
def yellow(s): return _c("33", s)
def cyan(s): return _c("36", s)


def human(n: float) -> str:
    step = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if step < 1024 or unit == "TB":
            return f"{step:.0f}{unit}" if unit == "B" else f"{step:.1f}{unit}"
        step /= 1024
    return f"{step:.1f}TB"


def short_ts(ts) -> str:
    return (ts or "")[:16].replace("T", " ")


def short_id(sid: str) -> str:
    return sid[:8]


def term_width() -> int:
    """Current terminal width (honors $COLUMNS; falls back to 80 when not a TTY)."""
    return shutil.get_terminal_size((80, 24)).columns


def _trunc(s: str, n: int) -> str:
    """Truncate keeping the head, with an ellipsis."""
    return s if len(s) <= n else s[:max(1, n - 1)] + "…"


def _trunc_left(s: str, n: int) -> str:
    """Truncate keeping the tail — for paths (…/keep/the/meaningful/end)."""
    return s if len(s) <= n else "…" + s[-(n - 1):]


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


def _badge(status: str) -> str:
    """Fixed-width, log-level-style status word (color is decoration, not the signal)."""
    label = f"{status:<6}"
    if status == "ORPHAN":
        return red(bold(label))
    if status == "BACKUP":
        return yellow(label)
    return green(label)  # OK


def _session_line(s: core.Session, width: int) -> str:
    """One aligned per-session detail row, fitted to ``width`` so it never wraps.

    Columns are padded as plain text *before* coloring (so the invisible ANSI codes
    never throw off widths). When the window is narrow, the optional columns are
    dropped right-to-left (branch → msgs → size), then the title is truncated to fit.
    The drop decision depends only on ``width``, so every row stays aligned.
    """
    indent, sep = "    ", "  "
    sid = short_id(s.id)
    ts = f"{(short_ts(s.last_ts) or '—'):<16}"
    size = f"{human(s.size):>8}"
    msgs = f"{s.message_count:>4} msg"
    branch = f"{(s.git_branch or '—')[:12]:<12}"
    # (plain, colored); first two are essential, the rest are droppable when narrow.
    cols = [(sid, dim(sid)), (ts, dim(ts)),
            (size, size), (msgs, dim(msgs)), (branch, cyan(branch))]
    essential = 2
    title = " ".join((s.title or "—").split())
    min_title = 12

    def prefix_w(cs):
        return len(indent) + sum(len(p) for p, _ in cs) + len(sep) * len(cs)

    while len(cols) > essential and prefix_w(cols) + min_title > width:
        cols.pop()
    budget = max(min_title, width - prefix_w(cols))
    title = _trunc(title, budget)
    return indent + sep.join(c for _, c in cols) + sep + title


# --------------------------------------------------------------------------- #
# commands
# --------------------------------------------------------------------------- #
def _classify(sessions):
    """Split orphaned sessions into true orphans vs stale backups.

    A session whose recorded cwd is gone is a *backup* if another file with the
    same id is resumable (its cwd exists); otherwise it's a *true orphan*.
    """
    by_id: dict[str, list] = defaultdict(list)
    for s in sessions:
        by_id[s.id].append(s)

    def is_backup(s):
        return s.orphaned and any(
            (not o.orphaned) for o in by_id[s.id] if o.path != s.path
        )

    true_orphans = [s for s in sessions if s.orphaned and not is_backup(s)]
    backups = [s for s in sessions if s.orphaned and is_backup(s)]
    return true_orphans, backups, is_backup


_SESSIONS_PER_PROJECT_CAP = 10


def _print_section_header(status: str, counts: str, hint: str, width: int) -> None:
    """Badge + counts, with the dim explanatory hint truncated or dropped if it won't fit."""
    base = f"{_badge(status)} " + bold(counts)
    room = width - (6 + 1 + len(counts)) - 3  # badge(6) + space + counts, then "   " gap
    print(base + dim("   " + _trunc(hint, room)) if room >= 12 else base)


def _print_project_line(p: dict, width: int) -> None:
    """One compact line per project: index · name · session count · size · last activity."""
    idx = cyan(f"{p['index']:>3}")
    meta = (f"{_plural(p['n'], 'session')} · {human(p['size'])} · "
            f"last {short_ts(p['last']) or '—'}")
    name = _trunc(p["name"], max(8, width - len(meta) - 9))  # 3 idx + gaps + slack
    print(f"{idx}  {bold(name)}  " + dim(meta))


def _print_project_expansion(p: dict, width: int) -> None:
    """Under -v: the recorded path (orphans/backups) + aligned per-session rows."""
    if p["status"] in ("ORPHAN", "BACKUP"):
        label = "      was: "
        print(dim(label + _trunc_left(p["key"], max(8, width - len(label)))))
    for s in p["sessions"][:_SESSIONS_PER_PROJECT_CAP]:
        print(_session_line(s, width))
    extra = p["n"] - _SESSIONS_PER_PROJECT_CAP
    if extra > 0:
        print(dim(f"      … +{extra} more"))


def cmd_scan(args) -> int:
    sessions = list(core.iter_sessions())
    true_orphans, backups, is_backup = _classify(sessions)

    by_project: dict[str, list] = defaultdict(list)
    for s in sessions:
        by_project[s.cwd or s.slug_dir].append(s)

    projects = []
    for key, ss in by_project.items():
        ss = sorted(ss, key=lambda s: s.last_ts or "", reverse=True)
        key_path = Path(key)
        if key_path.is_absolute() and key_path.exists():
            status = "OK"  # Claude derives the slug from an absolute cwd; a relative
        elif all(is_backup(s) for s in ss):  # one (../vane) is never resumable
            status = "BACKUP"
        else:
            status = "ORPHAN"
        projects.append({
            "key": key,
            "name": ss[0].project_name,
            "sessions": ss,
            "n": len(ss),
            "size": sum(s.size for s in ss),
            "last": max((s.last_ts or "" for s in ss), default=""),
            "status": status,
        })

    def section(status):  # broken-first; recency within a section
        return sorted([p for p in projects if p["status"] == status],
                      key=lambda p: p["last"], reverse=True)

    # Global display order: broken first. Number the projects in that order and
    # persist the map so follow-up commands can target a project by its number.
    order = section("ORPHAN") + section("BACKUP") + section("OK")
    for i, p in enumerate(order, 1):
        p["index"] = i
    _save_scan(order)

    total_size = sum(s.size for s in sessions)
    empties = core.empty_slug_dirs()
    width = term_width()

    home = str(Path.home())
    proj = str(core.PROJECTS_DIR)
    proj = "~" + proj[len(home):] if proj.startswith(home) else proj
    print(bold(f"\n{len(sessions)} sessions · {len(by_project)} projects · "
               f"{human(total_size)}") + dim(f"  {proj}") + "\n")

    sections = [
        ("ORPHAN", "folder moved/renamed — claude --resume can't find them"),
        ("BACKUP", "resumable elsewhere — safe to delete (ccsess clean)"),
        ("OK", "directory exists — resumable normally"),
    ]
    for status, hint in sections:
        ps = [p for p in order if p["status"] == status]
        if not ps:
            continue
        n = sum(p["n"] for p in ps)
        counts = f"{_plural(len(ps), 'project')} · {_plural(n, 'session')}"
        if status == "OK":
            counts += f" · {human(sum(p['size'] for p in ps))}"
        _print_section_header(status, counts, hint, width)
        for p in ps:
            _print_project_line(p, width)
            if args.verbose:
                _print_project_expansion(p, width)
        print()

    print(dim("→ target a project by number or name:  ")
          + "ccsess rescue 1" + dim("   ·   -v expands sessions"))
    if true_orphans:
        print(yellow(f"⚠ {len(true_orphans)} orphaned session(s) needing rescue")
              + "  → " + dim("ccsess rescue <n>"))
    if backups:
        print(dim(f"  {len(backups)} stale backup(s) — ccsess clean"))
    if empties:
        print(dim(f"  {len(empties)} empty/stale slug folder(s) — ccsess clean"))
    return 0


def cmd_doctor(args) -> int:
    sessions = list(core.iter_sessions())
    true_orphans, backups, _ = _classify(sessions)
    empties = core.empty_slug_dirs()
    total = sum(s.size for s in sessions)
    largest = sorted(sessions, key=lambda s: -s.size)[:5]

    print(bold("\nClaude Code session health\n"))
    print(f"  sessions:        {len(sessions)}")
    print(f"  total transcript size: {human(total)}")
    print(f"  orphaned:        {red(str(len(true_orphans))) if true_orphans else green('0')}")
    print(f"  stale backups:   {len(backups)}")
    print(f"  empty folders:   {len(empties)}")

    if true_orphans:
        print(bold("\nOrphans (recorded cwd is gone, no resumable copy):"))
        for s in true_orphans:
            print(f"  {red('✗')} {dim(short_id(s.id))}  {s.cwd}")
            print(f"      {dim('→ ccsess rescue ' + s.id + ' --to <dir> --apply')}")
    if backups:
        print(bold("\nStale backups (same session resumable elsewhere — safe to delete):"))
        for s in backups:
            print(f"  {dim('◌')} {s.path}")
    if empties:
        print(bold("\nEmpty/stale slug folders (safe to remove manually):"))
        for d in empties[:20]:
            print(f"  {dim('·')} {d}")
        if len(empties) > 20:
            print(dim(f"  … and {len(empties) - 20} more"))
    if largest:
        print(bold("\nLargest sessions:"))
        for s in largest:
            print(f"  {human(s.size):>8}  {dim(short_id(s.id))}  "
                  f"{(s.title or '—')[:50]}")
    return 0


_SCAN_CACHE = core.config_dir() / "ccsess-scan.json"


def _save_scan(order: list) -> None:
    """Persist the index→project map from the last scan so commands can target by number."""
    data = {"projects": [
        {"index": p["index"], "name": p["name"], "key": p["key"], "status": p["status"],
         "sessions": [{"id": s.id, "path": str(s.path)} for s in p["sessions"]]}
        for p in order]}
    try:
        _SCAN_CACHE.write_text(json.dumps(data), encoding="utf-8")
    except OSError:
        pass


def _load_scan():
    try:
        return json.loads(_SCAN_CACHE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _group(key: str, sessions: list) -> dict:
    sessions = sorted(sessions, key=lambda s: s.last_ts or "", reverse=True)
    return {"name": (sessions[0].project_name if sessions else key),
            "key": key, "sessions": sessions}


def _resolve_projects(token: str) -> list:
    """Resolve a token to project group(s): ``{name, key, sessions:[Session]}``.

    A token may be a scan index (number), a project name, a slug-folder name, or a
    session id / id-prefix. Index lookups use the map saved by the last ``ccsess scan``.
    """
    token = token.strip()
    if token.isdigit():
        cache = _load_scan()
        if not cache:
            return []
        for p in cache["projects"]:
            if p["index"] == int(token):
                ss = [core.read_session(Path(e["path"]))
                      for e in p["sessions"] if Path(e["path"]).exists()]
                return [_group(p["key"], ss)] if ss else []
        return []

    by_project: dict[str, list] = defaultdict(list)
    for s in core.iter_sessions():
        by_project[s.cwd or s.slug_dir].append(s)

    named = [_group(k, ss) for k, ss in by_project.items()
             if ss[0].project_name == token or k == token]
    if named:
        return named

    # fall back to a session id / unique prefix → just the matching session(s)
    for key, ss in by_project.items():
        hits = [s for s in ss if s.id == token or s.id.startswith(token)]
        if hits:
            return [_group(key, hits)]
    return []


def _resolve_one(token: str):
    """Resolve to a single project group, printing diagnostics on miss/ambiguity."""
    groups = _resolve_projects(token)
    if not groups:
        print(red(f"No project or session matching '{token}'"))
        if token.strip().isdigit():
            print(dim("Numbers come from the last `ccsess scan` — re-run it to refresh."))
        return None
    if len(groups) > 1:
        print(yellow(f"'{token}' matches {len(groups)} projects — use the scan number:"))
        for g in groups:
            print(f"  {bold(g['name'])}  {dim(g['key'])}  "
                  f"{dim(_plural(len(g['sessions']), 'session'))}")
        return None
    return groups[0]


def _pick_session(group: dict, action: str, assume_yes: bool = False):
    """Return the single session to act on, prompting when a project has several."""
    ss = group["sessions"]
    if not ss:
        return None
    if len(ss) == 1:
        return ss[0]
    print(yellow(f"\n{group['name']} has {len(ss)} sessions — which to {action}?"))
    for i, s in enumerate(ss, 1):
        print(f"  {cyan(f'{i:>2}')}  {dim(short_id(s.id))}  "
              f"{(short_ts(s.last_ts) or '—'):<16}  {(s.title or '—')[:50]}")
    if not sys.stdin.isatty():
        print(yellow("Run interactively, or pass a session id, to choose one."))
        return None
    raw = input("  pick # (or 'q' to cancel): ").strip()
    if raw.isdigit() and 1 <= int(raw) <= len(ss):
        return ss[int(raw) - 1]
    print(dim("cancelled"))
    return None


def _confirm(prompt: str, assume_yes: bool = False) -> bool:
    if assume_yes:
        return True
    if not sys.stdin.isatty():
        print(yellow("Refusing to proceed without confirmation (non-interactive). Pass --yes."))
        return False
    return input(f"{prompt} [y/N] ").strip().lower() in ("y", "yes")


def cmd_rescue(args) -> int:
    group = _resolve_one(args.session)
    if not group:
        return 1
    if args.all:
        if not args.to:
            print(red("--all requires --to <dir> (relinks every session of the project there)."))
            return 1
        sessions = group["sessions"]
    else:
        sess = _pick_session(group, "rescue", assume_yes=args.yes)
        if not sess:
            return 1
        sessions = [sess]

    target = args.to
    if not target:
        sess = sessions[0]
        if not sess.orphaned:
            print(green(f"Session is not orphaned — its cwd exists: {sess.cwd}"))
            print(dim("Pass --to <dir> to relink it somewhere else anyway."))
            return 0
        print(bold(f"\nOrphan: {sess.cwd}  {dim('(' + short_id(sess.id) + ')')}"))
        cands = core.candidate_dirs(sess.path, sess.cwd or "", git_branch=sess.git_branch)
        if cands:
            print("\nLikely new homes (by matching file paths referenced in the transcript):")
            for c in cands:
                mark = green("★") if c.hits else dim("·")
                print(f"  {mark} {c.path}  {dim(f'({c.hits} path hits)')}")
            print(f"\n{dim('Re-run with:')} ccsess rescue {short_id(sess.id)} --to {cands[0].path} --apply")
        else:
            print(yellow("No candidate directory found automatically."))
            print(dim(f"Specify the new location: ccsess rescue {short_id(sess.id)} --to <dir> --apply"))
        return 0

    dest_dir = Path(target).expanduser().resolve()
    missing = not dest_dir.is_dir()

    plans = [core.plan_relink(s.path, target, move=args.move, rewrite_paths=not args.no_rewrite)
             for s in sessions]
    print(bold(f"\nRescue plan ({_plural(len(plans), 'session')} → {dest_dir}):"))
    overwrite = False
    for plan in plans:
        print(f"  {'move' if plan.move else 'copy'}: {plan.src}")
        print(f"            → {plan.dest}")
        if plan.dest.exists():
            overwrite = True
            print(yellow("            ⚠ destination already exists — will be overwritten"))
    if plans[0].rewrite_paths:
        print(f"  rewrite cwd: {plans[0].old_cwd}")
        print(f"            → {plans[0].new_cwd}")
    if missing:
        print(yellow(f"\n⚠ target directory does not exist: {dest_dir}")
              + dim("\n  A session can only resume from a real directory — create it or fix --to."))
    if not args.apply:
        print(yellow("\n(dry-run) ") + "re-run with --apply to execute")
        return 0
    if missing:
        print(red("Refusing to apply: target directory does not exist."))
        return 1
    prompt = "\nApply this rescue?" if len(plans) == 1 else f"\nApply these {len(plans)} rescues?"
    if overwrite:
        prompt += " (overwrites existing)"
    if not _confirm(prompt, assume_yes=args.yes):
        print(dim("cancelled"))
        return 1
    for plan in plans:
        res = core.apply_relink(plan)
        print(green(f"✓ {res['dest']}")
              + (dim(f"  ({res['replacements']} path replacements)") if res["replacements"] else ""))
    print(bold("\nResume:"))
    if len(sessions) == 1:
        print(f"  cd {target} && claude --resume {sessions[0].id}")
    else:
        print(f"  cd {target} && claude --resume   " + dim("# then pick from the list"))
    return 0


def cmd_resume(args) -> int:
    group = _resolve_one(args.session)
    if not group:
        return 1
    sess = _pick_session(group, "resume", assume_yes=args.yes)
    if not sess:
        return 1
    p = sess.path

    cwd = os.getcwd()
    plan = core.plan_relink(p, cwd, move=False, rewrite_paths=False)
    if plan.dest.resolve() == p.resolve():
        print(green(f"Already resumable here: claude --resume {p.stem}"))
        return 0
    print(f"Make {dim(short_id(p.stem))} resumable from {bold(cwd)}")
    print(f"  copy {p}\n     → {plan.dest}")
    if not args.apply:
        print(yellow("(dry-run) ") + "re-run with --apply")
        return 0
    if not _confirm("Apply?", assume_yes=args.yes):
        print(dim("cancelled"))
        return 1
    res = core.apply_relink(plan)
    print(green(f"✓ {res['dest']}"))
    print(bold(f"\n  claude --resume {p.stem}"))
    return 0


def _do_search(args) -> list[dict]:
    if idx.DB_PATH.exists():
        return idx.search(args.query, project=args.project, since=args.since, limit=args.limit)
    # fallback: streaming substring scan (no index yet)
    print(dim("(no index yet — streaming scan; run `ccsess index` for fast search)"))
    q = args.query.lower()
    hits = []
    for s in core.iter_sessions():
        if args.project and s.project_name != args.project:
            continue
        found = False
        with s.path.open(encoding="utf-8", errors="replace") as f:
            for line in f:
                if q in line.lower():
                    found = True
                    break
        if found:
            hits.append({"id": s.id, "title": s.title, "project": s.project_name,
                         "cwd": s.cwd, "orphaned": int(s.orphaned),
                         "last_ts": s.last_ts, "snip": "", "hits": 1})
    return hits[: args.limit]


def cmd_search(args) -> int:
    rows = _do_search(args)
    if not rows:
        print(dim("no matches"))
        return 0
    print(bold(f"\n{len(rows)} session(s) matching {cyan(args.query)}\n"))
    for r in rows:
        orphan = red(" ✗orphan") if r.get("orphaned") else ""
        print(f"{dim(short_id(r['id']))}  {bold((r['title'] or '—')[:55])}{orphan}")
        print(f"    {dim(r.get('project') or '')}  {dim(short_ts(r.get('last_ts')))}")
        if r.get("snip"):
            print(f"    {dim(r['snip'][:160])}")
    print(dim(f"\nResume one from here:  ccsess resume <id> --apply"))
    return 0


def cmd_index(args) -> int:
    print(dim("indexing…"))
    res = idx.build(rebuild=args.rebuild)
    print(green(f"✓ indexed {res['indexed']} new/changed, skipped {res['skipped']} unchanged "
                f"({res['total_sessions']} sessions total)"))
    print(dim(f"  db: {idx.DB_PATH}"))
    return 0


def cmd_stats(args) -> int:
    if not idx.DB_PATH.exists():
        print(yellow("No index yet. Run: ccsess index"))
        return 1
    conn = idx.connect()
    n, msgs, size = conn.execute(
        "SELECT COUNT(*), SUM(message_count), SUM(size) FROM sessions").fetchone()
    it, ot, cr, cw, cost = conn.execute(
        "SELECT SUM(input_tokens), SUM(output_tokens), SUM(cache_read_tokens), "
        "SUM(cache_write_tokens), SUM(cost_usd) FROM sessions").fetchone()
    print(bold("\nOverview"))
    print(f"  sessions: {n}   messages: {msgs or 0}   transcripts: {human(size or 0)}")
    print(f"  tokens — in {it or 0:,}  out {ot or 0:,}  "
          f"cache-read {cr or 0:,}  cache-write {cw or 0:,}")
    print(f"  {dim('rough list-price cost estimate:')} ${cost or 0:,.2f}")

    print(bold("\nTop projects"))
    for r in conn.execute(
        "SELECT project, COUNT(*) c, SUM(message_count) m, SUM(cost_usd) cost "
        "FROM sessions GROUP BY project ORDER BY c DESC LIMIT 10"):
        msg = dim(f"{r['m'] or 0:>5} msg")
        print(f"  {r['c']:>3} sess  {msg}  "
              f"${r['cost'] or 0:>7.2f}  {r['project']}")

    print(bold("\nBy model"))
    for r in conn.execute(
        "SELECT model, COUNT(*) c, SUM(output_tokens) o FROM sessions "
        "WHERE model IS NOT NULL GROUP BY model ORDER BY o DESC"):
        print(f"  {r['c']:>3} sess  out {r['o'] or 0:>12,}  {r['model']}")

    orphan_n = conn.execute("SELECT COUNT(*) FROM sessions WHERE orphaned=1").fetchone()[0]
    if orphan_n:
        print(yellow(f"\n⚠ {orphan_n} orphaned session(s) — ccsess doctor"))
    conn.close()
    return 0


def cmd_export(args) -> int:
    group = _resolve_one(args.session)
    if not group:
        return 1
    sess = _pick_session(group, "export", assume_yes=getattr(args, "yes", False))
    if not sess:
        return 1
    p = sess.path
    text = render.to_json(p) if args.format == "json" else render.to_markdown(p)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
        print(green(f"✓ wrote {args.out}"))
    else:
        print(text)
    return 0


def cmd_clean(args) -> int:
    sessions = list(core.iter_sessions())
    _, backups, _ = _classify(sessions)
    empties = core.empty_slug_dirs()
    if not backups and not empties:
        print(green("Nothing to clean — no stale backups or empty folders."))
        return 0

    print(bold("\nWill delete:"))
    if backups:
        print(f"  {_plural(len(backups), 'stale backup transcript')} "
              + dim("(same session resumable elsewhere):"))
        for s in backups[:20]:
            print(f"    {dim('◌')} {s.path}")
        if len(backups) > 20:
            print(dim(f"    … +{len(backups) - 20} more"))
    if empties:
        print(f"  {_plural(len(empties), 'empty slug folder')}:")
        for d in empties[:20]:
            print(f"    {dim('·')} {d}")
        if len(empties) > 20:
            print(dim(f"    … +{len(empties) - 20} more"))

    if not _confirm("\nDelete these?", assume_yes=args.yes):
        print(dim("cancelled"))
        return 1
    nb = nd = 0
    for s in backups:
        try:
            s.path.unlink()
            nb += 1
        except OSError as e:
            print(red(f"  failed {s.path}: {e}"))
    for d in empties:
        try:
            d.rmdir()
            nd += 1
        except OSError as e:
            print(red(f"  failed {d}: {e}"))
    print(green(f"✓ deleted {_plural(nb, 'backup')} and {_plural(nd, 'folder')}"))
    return 0


def cmd_rm(args) -> int:
    group = _resolve_one(args.session)
    if not group:
        return 1
    sess = _pick_session(group, "delete", assume_yes=args.yes)
    if not sess:
        return 1
    print(yellow("\nPermanently delete this transcript:"))
    print(f"  {sess.path}")
    print(dim(f"  {(sess.title or '—')[:60]} · {human(sess.size)} · "
              f"{_plural(sess.message_count, 'message')}"))
    if sess.orphaned:
        print(dim("  (orphaned — its recorded directory no longer exists)"))
    if not _confirm("This cannot be undone. Delete?", assume_yes=args.yes):
        print(dim("cancelled"))
        return 1
    try:
        sess.path.unlink()
        print(green(f"✓ deleted {sess.path}"))
    except OSError as e:
        print(red(f"failed: {e}"))
        return 1
    return 0


# --------------------------------------------------------------------------- #
# argument parser
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="ccsess", description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("scan", help="list projects (one line each), flag orphans")
    s.add_argument("-v", "--verbose", action="store_true",
                   help="expand each project to its per-session detail")
    s.set_defaults(func=cmd_scan)

    s = sub.add_parser("doctor", help="health report + cleanup suggestions")
    s.set_defaults(func=cmd_doctor)

    # follow-up commands accept a scan number, a project name, or a session id/prefix
    target_help = "scan number, project name, or session id/prefix"

    s = sub.add_parser("rescue", help="relink an orphaned session into a real directory")
    s.add_argument("session", help=target_help)
    s.add_argument("--to", help="target directory to relink into")
    s.add_argument("--all", action="store_true",
                   help="relink every session of the project (requires --to)")
    s.add_argument("--apply", action="store_true", help="execute (default: dry-run)")
    s.add_argument("--move", action="store_true", help="move instead of copy")
    s.add_argument("--no-rewrite", action="store_true", help="don't rewrite internal paths")
    s.add_argument("-y", "--yes", action="store_true", help="skip the confirmation prompt")
    s.set_defaults(func=cmd_rescue)

    s = sub.add_parser("resume", help="make a session resumable from the current dir")
    s.add_argument("session", help=target_help)
    s.add_argument("--apply", action="store_true", help="execute (default: dry-run)")
    s.add_argument("-y", "--yes", action="store_true", help="skip the confirmation prompt")
    s.set_defaults(func=cmd_resume)

    s = sub.add_parser("clean", help="delete stale backups and empty slug folders")
    s.add_argument("-y", "--yes", action="store_true", help="skip the confirmation prompt")
    s.set_defaults(func=cmd_clean)

    s = sub.add_parser("rm", help="permanently delete a session transcript")
    s.add_argument("session", help=target_help)
    s.add_argument("-y", "--yes", action="store_true", help="skip the confirmation prompt")
    s.set_defaults(func=cmd_rm)

    s = sub.add_parser("search", help="full-text search across all sessions")
    s.add_argument("query")
    s.add_argument("--project")
    s.add_argument("--since", help="ISO date lower bound, e.g. 2026-01-01")
    s.add_argument("--limit", type=int, default=20)
    s.set_defaults(func=cmd_search)

    s = sub.add_parser("index", help="(re)build the SQLite+FTS5 index")
    s.add_argument("--rebuild", action="store_true", help="drop and rebuild from scratch")
    s.set_defaults(func=cmd_index)

    s = sub.add_parser("stats", help="usage/token/cost rollups from the index")
    s.set_defaults(func=cmd_stats)

    s = sub.add_parser("export", help="export a transcript as markdown or json")
    s.add_argument("session", help=target_help)
    s.add_argument("--format", choices=["md", "json"], default="md")
    s.add_argument("--out", help="write to file instead of stdout")
    s.set_defaults(func=cmd_export)
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
