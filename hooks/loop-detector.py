#!/usr/bin/env python3
"""
loop-detector.py — PostToolUse Hook for Claude Code

Detects agent loops before they burn through your context window and budget.
Uses three independent strategies run on every tool call:

  1. TOOL REPETITION  — same (tool, args) called N times in a row
  2. ERROR REPETITION — same error output repeated across calls
  3. STALL DETECTION  — many tool calls with no file modifications

Fires WARN at lower thresholds (injects message into model context).
Fires STOP at higher thresholds (exit 2 — blocks next tool call).

WHY THIS EXISTS
  Agents in long sessions sometimes get stuck: they call the same Bash
  command repeatedly, hit the same error in a loop, or read files endlessly
  without making progress. This hook catches all three patterns and forces
  the model to reconsider its approach.

CONFIGURATION (environment variables)
  LOOP_DETECTOR_ENABLED         1 to enable, 0 to disable (default: 1)
  LOOP_DETECTOR_WINDOW          How many recent calls to look back (default: 20)
  LOOP_DETECTOR_WARN_THRESHOLD  Repetitions before warning (default: 3)
  LOOP_DETECTOR_STOP_THRESHOLD  Repetitions before blocking (default: 5)
  LOOP_DETECTOR_STALL_WARN      Calls-without-file-edit before warning (default: 10)
  LOOP_DETECTOR_STALL_STOP      Calls-without-file-edit before blocking (default: 20)

STATE
  Per-session JSONL files in ~/.claude/loop-detector/
  Incidents log at ~/.claude/loop-detector/incidents.log
  State files older than 24h are auto-cleaned.

SCOPE
  Only activates for agent/subagent sessions. Interactive (non-agent)
  sessions pass through silently — you don't want this interrupting
  normal exploratory use.

INSTALLATION
  See README.md for settings.json snippet.
"""

import sys
import json
import os
import hashlib
import time
import re

# --- Configuration (env-var overrides) ---
ENABLED        = os.environ.get("LOOP_DETECTOR_ENABLED", "1") == "1"
WINDOW_SIZE    = int(os.environ.get("LOOP_DETECTOR_WINDOW", "20"))
WARN_THRESHOLD = int(os.environ.get("LOOP_DETECTOR_WARN_THRESHOLD", "3"))
STOP_THRESHOLD = int(os.environ.get("LOOP_DETECTOR_STOP_THRESHOLD", "5"))
STALL_WARN     = int(os.environ.get("LOOP_DETECTOR_STALL_WARN", "10"))
STALL_STOP     = int(os.environ.get("LOOP_DETECTOR_STALL_STOP", "20"))

# Read-only tools: exempt from tool-repetition check (reading the same file is fine)
EXEMPT_TOOLS         = {"Read", "Grep", "Glob"}
# Tools that count as "making progress" for stall detection
FILE_MODIFYING_TOOLS = {"Edit", "Write"}

STATE_DIR     = os.path.join(os.path.expanduser("~"), ".claude", "loop-detector")
INCIDENTS_LOG = os.path.join(STATE_DIR, "incidents.log")
STATE_MAX_AGE = 86400  # 24 hours


def ensure_state_dir():
    os.makedirs(STATE_DIR, exist_ok=True)


def cleanup_old_state_files():
    """Remove state files older than STATE_MAX_AGE to avoid accumulation."""
    try:
        now = time.time()
        for fname in os.listdir(STATE_DIR):
            if not fname.endswith(".jsonl"):
                continue
            fpath = os.path.join(STATE_DIR, fname)
            try:
                if now - os.path.getmtime(fpath) > STATE_MAX_AGE:
                    os.remove(fpath)
            except OSError:
                pass
    except OSError:
        pass


def state_file_path(session_id):
    safe = re.sub(r"[^\w\-]", "_", session_id)[:64]
    return os.path.join(STATE_DIR, f"{safe}.jsonl")


def load_state(path):
    if not os.path.isfile(path):
        return []
    records = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    except OSError:
        pass
    return records


def append_record(path, record):
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")
    except OSError:
        # If append fails, try truncate-and-write as fallback
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(json.dumps(record) + "\n")
        except OSError:
            pass


def log_incident(session_id, kind, message):
    try:
        entry = {
            "ts":         time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "session_id": session_id,
            "kind":       kind,
            "message":    message,
        }
        with open(INCIDENTS_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
    except OSError:
        pass


def hash_args(tool_input):
    """Stable hash of tool arguments for repetition detection."""
    try:
        canon = json.dumps(tool_input, sort_keys=True, ensure_ascii=False)
    except (TypeError, ValueError):
        canon = str(tool_input)
    return hashlib.md5(canon.encode("utf-8")).hexdigest()[:12]


def normalize_error(text):
    """Collapse whitespace and truncate for fingerprinting."""
    if not text:
        return ""
    return re.sub(r"\s+", " ", text).strip()[:200]


def is_agent_session(hook_input):
    """Returns True if this looks like a subagent (not interactive) session."""
    if os.environ.get("CLAUDE_AGENT_ID"):
        return True
    if os.environ.get("CLAUDE_IS_SUBAGENT", "").lower() in ("1", "true", "yes"):
        return True
    if hook_input.get("parent_tool_use_id"):
        return True
    if hook_input.get("agent_id"):
        return True
    return False


def check_tool_repetition(records, tool_name, args_hash):
    """Count (tool, args) repeats in recent window. Returns (count, stop, msg)."""
    if tool_name in EXEMPT_TOOLS:
        return 0, False, None

    window = records[-WINDOW_SIZE:]
    count = sum(
        1 for r in window
        if r.get("tool_name") == tool_name and r.get("args_hash") == args_hash
    )
    count += 1  # include current call

    if count >= STOP_THRESHOLD:
        msg = (
            f"LOOP STOP: {tool_name} called with identical arguments "
            f"{count} times. Halting to prevent runaway loop. "
            "Try a completely different approach or tool."
        )
        return count, True, msg

    if count >= WARN_THRESHOLD:
        msg = (
            f"LOOP WARNING: {tool_name} called with identical arguments "
            f"{count} times. This may be a loop — try a different approach."
        )
        return count, False, msg

    return count, False, None


def check_error_repetition(records, error_raw):
    """Check if the same error has occurred repeatedly. Returns (count, msg)."""
    if not error_raw:
        return 0, None

    fingerprint = normalize_error(error_raw)
    if not fingerprint:
        return 0, None

    window = records[-10:]
    count = sum(1 for r in window if r.get("error_fingerprint") == fingerprint)
    count += 1

    if count >= WARN_THRESHOLD:
        msg = (
            f"LOOP WARNING: The same error has occurred {count} times in a row. "
            "Repeating the same action will not fix this — try a different approach."
        )
        return count, msg

    return count, None


def check_stall(records, tool_name, file_mod_success):
    """Count tool calls since last successful file modification. Returns (count, stop, msg)."""
    if file_mod_success:
        return 0, False, None

    calls_since = 0
    found_mod = False
    for r in reversed(records):
        if r.get("tool_name") in FILE_MODIFYING_TOOLS and r.get("file_mod_success"):
            found_mod = True
            break
        calls_since += 1

    if not found_mod:
        calls_since = len(records)

    calls_since += 1  # include current call

    if calls_since >= STALL_STOP:
        msg = (
            f"LOOP STOP: {calls_since} tool calls with no successful file "
            "modifications. Session appears stalled — stop and re-evaluate your plan."
        )
        return calls_since, True, msg

    if calls_since >= STALL_WARN:
        msg = (
            f"LOOP WARNING: {calls_since} tool calls since last successful "
            "file edit. Are you making progress?"
        )
        return calls_since, False, msg

    return calls_since, False, None


def main():
    try:
        raw = sys.stdin.read()
        if not raw.strip():
            sys.exit(0)
        hook_input = json.loads(raw)
    except (json.JSONDecodeError, Exception):
        sys.exit(0)

    if not ENABLED:
        sys.exit(0)

    if not is_agent_session(hook_input):
        sys.exit(0)

    session_id  = hook_input.get("session_id", "unknown")
    tool_name   = hook_input.get("tool_name", "")
    tool_input  = hook_input.get("tool_input") or {}
    error_raw   = hook_input.get("error") or ""
    tool_output = hook_input.get("tool_output") or ""

    succeeded = not bool(error_raw)
    file_mod_success = (
        tool_name in FILE_MODIFYING_TOOLS
        and succeeded
        and "error" not in str(tool_output).lower()[:50]
    )

    ensure_state_dir()
    cleanup_old_state_files()

    spath   = state_file_path(session_id)
    records = load_state(spath)

    args_hash         = hash_args(tool_input)
    error_fingerprint = normalize_error(str(error_raw)) if error_raw else ""

    stop_code    = False
    warning_msgs = []

    _, tool_stop, tool_msg = check_tool_repetition(records, tool_name, args_hash)
    if tool_msg:
        warning_msgs.append(tool_msg)
    if tool_stop:
        stop_code = True

    _, err_msg = check_error_repetition(records, str(error_raw) if error_raw else "")
    if err_msg:
        warning_msgs.append(err_msg)

    _, stall_stop, stall_msg = check_stall(records, tool_name, file_mod_success)
    if stall_msg:
        warning_msgs.append(stall_msg)
    if stall_stop:
        stop_code = True

    record = {
        "ts":                time.time(),
        "tool_name":         tool_name,
        "args_hash":         args_hash,
        "error_fingerprint": error_fingerprint,
        "file_mod_success":  file_mod_success,
    }
    append_record(spath, record)

    if warning_msgs:
        combined = " | ".join(warning_msgs)
        log_incident(session_id, "STOP" if stop_code else "WARN", combined)
        print(json.dumps({"result": combined}))

    sys.exit(2 if stop_code else 0)


if __name__ == "__main__":
    main()
