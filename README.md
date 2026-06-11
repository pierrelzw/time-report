# time-report

Interactive HTML Gantt + token/cost report for Claude Code & Codex sessions.

## Install

```bash
claude plugin marketplace add pierrelzw/zhiwei-skills && claude plugin install time-report@pierrelzw --scope user
```

## Usage

```
/time-report --list                         # List all projects with session counts
/time-report <project> <YYYY-MM>            # Report for a specific month
/time-report <project> <from> <to>          # Report for a date range (YYYY-MM-DD)
/time-report <project> <range> 可视化        # Also render per-session transcripts
```

Examples:

```
/time-report wechat_automation 2026-05
/time-report editory 2026-05-01 2026-05-15
```

## What you get

1. **Gantt chart** — every session as a bar on a per-day timeline; hover for detail,
   click to expand. Active Time unions parallel sessions per calendar day, so
   overlapping wall-clock is counted once. A threshold slider (default 15 min)
   recomputes active time live.
2. **Token & Cost table** — one row per session: Started, Title, Summary, Messages,
   Input, Output, Cache Write, Cache Read, Total, Cost. Sortable; paginates past
   15 rows; the TOTAL footer always reflects the full dataset.

The terminal also prints a text summary and a per-session table.

## Sources & dedup

- Includes both **Claude Code** (`~/.claude/projects`) and **Codex**
  (`~/.codex/sessions` + `archived_sessions`) sessions.
- Sessions are deduped by `external_id`, so a session that ran in a git worktree
  under the repo (registered under both the repo path and the worktree path) is
  counted once.

## Cost methodology

Costs are computed from each assistant message's `usage` block, priced **as if every
call went through the first-party Anthropic API at standard (non-batch) rates** —
Opus $5/$25, Sonnet $3/$15, Haiku $1/$5 per 1M input/output tokens; cache writes
1.25× (5-min) / 2× (1-hour) input, cache reads 0.1× input. Model family is inferred
per message, so mixed-model sessions are priced correctly. **This is an
API-equivalent estimate, not your actual Claude Code subscription charge.**

**Codex sessions are not priced** (they run OpenAI models): their time and token
counts are included, but the Cost column shows "—" and they are excluded from the
`$` total, keeping it a pure Anthropic-API-equivalent figure.

## Requirements

- Python 3
- The `cccmemory` MCP server (the skill calls `index_all_projects` to refresh the DB).
- Optional: `uv`/`uvx` on PATH for transcript rendering (`可视化` / `--transcripts`).

## License

MIT
