#!/usr/bin/env python3
"""
outbound-gate.py — PreToolUse Hook for Claude Code

A security gate that intercepts operations crossing trust boundaries before
they execute. Fires outside the reasoning loop, so prompt injection cannot
bypass it.

WHY THIS EXISTS
  Claude Code agents can be manipulated by adversarial content in files or
  web pages they read. A compromised agent might attempt to push code, send
  emails, install packages, or make outbound HTTP requests without your
  knowledge. PreToolUse hooks run before tool execution and can block (exit 2)
  any action regardless of what the model "decided."

WHAT IS GATED BY DEFAULT
  - git push (any form)
  - Destructive git operations (reset --hard, push --force, branch -D, etc.)
  - Package installs (npm install, pip install, etc.)
  - Outbound fetches (curl, wget, git clone)
  - Writing to configuration files (.claude/settings.json, CLAUDE.md, etc.)

WHAT IS ALLOWED FREELY
  All local file reads, writes, edits, grep, glob, bash commands that don't
  match the gate patterns.

CUSTOMIZATION
  This hook is designed to be edited. Add or remove gate patterns to match
  your project's security posture. Each block() call can be customized with
  a message explaining what was blocked and why.

  Common additions:
    - Block writes to specific directories (secrets, credentials)
    - Gate database migration commands
    - Gate deployment scripts
    - Gate API calls to specific domains

HOW IT WORKS
  Claude Code passes tool name and input via environment variables for
  PreToolUse hooks. This hook reads those, pattern-matches against gates,
  and either exits 0 (allow) or exits 2 with a message (block).

INSTALLATION
  See README.md for settings.json snippet.
"""

import os
import re
import sys
import json

# PreToolUse hooks receive tool info via environment variables
TOOL  = os.environ.get("CLAUDE_TOOL_NAME", "")
INPUT = os.environ.get("CLAUDE_TOOL_INPUT", "")


def block(message):
    """Block the tool call with an explanatory message."""
    # Exit 2 = block. Message goes to stderr and is shown to the model.
    print(message, file=sys.stderr)
    sys.exit(2)


# ─── BASH COMMAND GATES ────────────────────────────────────────────────────────

if TOOL == "Bash":
    # Block all git push (code leaving the machine)
    if re.search(r"\bgit\s+push\b", INPUT):
        block("PUSH GATE: git push requires explicit confirmation. Verify the target branch and remote before proceeding.")

    # Block irreversible git operations
    if re.search(r"git\s+(reset\s+--hard|push\s+--force|push\s+-f|branch\s+-D|clean\s+-f|checkout\s+\.|restore\s+\.)", INPUT):
        block("DESTRUCTIVE GIT GATE: This operation is irreversible. Confirm explicitly before proceeding.")

    # Block package installs (supply-chain risk, unexpected dependencies)
    if re.search(r"\b(npm\s+install|npm\s+i\s|pip\s+install|pip3\s+install|npx\s|pnpm\s+add|yarn\s+add|gem\s+install|cargo\s+install)\b", INPUT):
        block("PACKAGE GATE: Installing an external package. Review the package name and source before proceeding.")

    # Block outbound fetches
    if re.search(r"\b(curl\s|wget\s|git\s+clone\b)", INPUT):
        block("FETCH GATE: Fetching external content. Verify the URL is expected before proceeding.")


# ─── FILE WRITE GATES ─────────────────────────────────────────────────────────

if TOOL in ("Edit", "Write"):
    # Block writes to Claude Code configuration files
    if re.search(r"(\.claude[/\\]settings|\.mcp\.json|CLAUDE\.md)", INPUT, re.IGNORECASE):
        block("CONFIG GATE: Writing to a Claude Code configuration file. Confirm this change is intentional.")


# ─── ALL CLEAR ────────────────────────────────────────────────────────────────

sys.exit(0)
