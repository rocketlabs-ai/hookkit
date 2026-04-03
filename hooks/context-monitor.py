#!/usr/bin/env python3
"""
context-monitor.py — PostToolUse Hook for Claude Code

Fires tiered warnings as your session context window fills up, giving you
time to wrap, compact, or delegate before autocompaction hits.

WHY THIS EXISTS
  Claude Code auto-compacts at ~80% context usage, which silently discards
  planning context. By the time you notice, you've lost important state.
  This hook warns you at 50%, 65%, and 75% so you can act deliberately.

HOW IT WORKS
  Reads the tail of the session JSONL transcript to find the most recent
  usage entry. Compares against a configurable window size. Each threshold
  fires exactly once per session (marker files prevent repeat warnings).

THRESHOLDS
  50% — info notice, one-time
  65% — warning: consider wrapping or delegating
  75% — critical: autocompaction imminent

CONFIGURATION (environment variables)
  CONTEXT_WINDOW_SIZE   Total token budget to measure against (default: 200000)
                        Set this to match your model's context window.
                        Examples: 200000 (Sonnet), 1000000 (Opus Max)

OUTPUT
  Warnings are printed to stdout as plain text, injected into model context.
  Each threshold only fires once per session (idempotent via marker files).

INSTALLATION
  See README.md for settings.json snippet.
"""

import sys
import json
import os
import glob
import time

# Default: 200K (Claude Sonnet). Override with CONTEXT_WINDOW_SIZE env var.
CONTEXT_WINDOW = int(os.environ.get("CONTEXT_WINDOW_SIZE", 200_000))
MARKER_MAX_AGE = 86400  # 24 hours — clean up old marker files

# (fraction, level, message)
THRESHOLDS = [
    (0.50, "info",     "Context at 50%. Halfway through your working budget."),
    (0.65, "warning",  "Context at 65% — autocompaction at 80%. Consider wrapping or delegating remaining work."),
    (0.75, "critical", "Context at 75% — autocompaction imminent. Wrap now or lose planning context."),
]


def cleanup_old_markers(directory):
    """Remove stale marker files to avoid accumulation."""
    pattern = os.path.join(directory, "*.context-warned-*")
    now = time.time()
    for marker in glob.glob(pattern):
        try:
            if now - os.path.getmtime(marker) > MARKER_MAX_AGE:
                os.remove(marker)
        except OSError:
            pass


def get_latest_usage(transcript_path):
    """
    Read the tail of the JSONL transcript and return the most recent usage dict.
    Only reads the last 64KB to stay fast on large transcripts.
    Skips sidechain (subagent) entries to get the main session usage.
    """
    TAIL_BYTES = 64 * 1024
    last_usage = None
    try:
        file_size = os.path.getsize(transcript_path)
        with open(transcript_path, "r", encoding="utf-8") as f:
            if file_size > TAIL_BYTES:
                f.seek(file_size - TAIL_BYTES)
                f.readline()  # discard partial first line after seek
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if entry.get("isSidechain"):
                    continue
                if entry.get("isApiErrorMessage"):
                    continue
                msg = entry.get("message", {})
                if isinstance(msg, dict) and "usage" in msg:
                    last_usage = msg["usage"]
    except (OSError, IOError):
        pass
    return last_usage


def main():
    try:
        raw = sys.stdin.read()
        if not raw.strip():
            sys.exit(0)
        hook_input = json.loads(raw)
    except (json.JSONDecodeError, Exception):
        sys.exit(0)

    transcript_path = hook_input.get("transcript_path", "")
    if not transcript_path or not os.path.isfile(transcript_path):
        sys.exit(0)

    transcript_dir  = os.path.dirname(transcript_path)
    transcript_base = os.path.basename(transcript_path)
    cleanup_old_markers(transcript_dir)

    usage = get_latest_usage(transcript_path)
    if not usage:
        sys.exit(0)

    # Sum all token categories (input + cache creation + cache reads)
    total_tokens = (
        usage.get("input_tokens", 0)
        + usage.get("cache_creation_input_tokens", 0)
        + usage.get("cache_read_input_tokens", 0)
    )
    pct = total_tokens / CONTEXT_WINDOW

    for threshold, level, message in THRESHOLDS:
        marker = os.path.join(
            transcript_dir,
            f"{transcript_base}.context-warned-{int(threshold * 100)}"
        )
        if pct >= threshold and not os.path.exists(marker):
            try:
                with open(marker, "w") as f:
                    f.write(f"{total_tokens}/{CONTEXT_WINDOW} = {pct:.1%}")
            except OSError:
                pass

            prefix = {
                "info":     "CONTEXT",
                "warning":  "CONTEXT WARNING",
                "critical": "CONTEXT CRITICAL",
            }.get(level, "CONTEXT")

            print(
                f"{prefix}: {message} "
                f"({total_tokens:,}/{CONTEXT_WINDOW:,} tokens, {pct:.0%})"
            )
            break  # Only fire one threshold per call (highest unwarned)

    sys.exit(0)


if __name__ == "__main__":
    main()
