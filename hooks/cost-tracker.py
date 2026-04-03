#!/usr/bin/env python3
"""
cost-tracker.py — PostToolUse Hook for Claude Code

Estimates API cost per tool call and fires budget warnings at configurable
thresholds. Appends one JSONL line per call to a log file for later analysis.

HOW IT WORKS
  PostToolUse hooks receive tool name, input, and output on stdin.
  This hook estimates token counts from payload size (len/4), calculates
  estimated cost using Anthropic's published rates, and accumulates a
  per-session running total.

  Token counts are estimates — the PostToolUse payload does not include
  actual API usage data. Treat these as "context volume" proxies, not
  exact billing figures.

CONFIGURATION (environment variables)
  COST_TRACKER_LOG   Path to JSONL log file (default: ~/.claude/cost-tracker.jsonl)
  COST_TRACKER_WARN  Soft warning threshold in USD (default: 2.00)
  COST_TRACKER_STOP  Hard warning threshold in USD (default: 5.00)
  ANTHROPIC_MODEL    Model name hint for pricing tier (haiku/sonnet/opus)

OUTPUT
  Warnings are printed as JSON {"result": "..."} which Claude Code injects
  into the model context. The log file is append-only JSONL.

INSTALLATION
  See README.md for settings.json snippet.

PRICING (per 1M tokens, approximate — update as needed)
  haiku:  $0.80 in / $4.00 out
  sonnet: $3.00 in / $15.00 out
  opus:   $15.00 in / $75.00 out
"""

import sys
import json
import os
import datetime

# --- Configuration ---
LOG_FILE   = os.environ.get("COST_TRACKER_LOG", os.path.join(os.path.expanduser("~"), ".claude", "cost-tracker.jsonl"))
WARN_SOFT  = float(os.environ.get("COST_TRACKER_WARN", "2.00"))
WARN_HARD  = float(os.environ.get("COST_TRACKER_STOP", "5.00"))

# Tail read size for cumulative calculation (64KB covers ~300+ entries)
TAIL_BYTES = 64 * 1024

# Pricing per 1M tokens
RATES = {
    "haiku":  {"in": 0.80,  "out": 4.00},
    "sonnet": {"in": 3.00,  "out": 15.00},
    "opus":   {"in": 15.00, "out": 75.00},
}


def detect_model(hook_input):
    """Infer model tier from hook payload or environment. Defaults to sonnet."""
    model_raw = hook_input.get("model", "") or ""
    for tier in ("haiku", "opus", "sonnet"):
        if tier in model_raw.lower():
            return tier
    for env_var in ("ANTHROPIC_MODEL", "CLAUDE_MODEL"):
        env_val = os.environ.get(env_var, "").lower()
        for tier in ("haiku", "opus", "sonnet"):
            if tier in env_val:
                return tier
    return "sonnet"


def estimate_tokens(value):
    """Estimate token count from content size: ~1 token per 4 chars."""
    if isinstance(value, str):
        return max(1, len(value) // 4)
    if isinstance(value, (dict, list)):
        return max(1, len(json.dumps(value)) // 4)
    return 1


def calc_cost(input_tokens, output_tokens, model):
    """Calculate estimated cost in USD."""
    rates = RATES.get(model, RATES["sonnet"])
    return round((input_tokens * rates["in"] + output_tokens * rates["out"]) / 1_000_000, 6)


def get_cumulative_session_cost(session_id):
    """Tail-read log file and sum costs for this session."""
    if not os.path.isfile(LOG_FILE):
        return 0.0
    total = 0.0
    try:
        file_size = os.path.getsize(LOG_FILE)
        with open(LOG_FILE, "r", encoding="utf-8") as f:
            if file_size > TAIL_BYTES:
                f.seek(file_size - TAIL_BYTES)
                f.readline()  # discard partial line after seek
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                    if entry.get("session_id") == session_id:
                        total += float(entry.get("est_cost_usd", 0))
                except (json.JSONDecodeError, ValueError, TypeError):
                    continue
    except (OSError, IOError):
        pass
    return round(total, 6)


def build_warning(cumulative):
    """Return warning message if a threshold is crossed, else None."""
    if cumulative >= WARN_HARD:
        return (
            f"COST ALERT: Session cost exceeds ${WARN_HARD:.2f} (${cumulative:.2f}). "
            "Review agent efficiency or switch to a cheaper model."
        )
    if cumulative >= WARN_SOFT:
        return (
            f"COST WARNING: Session has spent ${cumulative:.2f}. "
            "Consider using a smaller model for remaining work."
        )
    return None


def main():
    try:
        raw = sys.stdin.read()
        if not raw.strip():
            sys.exit(0)
        hook_input = json.loads(raw)
    except (json.JSONDecodeError, Exception):
        sys.exit(0)

    session_id  = hook_input.get("session_id", "unknown")
    tool_name   = hook_input.get("tool_name", "unknown")
    tool_input  = hook_input.get("tool_input", {})
    tool_output = hook_input.get("tool_output", "")

    # Prefer explicit usage fields if present, otherwise estimate from size
    usage = hook_input.get("usage", {}) or {}
    if usage and ("input_tokens" in usage or "output_tokens" in usage):
        input_tokens  = int(usage.get("input_tokens", 0))
        output_tokens = int(usage.get("output_tokens", 0))
    else:
        input_tokens  = estimate_tokens(tool_input)
        output_tokens = estimate_tokens(tool_output)

    model    = detect_model(hook_input)
    est_cost = calc_cost(input_tokens, output_tokens, model)

    prior       = get_cumulative_session_cost(session_id)
    cumulative  = round(prior + est_cost, 6)

    ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    entry = {
        "ts":                     ts,
        "session_id":             session_id,
        "tool":                   tool_name,
        "model":                  model,
        "input_tokens":           input_tokens,
        "output_tokens":          output_tokens,
        "est_cost_usd":           est_cost,
        "cumulative_session_usd": cumulative,
    }

    try:
        os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, separators=(",", ":")) + "\n")
    except (OSError, IOError):
        pass  # Never fail the hook on write errors

    warning = build_warning(cumulative)
    if warning:
        print(json.dumps({"result": warning}))

    sys.exit(0)


if __name__ == "__main__":
    main()
