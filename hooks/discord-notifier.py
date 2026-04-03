#!/usr/bin/env python3
"""
discord-notifier.py — PostToolUse Hook for Claude Code

Sends Discord webhook notifications when agents complete tasks, hit errors,
or context usage climbs past a warning threshold.

WHY THIS EXISTS
  In multi-agent workflows you often have background tasks running. You
  can't watch every terminal. This hook pings Discord so you know when
  something needs attention without polling.

EVENTS FIRED
  - Task completion   — agent finishes a recognized task pattern
  - Error / blocker   — tool result contains an error or failure signal
  - Context warning   — context usage exceeds NOTIFY_CONTEXT_THRESHOLD (default 70%)

CONFIGURATION (environment variables)
  DISCORD_WEBHOOK_URL       Webhook URL to POST to (required)
  NOTIFY_ON_COMPLETE        Fire on task completion  (default: 1)
  NOTIFY_ON_ERROR           Fire on errors/blockers  (default: 1)
  NOTIFY_ON_CONTEXT_WARN    Fire on context >70%     (default: 0)
  NOTIFY_CONTEXT_THRESHOLD  Context % to warn at     (default: 0.70)
  CONTEXT_WINDOW_SIZE       Token budget             (default: 200000)
  DISCORD_AGENT_NAME        Override agent name label (default: auto-detected)

SETTINGS.JSON SNIPPET
  Add to your PostToolUse hooks array (all tools, empty matcher):

  {
    "matcher": "",
    "hooks": [
      {
        "type": "command",
        "command": "DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/... python3 ~/.claude/hookkit/discord-notifier.py",
        "timeout": 10
      }
    ]
  }

  Or set DISCORD_WEBHOOK_URL in your shell environment and just reference the script.
"""

import sys
import json
import os
import re
import urllib.request
import urllib.error
import time

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

WEBHOOK_URL            = os.environ.get("DISCORD_WEBHOOK_URL", "")
NOTIFY_ON_COMPLETE     = os.environ.get("NOTIFY_ON_COMPLETE",     "1") == "1"
NOTIFY_ON_ERROR        = os.environ.get("NOTIFY_ON_ERROR",        "1") == "1"
NOTIFY_ON_CONTEXT_WARN = os.environ.get("NOTIFY_ON_CONTEXT_WARN", "0") == "1"
CONTEXT_THRESHOLD      = float(os.environ.get("NOTIFY_CONTEXT_THRESHOLD", "0.70"))
CONTEXT_WINDOW         = int(os.environ.get("CONTEXT_WINDOW_SIZE",        "200000"))
AGENT_NAME_OVERRIDE    = os.environ.get("DISCORD_AGENT_NAME", "")

# ---------------------------------------------------------------------------
# Completion signal patterns (checked against tool result text)
# ---------------------------------------------------------------------------

COMPLETION_PATTERNS = [
    r"\btask complete\b",
    r"\ball done\b",
    r"\bfinished\b",
    r"\bcompleted successfully\b",
    r"\bdelivered\b",
    r"\bpull request created\b",
    r"\bcommit pushed\b",
    r"\bdeploy(ment)? (complete|succeeded|finished)\b",
    r"\bbuild (complete|succeeded|finished|passed)\b",
    r"\btests? (pass(ed)?|succeeded)\b",
    r"\bwrap(ping)? up\b",
]

# Error/blocker signal patterns
ERROR_PATTERNS = [
    r"\berror\b",
    r"\bfailed?\b",
    r"\bfailure\b",
    r"\bexception\b",
    r"\btraceback\b",
    r"\bblocker\b",
    r"\bblocked\b",
    r"\bcannot proceed\b",
    r"\bnot found\b",
    r"\bpermission denied\b",
    r"\bsyntax error\b",
    r"\bimport error\b",
    r"\bmodule not found\b",
    r"\btimeout\b",
    r"\bconnection refused\b",
    r"\bneed(s)? attention\b",
    r"\bneed(s)? (your )?input\b",
    r"\bstuck\b",
]

# Tool names that carry task-completion semantics worth surfacing
COMPLETION_TOOLS = {"TodoWrite", "mcp__claude-in-chrome__update_plan"}

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def get_agent_name(hook_input: dict) -> str:
    """Best-effort agent name from env, session ID, or tool context."""
    if AGENT_NAME_OVERRIDE:
        return AGENT_NAME_OVERRIDE
    session_id = hook_input.get("session_id", "")
    if session_id:
        return f"agent-{session_id[:8]}"
    return "claude-agent"


def truncate(text: str, max_chars: int = 300) -> str:
    if len(text) <= max_chars:
        return text
    return text[:max_chars - 1] + "…"


def matches_any(text: str, patterns: list) -> bool:
    text_lower = text.lower()
    for pat in patterns:
        if re.search(pat, text_lower):
            return True
    return False


def get_context_usage(transcript_path: str):
    """
    Return (used_tokens, pct) by reading transcript tail.
    Returns (None, None) if unavailable.
    Mirrors logic from context-monitor.py.
    """
    TAIL_BYTES = 64 * 1024
    last_usage = None
    try:
        file_size = os.path.getsize(transcript_path)
        with open(transcript_path, "r", encoding="utf-8") as f:
            if file_size > TAIL_BYTES:
                f.seek(file_size - TAIL_BYTES)
                f.readline()
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if entry.get("isSidechain") or entry.get("isApiErrorMessage"):
                    continue
                msg = entry.get("message", {})
                if isinstance(msg, dict) and "usage" in msg:
                    last_usage = msg["usage"]
    except (OSError, IOError):
        return None, None

    if not last_usage:
        return None, None

    total = (
        last_usage.get("input_tokens", 0)
        + last_usage.get("cache_creation_input_tokens", 0)
        + last_usage.get("cache_read_input_tokens", 0)
    )
    return total, total / CONTEXT_WINDOW


def context_marker_path(transcript_path: str) -> str:
    return transcript_path + f".discord-warned-{int(CONTEXT_THRESHOLD * 100)}"


def post_to_discord(payload: dict) -> bool:
    """POST a message payload to Discord. Returns True on success."""
    if not WEBHOOK_URL:
        return False
    try:
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            WEBHOOK_URL,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=8) as resp:
            return resp.status in (200, 204)
    except (urllib.error.URLError, urllib.error.HTTPError, OSError):
        return False


def build_embed(title: str, description: str, color: int, agent: str, tool: str) -> dict:
    ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    return {
        "embeds": [
            {
                "title": title,
                "description": description,
                "color": color,
                "fields": [
                    {"name": "Agent",     "value": agent, "inline": True},
                    {"name": "Tool",      "value": tool,  "inline": True},
                ],
                "footer": {"text": "HookKit · discord-notifier"},
                "timestamp": ts,
            }
        ]
    }

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    if not WEBHOOK_URL:
        sys.exit(0)

    try:
        raw = sys.stdin.read()
        if not raw.strip():
            sys.exit(0)
        hook_input = json.loads(raw)
    except (json.JSONDecodeError, Exception):
        sys.exit(0)

    tool_name   = hook_input.get("tool_name", "")
    tool_result = hook_input.get("tool_result", {})
    transcript  = hook_input.get("transcript_path", "")
    agent       = get_agent_name(hook_input)

    # tool_result may be a dict or a string depending on the hook payload version
    if isinstance(tool_result, dict):
        result_text = tool_result.get("output", "") or tool_result.get("content", "") or ""
        is_error    = bool(tool_result.get("is_error")) or tool_result.get("type") == "error"
    elif isinstance(tool_result, str):
        result_text = tool_result
        is_error    = False
    else:
        result_text = str(tool_result)
        is_error    = False

    notified = False

    # ------------------------------------------------------------------
    # 1. Error / blocker detection
    # ------------------------------------------------------------------
    if NOTIFY_ON_ERROR and not notified:
        fire_error = is_error or matches_any(result_text, ERROR_PATTERNS)
        # Suppress false positives from short/clean results
        if fire_error and (is_error or len(result_text) > 30):
            summary = truncate(result_text) if result_text else "(no output)"
            payload = build_embed(
                title       = "Agent needs attention",
                description = f"**Tool:** `{tool_name}`\n\n```\n{summary}\n```",
                color       = 0xE74C3C,  # red
                agent       = agent,
                tool        = tool_name,
            )
            post_to_discord(payload)
            notified = True

    # ------------------------------------------------------------------
    # 2. Task completion detection
    # ------------------------------------------------------------------
    if NOTIFY_ON_COMPLETE and not notified:
        completion_tool_hit = tool_name in COMPLETION_TOOLS
        completion_text_hit = matches_any(result_text, COMPLETION_PATTERNS)

        if completion_tool_hit or completion_text_hit:
            summary = truncate(result_text) if result_text else "(no output)"
            payload = build_embed(
                title       = "Agent task complete",
                description = f"**Tool:** `{tool_name}`\n\n```\n{summary}\n```",
                color       = 0x2ECC71,  # green
                agent       = agent,
                tool        = tool_name,
            )
            post_to_discord(payload)
            notified = True

    # ------------------------------------------------------------------
    # 3. Context warning
    # ------------------------------------------------------------------
    if NOTIFY_ON_CONTEXT_WARN and transcript:
        marker = context_marker_path(transcript)
        if not os.path.exists(marker):
            used, pct = get_context_usage(transcript)
            if pct is not None and pct >= CONTEXT_THRESHOLD:
                try:
                    with open(marker, "w") as f:
                        f.write(f"{used}/{CONTEXT_WINDOW}={pct:.2%}")
                except OSError:
                    pass
                payload = build_embed(
                    title       = f"Context at {pct:.0%} — action needed",
                    description = (
                        f"Session context is **{pct:.0%}** used "
                        f"({used:,} / {CONTEXT_WINDOW:,} tokens).\n"
                        "Consider wrapping, compacting, or delegating."
                    ),
                    color       = 0xF39C12,  # orange
                    agent       = agent,
                    tool        = tool_name,
                )
                post_to_discord(payload)

    sys.exit(0)


if __name__ == "__main__":
    main()
