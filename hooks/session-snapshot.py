#!/usr/bin/env python3
"""
session-snapshot.py — Stop Hook for Claude Code

Captures a structured session state snapshot when a Claude Code session ends.
Acts as a safety net: if a session dies without a manual wrap, the next session
can read this file to recover context.

WHY THIS EXISTS
  Long sessions end unexpectedly — terminal crashes, context overflows, network
  drops. When that happens without a deliberate "wrap" step, planning context
  and in-progress work state is lost. This hook auto-captures the essentials
  on every session exit, silently.

WHAT IS CAPTURED
  - Recent user messages (last 10 unique, from transcript tail)
  - Git state (branch, staged/unstaged changes, untracked file count)
  - Active plan files (titles from ~/.claude/plans/*.md)
  - Tool usage summary for the session

OUTPUT FILES
  SNAPSHOT_DIR/latest.md   — current session snapshot (overwritten each time)
  SNAPSHOT_DIR/previous.md — previous session snapshot (rotated from latest)

CONFIGURATION (environment variables)
  SNAPSHOT_DIR   Directory to write snapshots (default: ~/.claude/snapshots)

  The hook also reads these standard Claude Code env vars automatically:
    CLAUDE_SESSION_ID    — session UUID
    CLAUDE_PROJECT_DIR   — project root directory

FAIL-OPEN
  This hook never blocks session exit. All errors are silently suppressed.
  The snapshot is best-effort.

INSTALLATION
  See README.md for settings.json snippet (Stop hook type).
"""

import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

# --- Configuration ---
SESSION_ID   = os.environ.get("CLAUDE_SESSION_ID", "unknown")
PROJECT_DIR  = os.environ.get("CLAUDE_PROJECT_DIR", os.getcwd())
SNAPSHOT_DIR = Path(os.environ.get("SNAPSHOT_DIR", os.path.join(os.path.expanduser("~"), ".claude", "snapshots")))

# Claude Code stores transcripts here
TRANSCRIPT_DIR = Path.home() / ".claude" / "projects"

TAIL_LINES = 200  # lines to read from end of transcript


def run_cmd(cmd, cwd=None):
    """Run a shell command, return stdout or empty string on failure."""
    try:
        r = subprocess.run(
            cmd, capture_output=True, text=True, timeout=10,
            cwd=cwd or PROJECT_DIR, shell=True
        )
        return r.stdout.strip()
    except Exception:
        return ""


def get_git_summary():
    """Return a markdown summary of current git state."""
    branch    = run_cmd("git branch --show-current")
    diff_stat = run_cmd("git diff --stat HEAD")
    staged    = run_cmd("git diff --cached --stat")
    untracked = run_cmd("git ls-files --others --exclude-standard")

    lines = []
    if branch:
        lines.append(f"**Branch:** `{branch}`")
    if diff_stat:
        lines.append(f"**Unstaged changes:**\n```\n{diff_stat}\n```")
    if staged:
        lines.append(f"**Staged changes:**\n```\n{staged}\n```")
    if untracked:
        count = len(untracked.splitlines())
        lines.append(f"**Untracked files:** {count}")

    return "\n".join(lines) if lines else "*No git changes detected.*"


def get_active_plans():
    """Return markdown list of active plan files."""
    plans_dir = Path.home() / ".claude" / "plans"
    if not plans_dir.exists():
        return "*No active plans.*"
    plans = list(plans_dir.glob("*.md"))
    if not plans:
        return "*No active plans.*"
    lines = []
    for p in plans:
        try:
            first_line = p.read_text(encoding="utf-8").split("\n")[0]
            title = re.sub(r"^#+\s*", "", first_line).strip() or p.stem
            lines.append(f"- `{p.name}` — {title}")
        except Exception:
            lines.append(f"- `{p.name}`")
    return "\n".join(lines)


def find_transcript():
    """Find the transcript file for this session."""
    # Claude Code stores transcripts in ~/.claude/projects/<encoded-path>/<session_id>.jsonl
    for candidate in TRANSCRIPT_DIR.rglob(f"{SESSION_ID}.jsonl"):
        return candidate
    return None


def extract_session_summary(transcript_path):
    """Extract recent user messages and tool usage from transcript tail."""
    if not transcript_path or not transcript_path.exists():
        return [], []

    try:
        with open(transcript_path, "r", encoding="utf-8") as f:
            all_lines = f.readlines()
    except Exception:
        return [], []

    tail = all_lines[-TAIL_LINES:] if len(all_lines) > TAIL_LINES else all_lines

    user_messages = []
    tools_used    = set()

    for line in tail:
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue

        role = entry.get("role", "")

        if role == "user":
            content = entry.get("content", "")
            if isinstance(content, str) and len(content) > 5:
                if not content.startswith("{") and "<tool_result>" not in content:
                    user_messages.append(content[:200].replace("\n", " "))
            elif isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "text":
                        text = block.get("text", "")
                        if len(text) > 5 and not text.startswith("{"):
                            user_messages.append(text[:200].replace("\n", " "))

        if role == "assistant":
            content = entry.get("content", [])
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "tool_use":
                        tools_used.add(block.get("name", ""))

    # Deduplicate, most recent first, limit 10
    seen, unique = set(), []
    for msg in reversed(user_messages):
        key = msg[:80]
        if key not in seen:
            seen.add(key)
            unique.append(msg)
        if len(unique) >= 10:
            break
    unique.reverse()

    return unique, sorted(tools_used)


def main():
    try:
        SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)

        latest   = SNAPSHOT_DIR / "latest.md"
        previous = SNAPSHOT_DIR / "previous.md"

        # Rotate: latest → previous
        if latest.exists():
            try:
                previous.write_text(latest.read_text(encoding="utf-8"), encoding="utf-8")
            except Exception:
                pass

        git_summary  = get_git_summary()
        active_plans = get_active_plans()
        transcript   = find_transcript()
        user_msgs, tools_used = extract_session_summary(transcript)

        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

        lines = [
            f"# Session Snapshot — {SESSION_ID[:8]}",
            f"*Auto-captured: {now}*",
            f"*Session: `{SESSION_ID}`*",
            f"*Project: `{PROJECT_DIR}`*",
            "",
            "## Recent User Messages",
        ]

        if user_msgs:
            for msg in user_msgs:
                lines.append(f"- {msg[:150]}")
        else:
            lines.append("*No user messages captured.*")

        lines.extend([
            "",
            "## Git State",
            git_summary,
            "",
            "## Active Plans",
            active_plans,
        ])

        if tools_used:
            lines.extend([
                "",
                "## Tools Used This Session",
                ", ".join(tools_used),
            ])

        lines.extend([
            "",
            "---",
            "*Auto-generated by session-snapshot hook. Read this at the start of the next session to recover context.*",
        ])

        latest.write_text("\n".join(lines), encoding="utf-8")

    except Exception:
        pass  # Fail-open: never block session exit


if __name__ == "__main__":
    main()
