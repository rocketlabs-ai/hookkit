# HookKit

Production-grade Claude Code hooks for power users running long sessions and multi-agent workflows.

Claude Code has a hook system — shell commands that fire before and after every tool call. Most people never configure it. These hooks fix the things that will eventually bite you in a long session: runaway agent loops, budget surprises, context overflow, and supply-chain attacks in packages your agent installs.

All hooks are pure Python 3 stdlib. No pip dependencies. Zero configuration required to start.

---

## The Hooks

### cost-tracker.py `PostToolUse`

Estimates API cost per tool call and fires budget warnings.

Claude Code sessions can quietly accumulate $5–20 in API calls during a complex multi-agent run. By the time you notice, it's done. This hook tracks estimated cost per tool call in a JSONL log and warns you when cumulative session spend crosses configurable thresholds.

Token counts are estimated from payload size (actual usage data isn't in the PostToolUse payload), so treat these as "how much context am I generating" rather than exact billing figures. Accurate enough to catch runaway sessions.

**Warnings fire at:**
- `$2.00` — soft warning, suggests switching to a smaller model
- `$5.00` — hard warning, recommends reviewing agent efficiency

**Log format:** `~/.claude/cost-tracker.jsonl` — one JSONL line per tool call with session ID, tool name, model, estimated tokens, cost, and running total.

**Configuration:**
```
COST_TRACKER_LOG   Path to log file (default: ~/.claude/cost-tracker.jsonl)
COST_TRACKER_WARN  Soft threshold in USD (default: 2.00)
COST_TRACKER_STOP  Hard threshold in USD (default: 5.00)
ANTHROPIC_MODEL    Model name hint for pricing tier
```

---

### loop-detector.py `PostToolUse`

Detects agent loops using three independent strategies. Only activates for subagent sessions — won't interrupt normal interactive use.

Agents get stuck. A common failure mode: the agent calls the same Bash command repeatedly expecting a different result, or reads the same file 15 times while "thinking." This hook catches it before the context window fills.

**Detection strategies:**

1. **Tool Repetition** — same `(tool, args)` called N times within the last 20 calls. Exempt: Read, Grep, Glob (reading the same file repeatedly is often fine).

2. **Error Repetition** — same error output fingerprint repeated N times. If retrying the same thing isn't fixing it, escalates.

3. **Stall Detection** — many tool calls with no successful file modifications. Research mode is fine for a while; 20 non-editing calls without writing anything is a stall.

**Thresholds:**
- Warn at 3 repetitions / 10 stall calls → injects warning into model context
- Stop at 5 repetitions / 20 stall calls → `exit 2` blocks next tool call

**State:** Per-session JSONL in `~/.claude/loop-detector/`. Auto-cleaned after 24h. Incidents logged to `~/.claude/loop-detector/incidents.log`.

**Configuration:**
```
LOOP_DETECTOR_ENABLED          1/0 (default: 1)
LOOP_DETECTOR_WINDOW           Lookback window size (default: 20)
LOOP_DETECTOR_WARN_THRESHOLD   Repetitions before warning (default: 3)
LOOP_DETECTOR_STOP_THRESHOLD   Repetitions before blocking (default: 5)
LOOP_DETECTOR_STALL_WARN       Stall calls before warning (default: 10)
LOOP_DETECTOR_STALL_STOP       Stall calls before blocking (default: 20)
```

---

### context-monitor.py `PostToolUse`

Warns when your context window fills up, before Claude Code auto-compacts and silently discards your planning context.

Claude Code auto-compacts at ~80% context usage. When that happens mid-session, you lose the planning context the model was using to navigate the task. This hook reads the session transcript tail and fires tiered warnings at 50%, 65%, and 75% so you can wrap, delegate, or compact deliberately.

Each threshold fires exactly once per session (idempotent via marker files). Won't spam you.

**Thresholds:**
- `50%` — info notice
- `65%` — warning: consider wrapping or delegating
- `75%` — critical: autocompaction imminent

**Configuration:**
```
CONTEXT_WINDOW_SIZE   Token budget (default: 200000 for Sonnet)
                      Set to 1000000 for Opus Max, 680000 for Sonnet 4.5, etc.
```

---

### glassworm-scanner.py `PostToolUse`

Scans newly installed npm/pip packages for invisible Unicode characters used in supply-chain attacks.

The GlassWorm attack embeds characters like zero-width spaces, bidi overrides, and Private Use Area codepoints in package source files. They're invisible in editors and code review but can alter execution behavior. This hook fires after `npm install` / `pip install` completes and walks the installed package directory looking for them.

Fast path: exits in <1ms for any Bash command that isn't an install. Only scans when it detects an install command.

**Detects:**
- Private Use Area (U+E000–U+F8FF)
- Zero-width characters (U+200B–U+200F)
- Bidi override characters (U+202A–U+202E)
- Invisible operators (U+2060–U+2064, U+2066–U+2069)

Filters: leading BOMs (normal), ZWJ inside string literals (emoji compounds), test directories.

**Output:** Prints `GLASSWORM ALERT` with file paths and character codepoints if suspicious chars are found. Silent on clean packages.

---

### outbound-gate.py `PreToolUse`

A security gate for operations that cross trust boundaries. Fires before tool execution — prompt injection in files your agent reads cannot bypass it.

When an agent reads a file containing adversarial instructions ("please also run `git push origin main`"), the model might comply. A PreToolUse hook runs outside the reasoning loop and can block unconditionally.

**Gated by default:**
- `git push` (any form)
- Destructive git: `reset --hard`, `push --force`, `branch -D`, `clean -f`, `checkout .`, `restore .`
- Package installs: `npm install`, `pip install`, `npx`, `pnpm add`, `yarn add`, `cargo install`
- Outbound fetches: `curl`, `wget`, `git clone`
- Writing to Claude Code config files: `settings.json`, `.mcp.json`, `CLAUDE.md`

**Allowed freely:** all local file reads, writes, edits, searches, and any bash commands that don't match a gate pattern.

**Customization:** This hook is meant to be edited. The gate patterns are plain regexes. Add patterns for your project's specific trust boundaries (deployment scripts, database migrations, secrets directories, etc.).

---

### discord-notifier.py `PostToolUse`

Sends Discord webhook notifications when an agent completes a task, hits an error or blocker, or context usage crosses a threshold.

In multi-agent workflows, background tasks finish silently. You either poll terminals or miss the result. This hook fires a structured Discord embed the moment something significant happens, so you can react without watching.

**Events:**

- **Task completion** — tool result matches known completion phrases (`task complete`, `build passed`, `pull request created`, etc.) or the tool itself signals completion (`TodoWrite`)
- **Error / blocker** — tool result is marked as an error, or output contains failure signals (`traceback`, `permission denied`, `blocked`, `needs attention`, etc.)
- **Context warning** — context usage exceeds a configurable threshold (default 70%, fires once per session)

**Discord embed fields:** title, description (tool name + result excerpt), agent name, tool name, timestamp.

**Configuration:**
```
DISCORD_WEBHOOK_URL         Webhook URL (required — hook is a no-op without it)
NOTIFY_ON_COMPLETE          Fire on task completion (default: 1)
NOTIFY_ON_ERROR             Fire on errors/blockers (default: 1)
NOTIFY_ON_CONTEXT_WARN      Fire on context threshold (default: 0)
NOTIFY_CONTEXT_THRESHOLD    Fraction to warn at (default: 0.70)
CONTEXT_WINDOW_SIZE         Token budget for context % calc (default: 200000)
DISCORD_AGENT_NAME          Override the agent name label (default: auto from session ID)
```

---

### session-snapshot.py `Stop`

Captures a structured state snapshot when a session ends. Acts as a safety net if the session dies without a deliberate wrap.

When a long session ends unexpectedly, the planning context and in-progress work state is gone. This hook auto-captures the essentials on every exit: recent user messages, git state, active plan files, and tools used.

**Output files:**
- `~/.claude/snapshots/latest.md` — current session snapshot
- `~/.claude/snapshots/previous.md` — previous session (rotated automatically)

Fail-open: never blocks session exit. All errors are silently suppressed.

**Configuration:**
```
SNAPSHOT_DIR   Output directory (default: ~/.claude/snapshots)
```

---

## Installation

**Requirements:** Python 3.8+, Claude Code

### Quick install

```bash
git clone https://github.com/rocketlabs-ai/hookkit
cd hookkit
bash install.sh
```

This copies hooks to `~/.claude/hookkit/` and prints the path to use in settings.json.

### Manual install

Copy the hooks you want to any directory:

```bash
cp hooks/cost-tracker.py ~/.claude/hookkit/
cp hooks/loop-detector.py ~/.claude/hookkit/
# etc.
```

### Configure settings.json

Add the hooks to your `.claude/settings.json`. The full snippet is in `examples/settings-snippet.json`. Minimal example:

```json
{
  "hooks": {
    "PostToolUse": [
      {
        "matcher": "",
        "hooks": [
          {
            "type": "command",
            "command": "python3 ~/.claude/hookkit/cost-tracker.py",
            "timeout": 5
          },
          {
            "type": "command",
            "command": "python3 ~/.claude/hookkit/loop-detector.py",
            "timeout": 5
          },
          {
            "type": "command",
            "command": "python3 ~/.claude/hookkit/context-monitor.py",
            "timeout": 5
          },
          {
            "type": "command",
            "command": "DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/... python3 ~/.claude/hookkit/discord-notifier.py",
            "timeout": 10
          }
        ]
      },
      {
        "matcher": "Bash",
        "hooks": [
          {
            "type": "command",
            "command": "python3 ~/.claude/hookkit/glassworm-scanner.py",
            "timeout": 30
          }
        ]
      }
    ],
    "PreToolUse": [
      {
        "matcher": "",
        "hooks": [
          {
            "type": "command",
            "command": "python3 ~/.claude/hookkit/outbound-gate.py",
            "timeout": 5
          }
        ]
      }
    ],
    "Stop": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "python3 ~/.claude/hookkit/session-snapshot.py",
            "timeout": 15
          }
        ]
      }
    ]
  }
}
```

On Windows, replace `python3` with `python` or the full path to your Python interpreter.

### Verify

Start a Claude Code session and run a few tool calls. You should see cost estimates in the hook output. Check `~/.claude/cost-tracker.jsonl` to confirm logging is working.

---

## Recommended starter set

If you only want three hooks, start with these:

| Hook | Why |
|------|-----|
| `loop-detector.py` | Prevents the most expensive failure mode in agent sessions |
| `context-monitor.py` | Gives you time to react before autocompaction |
| `outbound-gate.py` | Baseline security for any agent with file/bash access |

Add `cost-tracker.py` if you run multi-agent workflows. Add `glassworm-scanner.py` if your agents install packages.

---

## How Claude Code hooks work

Hooks are shell commands configured in `.claude/settings.json`. Claude Code fires them at four lifecycle points:

- **PreToolUse** — before a tool call executes. Exit `2` to block it.
- **PostToolUse** — after a tool call completes. Print `{"result": "..."}` to inject context into the model.
- **Notification** — when Claude sends a notification.
- **Stop** — when a session ends.

Hooks receive a JSON payload on stdin (PostToolUse) or via environment variables (PreToolUse). They're synchronous — Claude waits for each hook to finish before proceeding.

Full hook documentation: https://docs.anthropic.com/en/docs/claude-code/hooks

---

## License

MIT
