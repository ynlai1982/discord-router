# Future Architecture Notes (post-Phase 4)

This file is a backlog for ideas that should NOT be implemented as part of the
Phase 0-4 Discord MCP fork project, but are worth revisiting after that work
ships and stabilizes.

---

## Phase 5 candidate: long-running claude + Monitor tool

### Trigger

Claude Code released the `Monitor` tool (announced ~2026-04-10 on X by
@alistaiir). It spawns a background process and streams each stdout line into
the conversation as a notification, without polling.

> "Use the monitor tool and `kubectl logs -f | grep ..` to listen for errors,
> make a pr to fix any crashes" — more reliable and token-efficient than
> polling within the agent loop.

### How it could rearchitect the router

Today's router is **spawn-per-message**: every Discord message triggers
`claude --print -p PROMPT` (one-shot subprocess). Cold start every time,
session continuity faked via `--resume`, MCP plugins reloaded per spawn.

Monitor enables a **long-running claude per session_group** alternative:

```
router.py (daemon, sole Discord gateway owner)
  ↓ writes new inbound messages to per-group event streams
            (stdout, named pipe, log file, ...)

long-running claude × N (one per session_group)
  ↑ Monitor("tail -F /path/to/<group>-stream") streams new lines into context
  → reacts in-place using the Phase 2-4 custom Discord MCP fork to reply
```

### Wins
- Persistent context per session_group (real continuity, not `--resume` replay)
- Zero cold start: messages reacted to immediately
- CLAUDE.md, memory system, MCP plugins all loaded ONCE per group
- Lower token cost per message (no system prompt re-emission)
- Monitor can also watch cron job stdout, long-task progress, etc — unifies
  several "wait for thing to happen" patterns

### New problems to solve
- **Watchdog**: a long-running claude that crashes silently kills its
  session_group entirely. launchd or a sibling watchdog must restart it.
- **Context window saturation**: long-running session eventually runs out of
  context. Need rotation strategy. The existing 07:00 daily reset can be
  extended into a "checkpoint and restart" routine.
- **Resource cost**: N concurrent long-running claudes consume RAM and token
  quota even when idle.
- **Multi-session orchestration**: launchd / supervisor needs to manage N
  long-running claudes instead of one router. More moving parts.
- **Migration of existing cron jobs**: today's cron jobs spawn `claude --print`
  too. They'd either keep that pattern or be redirected into the long-running
  session via the same event stream.

### Recommended pilot sequence (when Phase 5 arrives)

1. Phase 4 ships and runs cleanly for at least one week — fork stable.
2. Pick ONE pilot session_group: `mission-control` or `we-are-freedom`
   (both are isolated, low-traffic, and safe to break).
3. Add a per-group event stream output to router.py for that one group only.
4. Manually launch a long-running claude for that group, with Monitor on the
   stream.
5. Observe for 1 week: latency, context fill rate, crash rate, token cost.
6. If green, extend to a second group. If red, rollback is just "stop the
   long-running claude, fall back to spawn-per-message".
7. Only after multiple groups are stable, consider migrating `main`.

### Cross-references

- User memory: `project_main_session_architecture` ("主 session 統一架構、
  多模型派工、cron heartbeat")
- User memory: `project_memory_layering` (single agent multi model direction)
- Both memories already point in this direction; Monitor is the missing piece
  that makes inbound message handling viable for long-running claude.

### What Phase 5 does NOT change about Phase 2-4 work

- The custom Discord MCP fork (Phase 2-3) is REQUIRED for both architectures.
  Long-running claude still loads MCP plugins at startup; if it loaded the
  official Discord plugin, it would still grab the bot token. The fork solves
  that regardless of spawn vs persistent.
- The router HTTP API (Phase 1) is REQUIRED for both architectures. Long-running
  claude's MCP fork still proxies through the same HTTP endpoints.
- Phase 4 cutover (kill plugin, switch settings) still happens identically.

In short: Phase 2-4 are foundation. Phase 5 is an optional refactor on top.
