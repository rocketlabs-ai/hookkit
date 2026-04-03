#!/usr/bin/env python3
"""
glassworm-scanner.py — PostToolUse Hook for Claude Code (Bash)

Scans newly installed npm/pip packages for invisible Unicode characters
used in supply-chain attacks (the "GlassWorm" vector).

WHY THIS EXISTS
  The GlassWorm attack embeds invisible Unicode characters in package source
  files. These characters are invisible in editors and code review but can
  be interpreted by interpreters to alter execution flow. A PostToolUse hook
  fires AFTER the install completes, so the files exist on disk to scan.

WHAT IT DETECTS
  - Private Use Area chars (U+E000–U+F8FF)
  - Zero-width characters (U+200B–U+200F)
  - BOM / zero-width no-break space (U+FEFF, non-leading only)
  - Bidi override characters (U+202A–U+202E)
  - Invisible operators (U+2060–U+2064, U+2066–U+2069)

FAST PATH
  Exits in <1ms for any Bash command that isn't an install command.
  Only scans when it detects npm install, pip install, npx, or pnpm add.

SCOPE
  Scans .js, .mjs, .cjs, .py, .ts, .jsx, .tsx files.
  Skips test directories (false positives are common in test fixtures).
  Skips leading BOM (normal in some files) and ZWJ inside string literals
  (used for emoji compound sequences).

OUTPUT
  Prints a GLASSWORM ALERT message to stdout if suspicious characters are
  found. This is injected into model context. Silent on clean packages.

INSTALLATION
  See README.md for settings.json snippet.
"""

import json
import os
import re
import sys

# Suspicious Unicode characters
SUSPICIOUS = re.compile(
    "["
    "\uE000-\uF8FF"    # Private Use Area
    "\u200B-\u200F"    # Zero-width characters
    "\uFEFF"           # BOM / zero-width no-break space
    "\u202A-\u202E"    # Bidi override characters
    "\u2060-\u2064"    # Invisible operators
    "\u2066-\u2069"    # Bidi isolates
    "]"
)

SCAN_EXTS = {".js", ".mjs", ".cjs", ".py", ".ts", ".jsx", ".tsx"}

TEST_PATTERNS = re.compile(
    r"([\\/]tests?[\\/]|[\\/]__tests__[\\/]|[\\/]test_|_test\.py$|\.test\.|\.spec\.)",
    re.IGNORECASE,
)

NPM_INSTALL = re.compile(r"\b(npm\s+install|npm\s+i\s|npx\s|pnpm\s+(install|add))\b")
PIP_INSTALL = re.compile(r"\b(pip\s+install|pip3\s+install|python.*-m\s+pip\s+install)\b")


def extract_package_name(command):
    """Try to extract the primary package name from an install command."""
    m = re.search(r"npm\s+(?:install|i)\s+(?:--save[^ ]*\s+|--legacy[^ ]*\s+|-[A-Za-z]\s+)*([a-zA-Z0-9@/_.-]+)", command)
    if m:
        return m.group(1), "npm"
    m = re.search(r"pnpm\s+(?:install|add)\s+([a-zA-Z0-9@/_.-]+)", command)
    if m:
        return m.group(1), "npm"
    m = re.search(r"pip3?\s+install\s+(?:-[a-zA-Z]+\s+)*([a-zA-Z0-9_.-]+)", command)
    if m:
        return m.group(1), "pip"
    return None, None


def find_package_dir(pkg_name, pkg_type, cwd):
    """Locate the installed package directory on disk."""
    if pkg_type == "npm":
        nm = os.path.join(cwd, "node_modules", pkg_name.split("/")[-1])
        if os.path.isdir(nm):
            return nm
        if pkg_name.startswith("@"):
            nm = os.path.join(cwd, "node_modules", pkg_name)
            if os.path.isdir(nm):
                return nm
    elif pkg_type == "pip":
        for venv in ["venv", ".venv", "env"]:
            sp = os.path.join(cwd, venv, "Lib", "site-packages", pkg_name.replace("-", "_"))
            if os.path.isdir(sp):
                return sp
    return None


def scan_directory(pkg_dir):
    """Walk package directory, scan source files for suspicious Unicode."""
    findings = []
    files_scanned = 0

    for root, dirs, files in os.walk(pkg_dir):
        dirs[:] = [d for d in dirs if d not in {"node_modules", "__pycache__", ".cache", "dist"}]
        for fname in files:
            ext = os.path.splitext(fname)[1].lower()
            if ext not in SCAN_EXTS:
                continue
            fpath = os.path.join(root, fname)
            if TEST_PATTERNS.search(fpath):
                continue
            try:
                with open(fpath, "r", encoding="utf-8", errors="ignore") as f:
                    content = f.read()
                files_scanned += 1

                matches = SUSPICIOUS.findall(content)
                if not matches:
                    continue

                real_matches = []
                for c in matches:
                    # Skip leading BOM (benign)
                    if ord(c) == 0xFEFF and content.index(c) == 0:
                        continue
                    # Skip ZWJ inside string literals (emoji compounds)
                    if ord(c) == 0x200D:
                        idx = content.index(c)
                        before = content[max(0, idx - 50):idx]
                        if "'" in before or '"' in before or "`" in before:
                            continue
                    real_matches.append(c)

                if real_matches:
                    chars = set(f"U+{ord(c):04X}" for c in real_matches)
                    findings.append((fpath, len(real_matches), chars))

            except (OSError, PermissionError):
                pass

    return findings, files_scanned


def main():
    try:
        raw = sys.stdin.read()
        if not raw.strip():
            sys.exit(0)
        hook_input = json.loads(raw)
    except (json.JSONDecodeError, Exception):
        sys.exit(0)

    tool_name  = hook_input.get("tool_name", "")
    tool_input = hook_input.get("tool_input", {})

    # Fast path: only care about Bash tool
    if tool_name != "Bash":
        sys.exit(0)

    command = tool_input.get("command", "")

    # Fast path: not an install command
    if not NPM_INSTALL.search(command) and not PIP_INSTALL.search(command):
        sys.exit(0)

    pkg_name, pkg_type = extract_package_name(command)
    if not pkg_name:
        sys.exit(0)

    cwd     = hook_input.get("cwd", os.getcwd())
    pkg_dir = find_package_dir(pkg_name, pkg_type, cwd)

    if not pkg_dir or not os.path.isdir(pkg_dir):
        sys.exit(0)

    findings, files_scanned = scan_directory(pkg_dir)

    if findings:
        msg = (
            f"GLASSWORM ALERT: Suspicious invisible Unicode detected in "
            f"newly installed package '{pkg_name}' ({files_scanned} files scanned):\n"
        )
        for fpath, count, chars in findings:
            rel = os.path.relpath(fpath, pkg_dir)
            msg += f"  {rel}: {count} suspicious chars {chars}\n"
        msg += (
            "These characters can hide malicious code (supply-chain attack vector). "
            "Review the flagged files before using this package."
        )
        print(msg)
    # Clean packages: silent pass-through

    sys.exit(0)


if __name__ == "__main__":
    main()
