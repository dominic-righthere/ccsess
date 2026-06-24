"""Render a transcript as readable Markdown (for archiving or memory seeds)."""

from __future__ import annotations

import json
from pathlib import Path

from .core import iter_lines, read_session


def _blocks(content) -> list[str]:
    if isinstance(content, str):
        return [content] if content.strip() else []
    if not isinstance(content, list):
        return []
    out: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        bt = block.get("type")
        if bt == "text":
            out.append(block.get("text", ""))
        elif bt == "thinking":
            out.append("> _(thinking)_ " + (block.get("thinking", "")[:500]))
        elif bt == "tool_use":
            inp = json.dumps(block.get("input", {}), ensure_ascii=False)
            out.append(f"**🔧 {block.get('name')}** `{inp[:300]}`")
        elif bt == "tool_result":
            c = block.get("content")
            if isinstance(c, list):
                c = "\n".join(b.get("text", "") for b in c if isinstance(b, dict))
            txt = (c or "")[:600] if isinstance(c, str) else ""
            out.append(f"```\n{txt}\n```")
    return [o for o in out if o.strip()]


def to_markdown(path: Path) -> str:
    sess = read_session(path)
    lines = [
        f"# {sess.title or sess.id}",
        "",
        f"- **session**: `{sess.id}`",
        f"- **project**: {sess.cwd or sess.slug_dir}",
        f"- **branch**: {sess.git_branch or '—'}  •  **version**: {sess.version or '—'}",
        f"- **messages**: {sess.message_count}  •  **{sess.first_ts or '?'} → {sess.last_ts or '?'}**",
        "",
        "---",
        "",
    ]
    for d in iter_lines(path):
        t = d.get("type")
        if t not in ("user", "assistant"):
            continue
        msg = d.get("message") or {}
        blocks = _blocks(msg.get("content"))
        if not blocks:
            continue
        who = "🧑 User" if t == "user" else "🤖 Assistant"
        lines.append(f"### {who}")
        lines.append("")
        lines.extend(blocks)
        lines.append("")
    return "\n".join(lines)


def to_json(path: Path) -> str:
    sess = read_session(path)
    msgs = []
    for d in iter_lines(path):
        if d.get("type") in ("user", "assistant"):
            msgs.append({
                "role": d.get("type"),
                "ts": d.get("timestamp"),
                "content": (d.get("message") or {}).get("content"),
            })
    return json.dumps(
        {
            "id": sess.id, "cwd": sess.cwd, "title": sess.title,
            "git_branch": sess.git_branch, "version": sess.version,
            "messages": msgs,
        },
        ensure_ascii=False, indent=2,
    )
