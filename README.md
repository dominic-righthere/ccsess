# ccsess

A small, zero-dependency CLI to **rescue, search, resume, and mine Claude Code sessions**.

Claude Code stores every session as `~/.claude/projects/<slug>/<id>.jsonl`, where `<slug>`
is the working-directory path with `/` replaced by `-`. Because the storage location is
derived purely from the path, **renaming or moving a project directory orphans its
sessions** — `claude --resume` looks under the new slug and finds nothing, even though the
transcript is still on disk. `ccsess` finds those orphans and relinks them, searches across
every transcript regardless of folder, and indexes the whole corpus for insights.

> Native `/cd` (Claude Code v2.1.169+) relocates a **live** session. `ccsess` handles what
> `/cd` can't: sessions that were **already** orphaned, cross-folder search, and analysis.

## Install / run

Built with [uv](https://docs.astral.sh/uv/) (no pip). From the repo:

```bash
uv run ccsess --help
```

Install it on your PATH as a tool:

```bash
uv tool install --editable .
ccsess --help
```

## Commands

| Command | What it does |
|---|---|
| `ccsess scan [-v]` | **One line per project**, grouped by status (orphan / backup / ok) and numbered. `-v` expands each project to its sessions. |
| `ccsess doctor` | Health report: orphans, backups, empty folders, largest sessions. |
| `ccsess rescue <target> [--to DIR] [--all] [--apply] [--move] [-y]` | Relink an orphaned session into a real directory. Without `--to`, suggests likely homes; `--all` relinks **every** session of the project. |
| `ccsess resume <target> [--apply] [-y]` | Make any session resumable from the **current** directory. |
| `ccsess clean [-y]` | Delete stale backups + empty slug folders (the things `doctor` flags). Prompts first. |
| `ccsess rm <target> [-y]` | Permanently delete one session transcript. Prompts first. |
| `ccsess search "<text>" [--project X] [--since DATE] [--no-cache]` | Full-text search across **all** transcripts, wherever they live. |
| `ccsess index [--rebuild] [--clear]` | Build the **optional** persistent search cache (incremental); `--clear` deletes it. |
| `ccsess stats [--no-cache]` | Per-project / per-model rollups, token totals, rough cost estimate. |
| `ccsess export <target> [--format md\|json] [--out FILE]` | Render a transcript for archiving or as a memory seed. |

A **`<target>`** is a **scan number** (e.g. `2`), a **project name** (e.g. `enquire`), or a
**session id / prefix**. Numbers match what `ccsess scan` shows and are recomputed on demand, so
they work without any saved state. When a project has several sessions, the command lists them so
you can pick one.

## Typical flows

**Rescue a moved project's session:**
```bash
ccsess scan                       # numbered, orphans first
ccsess rescue 2                   # by scan number (or: ccsess rescue enquire)
ccsess rescue 2 --to ~/work/dev/newname --apply   # confirms, then relinks
cd ~/work/dev/newname && claude --resume <id>
```

**"I know we discussed X but can't find the session":**
```bash
ccsess index                      # once (re-run is incremental)
ccsess search "stripe webhook signature"
ccsess resume <id> --apply        # then: claude --resume <id>
```

## Safety

- **Dry-run by default.** `rescue`/`resume` only write with `--apply`, and then **prompt for
  confirmation** before touching anything (`-y/--yes` skips the prompt for scripts).
- **Copy, not move.** Originals are preserved unless you pass `--move`.
- **Validated rewrites.** When relinking rewrites the old working-directory path to the new
  one, every line is re-parsed as JSON before and after, and the destination is backed up
  (`.jsonl.bak`) first.
- **Deletion is confirmed.** `clean` and `rm` are the only commands that delete, and both
  prompt first (and refuse entirely when run non-interactively without `-y`). `doctor` still
  only *suggests*. `clean` removes copies that are resumable elsewhere and folders that are
  already empty; `rm` permanently removes a single transcript.

## How it works

- A session's real working directory comes from the `cwd` field inside the transcript (the
  folder slug is lossy). `slug_for(path) = path.replace('/', '-')`.
- Orphan detection: a session is **orphaned** when its recorded `cwd` no longer exists. It's a
  **stale backup** (not a true orphan) when another file with the same id is still resumable.
- Auto-finding a moved project's new home: `rescue` extracts the file paths the transcript
  referenced under the old `cwd`, then scores candidate directories by how many of those
  paths exist under them (plus basename/branch match).
- Only top-level `*/*.jsonl` files are treated as sessions; nested `subagents/*.jsonl` and
  plugin logs are intentionally excluded from rescue/resume.

## Development

The CLI itself has **zero runtime dependencies**; tests use `pytest` (a dev-only group).

```bash
uv run pytest          # run the suite
uv build               # build sdist + wheel
```

Tests live in `tests/` and cover slug encoding, title/whitespace handling, orphan vs
stale-backup detection, the width-responsive `scan` output, target resolution (index / name /
id), and the relink path-rewrite — including absolute-path normalization of a relative `--to`.

## Notes

- **ccsess writes nothing by default.** `scan`/`doctor`/`rescue`/`resume`/`export` read transcripts
  directly. `search` and `stats` build a throwaway in-memory index per run (writes nothing); pass
  `--no-cache` to force that even when a cache exists.
- **The index is an opt-in cache.** Run `ccsess index` to persist `ccsess.db` for instant repeat
  searches; `ccsess index --clear` deletes it. The index full-text-stores conversation prose and
  tool output, **capping each tool result at ~2KB** so big logs/dumps stay findable without bloat.
- Scan **numbers are recomputed** on demand (same ordering `scan` shows) — no cache file.
- `stats` cost figures are a **rough list-price approximation** (cache reads/writes included)
  for relative comparison, not a billing statement.
- Storage location respects **`$CLAUDE_CONFIG_DIR`** (the same override Claude Code uses),
  falling back to `~/.claude`, where the optional `ccsess.db` lives.
