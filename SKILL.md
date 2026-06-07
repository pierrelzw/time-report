---
name: time-report
description: 生成 Claude Code 会话时间的交互式 HTML 甘特图报告。用法：/time-report <project> <month|from to> 或 /time-report --list
version: 3.2.0
tools: Bash, mcp__cccmemory__index_all_projects
---

# Time Report

Generate an interactive HTML Gantt chart report showing Claude Code session activity.

## Arguments

`$ARGUMENTS` contains the user input. Parse as:
- `--list` → List all projects with session counts
- `<project> <YYYY-MM>` → Report for a specific month
- `<project> <from-date> <to-date>` → Report for a date range
- No arguments → Same as `--list`

## Execution

### Step 1: Ensure DB freshness
Call MCP tool to index any new sessions:
```
mcp__cccmemory__index_all_projects(incremental=true)
```

### Step 2: Generate report
```bash
python3 {baseDir}/scripts/time-report.py $ARGUMENTS --open
```

If the script reports an error, display the error message to the user.

## Output

The skill produces an interactive HTML report (written to `/tmp/time-report-<project>-<range>.html`) and opens it in the default browser (the `--open` flag handles this). It has two parts:

1. **Gantt chart** — each session as a horizontal bar on a per-day timeline, with hover tooltips and click-to-expand detail.
2. **Token Usage & Cost table** — one row per session with industry-named columns (Started, Session, Messages, Input, Output, Cache Write, Cache Read, Total, Cost) and an API-equivalent USD cost, plus stat cards for total tokens and total cost. A glossary above the table explains each column. Click any numeric/time column header to sort (default: **Total tokens, descending**; time sorts ascending, amounts descending). Tables longer than 15 rows paginate; the TOTAL footer always reflects the full dataset, not the current page.

The terminal also prints a text summary (active time, total tokens, estimated cost, per-model cost breakdown) followed by a **per-session table report** with the same token/cost columns.

When `--list` is used instead, the script prints a plain-text table of projects with their session counts to stdout — no HTML file is produced.

### Cost methodology

Costs are computed from each assistant message's `usage` block in the session JSONL, priced **as if every call went through the first-party Anthropic API at standard (non-batch) rates** — Opus $5/$25, Sonnet $3/$15, Haiku $1/$5 per 1M input/output tokens; cache writes 1.25× (5-min) / 2× (1-hour) input, cache reads 0.1× input (pricing cached from the `claude-api` skill, 2026-05-26). Model family is inferred from the `model` field per message, so mixed-model sessions (e.g. Haiku subagents) are priced correctly. **This is an API-equivalent estimate, not your actual Claude Code subscription charge** — subscriptions are billed differently. To refresh prices, edit the `PRICING` dict in `scripts/time-report.py`.

Price references:
- Per-model token prices: <https://www.anthropic.com/pricing#api>
- Prompt-cache write/read multipliers: <https://platform.claude.com/docs/en/build-with-claude/prompt-caching>
