#!/usr/bin/env python3
"""Claude Code Time Report Generator — v3.0 (cccmemory.db driven).

Uses ~/.cccmemory.db as the primary data source for project/session discovery,
then reads JSONL files for per-message timestamps.

Usage:
    python3 time-report.py --list                           # List all projects
    python3 time-report.py <project> <YYYY-MM>              # Monthly report
    python3 time-report.py <project> <from> <to>            # Date range
    python3 time-report.py <project> <YYYY-MM> --json       # JSON output
    python3 time-report.py <project> <YYYY-MM> -o out.html  # Custom output path
    python3 time-report.py <project> <YYYY-MM> --open       # Auto-open (default)
    python3 time-report.py <project> <YYYY-MM> --no-open    # Don't auto-open
"""

import argparse
import calendar
import concurrent.futures
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path


CLAUDE_DIR = Path.home() / ".claude" / "projects"
CODEX_DIR = Path.home() / ".codex"  # Codex CLI session transcripts live here
DB_PATH = Path(os.environ.get("CCCMEMORY_DB", str(Path.home() / ".cccmemory.db")))


# ── API pricing (USD per 1M tokens) ──────────────────────────────────────────
# Source: claude-api skill (cached 2026-05-26). Priced as if every call went
# through the first-party Anthropic API at standard (non-batch) rates.
# Claude Code subscriptions are billed differently — treat this as an
# "API-equivalent" cost estimate, not an actual charge.
# Reference:
#   - https://www.anthropic.com/pricing#api            (per-model token prices)
#   - https://platform.claude.com/docs/en/build-with-claude/prompt-caching
#                                                      (cache write/read multipliers)
PRICING = {
    "opus":   {"in": 5.0,  "out": 25.0},   # Opus 4.x
    "sonnet": {"in": 3.0,  "out": 15.0},   # Sonnet 4.x
    "haiku":  {"in": 1.0,  "out": 5.0},    # Haiku 4.5
}
# Cache costs are multiples of the model's base *input* price.
CACHE_READ_MULT = 0.10      # cache hit  → 0.1× input
CACHE_WRITE_5M_MULT = 1.25  # 5-min TTL write → 1.25× input
CACHE_WRITE_1H_MULT = 2.0   # 1-hour TTL write → 2× input

TOKEN_KEYS = ("input", "output", "cache_read", "cache_write_5m", "cache_write_1h")


def _model_family(model):
    """Map a full model id to a pricing family. Unknown → opus (conservative)."""
    m = (model or "").lower()
    if "haiku" in m:
        return "haiku"
    if "sonnet" in m:
        return "sonnet"
    if "opus" in m:
        return "opus"
    return "opus"


def _empty_tokens():
    return {k: 0 for k in TOKEN_KEYS}


def cost_for_family(family, tok):
    """USD cost for one family's token bucket."""
    p = PRICING[family]
    pin, pout = p["in"], p["out"]
    return (
        tok["input"] * pin
        + tok["output"] * pout
        + tok["cache_read"] * pin * CACHE_READ_MULT
        + tok["cache_write_5m"] * pin * CACHE_WRITE_5M_MULT
        + tok["cache_write_1h"] * pin * CACHE_WRITE_1H_MULT
    ) / 1_000_000


def fmt_tokens(n):
    """Human-readable token count: 1234 → '1.2K', 4500000 → '4.5M'."""
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return str(int(n))


def fmt_cost(usd):
    """Format a USD cost. Sub-cent values still show 2 decimals."""
    return f"${usd:,.2f}"


def _open_db():
    """Open cccmemory.db, exit with error if missing."""
    if not DB_PATH.exists():
        print(f"Error: {DB_PATH} not found.", file=sys.stderr)
        print("Run: mcp__cccmemory__index_all_projects() to create it.", file=sys.stderr)
        sys.exit(1)
    return sqlite3.connect(str(DB_PATH))


def _get_project_name(path):
    """Extract display name from canonical_path."""
    p = Path(path)
    # Known project containers: ~/codes/xxx, ~/Projects/xxx, ~/Documents/Projects/xxx
    if p.parent.name in ("codes", "Projects"):
        return p.name
    if len(p.parts) > 2 and p.parts[-2] == "Projects":
        return p.name
    # Non-project paths (Home, Downloads, /) → show full path
    return str(p)


def _build_jsonl_index():
    """Scan ~/.claude/projects/*/ to build session_id → JSONL file path index."""
    index = {}
    if not CLAUDE_DIR.exists():
        return index
    for d in CLAUDE_DIR.iterdir():
        if not d.is_dir():
            continue
        for jsonl_file in d.glob("*.jsonl"):
            sid = jsonl_file.stem  # UUID = session_id = external_id
            index[sid] = str(jsonl_file)
    return index


# Codex names its transcripts rollout-<ISO-timestamp>-<uuid>.jsonl; the uuid is
# the conversation's external_id in the DB. Match that trailing uuid.
_CODEX_UUID_RE = re.compile(
    r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$"
)


def _build_codex_index():
    """Scan ~/.codex/{sessions,archived_sessions} → session_id → JSONL path.

    Claude Code keeps transcripts under ~/.claude/projects; Codex keeps its own
    under ~/.codex, which `_build_jsonl_index` never sees. Without this, every
    Codex session resolves to "no transcript" and is dropped from the report.
    """
    index = {}
    if not CODEX_DIR.exists():
        return index
    for sub in ("sessions", "archived_sessions"):
        base = CODEX_DIR / sub
        if not base.exists():
            continue
        for jsonl_file in base.rglob("*.jsonl"):
            m = _CODEX_UUID_RE.search(jsonl_file.stem)
            if m:
                index[m.group(1)] = str(jsonl_file)
    return index


def list_projects():
    """List all projects with session counts from DB."""
    conn = _open_db()
    rows = conn.execute("""
        SELECT p.canonical_path, COUNT(c.id)
        FROM projects p
        JOIN conversations c ON c.project_id = p.id
        GROUP BY p.id
        ORDER BY COUNT(c.id) DESC
    """).fetchall()
    conn.close()

    if not rows:
        print("No projects found in database.")
        return

    print(f"\n{'Project':<40} {'Sessions':>8}  {'Path'}")
    print("-" * 90)
    total = 0
    for path, count in rows:
        name = _get_project_name(path)
        print(f"{name:<40} {count:>8}  {path}")
        total += count
    print(f"\nTotal: {len(rows)} projects, {total} sessions")


def resolve_project(keyword, date_from, date_to):
    """Find matching project sessions from DB within date range.

    Boundaries are computed in the machine's LOCAL timezone — the same tz the
    rest of the script uses for grouping/display (datetime.fromtimestamp).
    Using naive datetimes here makes .timestamp() interpret them as local time;
    forcing UTC would shift the reported "day" by the local UTC offset.
    """
    from_ms = int(datetime.combine(date_from, datetime.min.time())
                  .timestamp() * 1000)
    to_ms = int(datetime.combine(date_to + timedelta(days=1), datetime.min.time())
                .timestamp() * 1000)

    conn = _open_db()
    rows = conn.execute("""
        SELECT c.external_id, p.canonical_path, c.git_branch,
               c.message_count, c.first_message_at, c.last_message_at,
               c.source_type
        FROM conversations c
        JOIN projects p ON c.project_id = p.id
        WHERE LOWER(p.canonical_path) LIKE ?
          AND c.last_message_at >= ?
          AND c.first_message_at < ?
    """, (f"%{keyword.lower()}%", from_ms, to_ms)).fetchall()
    conn.close()

    project_paths = sorted(set(r[1] for r in rows))
    return rows, project_paths, from_ms, to_ms


def parse_iso_timestamp(iso_str):
    """Parse ISO 8601 timestamp string to epoch milliseconds."""
    if not iso_str:
        return None
    try:
        s = iso_str.replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        return int(dt.timestamp() * 1000)
    except (ValueError, AttributeError):
        return None


def extract_timestamps(jsonl_path):
    """Read JSONL file, extract timestamps and token usage from records.

    Returns dict with:
        timestamps: sorted list of epoch ms
        summary: first user message (truncated)
        token_events: list of {"ts", "family", <TOKEN_KEYS>} — one per
            assistant message that carried a usage block. Filtered to the
            report's date range later by the caller.
    """
    timestamps = []
    token_events = []
    summary = None
    title = None  # latest Claude Code auto-generated session title (ai-title record)
    skipped = 0

    try:
        with open(jsonl_path, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    skipped += 1
                    continue

                rec_type = record.get("type")

                # Session title: Claude Code writes successive `ai-title` records
                # as the topic is refined; keep the last non-empty one.
                if rec_type == "ai-title":
                    at = record.get("aiTitle")
                    if at:
                        title = at.strip()
                    continue

                if rec_type not in ("user", "assistant"):
                    continue

                ts_str = record.get("timestamp")
                ts_ms = parse_iso_timestamp(ts_str)
                if ts_ms:
                    timestamps.append(ts_ms)

                # Token usage rides on assistant messages
                if rec_type == "assistant":
                    ev = _parse_usage(record, ts_ms)
                    if ev:
                        token_events.append(ev)

                # Extract summary from first non-meta user message
                if rec_type == "user" and summary is None and not record.get("isMeta"):
                    msg = record.get("message", {})
                    content = msg.get("content", "") if isinstance(msg, dict) else ""
                    if isinstance(content, str) and content:
                        clean = re.sub(r"<[^>]+>", "", content).strip()
                        if clean:
                            summary = clean[:80]

    except OSError:
        pass

    if skipped > 0:
        print(f"  WARN: skipped {skipped} unparseable lines in {jsonl_path}", file=sys.stderr)

    return {
        "timestamps": sorted(timestamps),
        "summary": summary,
        "title": title,
        "token_events": token_events,
    }


def _parse_usage(record, ts_ms):
    """Pull a token-usage event out of one assistant JSONL record, or None."""
    msg = record.get("message")
    if not isinstance(msg, dict):
        return None
    usage = msg.get("usage")
    if not isinstance(usage, dict):
        return None

    cc = usage.get("cache_creation")
    if isinstance(cc, dict):
        w5 = cc.get("ephemeral_5m_input_tokens", 0) or 0
        w1 = cc.get("ephemeral_1h_input_tokens", 0) or 0
    else:
        # Older records: only the flat total — treat as 5-minute writes
        w5 = usage.get("cache_creation_input_tokens", 0) or 0
        w1 = 0

    return {
        "ts": ts_ms,
        "family": _model_family(msg.get("model")),
        "input": usage.get("input_tokens", 0) or 0,
        "output": usage.get("output_tokens", 0) or 0,
        "cache_read": usage.get("cache_read_input_tokens", 0) or 0,
        "cache_write_5m": w5,
        "cache_write_1h": w1,
    }


def aggregate_tokens(token_events):
    """Sum token events into per-family buckets, an overall bucket, and cost.

    Returns dict: {families: {fam: {tokens, cost}}, tokens: {...,total}, cost}.
    """
    families = {}
    for ev in token_events:
        fam = ev["family"]
        bucket = families.setdefault(fam, _empty_tokens())
        for k in TOKEN_KEYS:
            bucket[k] += ev[k]

    overall = _empty_tokens()
    total_cost = 0.0
    fam_out = {}
    for fam, tok in families.items():
        for k in TOKEN_KEYS:
            overall[k] += tok[k]
        c = cost_for_family(fam, tok)
        total_cost += c
        fam_out[fam] = {"tokens": tok, "cost": round(c, 6)}

    overall["total"] = sum(overall[k] for k in TOKEN_KEYS)
    return {"families": fam_out, "tokens": overall, "cost": round(total_cost, 6)}


# Codex's first user "messages" are injected wrappers (environment context,
# AGENTS.md / persona instructions), not the human's prompt — skip them when
# picking a summary line.
_CODEX_WRAPPER_RE = re.compile(r"^\s*<(environment_context|user_instructions|"
                               r"persona|system)", re.I)


def _codex_is_wrapper(text):
    s = text.strip()
    return bool(_CODEX_WRAPPER_RE.match(s)) or s.startswith("# AGENTS.md")


def extract_codex_timestamps(jsonl_path):
    """Codex-flavoured counterpart to extract_timestamps().

    Codex JSONL differs from Claude Code: every record has a top-level ISO
    `timestamp`; token usage rides on `event_msg`/`token_count` payloads whose
    `last_token_usage` is the per-turn delta (verified to sum to the session's
    cumulative `total_token_usage`). Codex runs OpenAI models, so we deliberately
    DO NOT price it — token counts are reported, cost is left to the caller
    (marked N/A) to keep the headline $ a pure Anthropic-API-equivalent figure.
    """
    timestamps = []
    token_events = []
    summary = None
    skipped = 0

    try:
        with open(jsonl_path, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    skipped += 1
                    continue

                ts_ms = parse_iso_timestamp(record.get("timestamp"))
                if ts_ms:
                    timestamps.append(ts_ms)

                payload = record.get("payload")
                if not isinstance(payload, dict):
                    continue
                ptype = payload.get("type")

                if ptype == "token_count":
                    info = payload.get("info") or {}
                    lu = info.get("last_token_usage") or {}
                    inp = lu.get("input_tokens", 0) or 0
                    cached = lu.get("cached_input_tokens", 0) or 0
                    token_events.append({
                        "ts": ts_ms,
                        # Codex's input_tokens INCLUDES the cached portion; split
                        # it so input = uncached prompt, cache_read = cached.
                        "input": max(inp - cached, 0),
                        "output": lu.get("output_tokens", 0) or 0,
                        "cache_read": cached,
                        "cache_write_5m": 0,
                        "cache_write_1h": 0,
                    })
                elif ptype == "message" and payload.get("role") == "user" \
                        and summary is None:
                    for c in payload.get("content", []):
                        tx = c.get("text", "") if isinstance(c, dict) else ""
                        if tx and not _codex_is_wrapper(tx):
                            clean = re.sub(r"<[^>]+>", "", tx).strip()
                            if clean:
                                summary = clean[:80]
                                break
    except OSError:
        pass

    if skipped > 0:
        print(f"  WARN: skipped {skipped} unparseable lines in {jsonl_path}",
              file=sys.stderr)

    return {
        "timestamps": sorted(timestamps),
        "summary": summary,
        "title": None,  # Codex has no auto-title; falls back to summary
        "token_events": token_events,
    }


def aggregate_codex_tokens(token_events):
    """Sum Codex token events WITHOUT pricing (Codex = OpenAI models).

    Same return shape as aggregate_tokens() but cost is 0 and families is empty,
    so Codex contributes token counts to the totals while staying out of the
    Anthropic-equivalent $ figure. The caller marks the session cost as N/A.
    """
    overall = _empty_tokens()
    for ev in token_events:
        for k in TOKEN_KEYS:
            overall[k] += ev.get(k, 0)
    overall["total"] = sum(overall[k] for k in TOKEN_KEYS)
    return {"families": {}, "tokens": overall, "cost": 0.0}


# ── Transcript visualization (claude-code-transcripts) ───────────────────────

def _transcript_tool_cmd():
    """Return the base argv for claude-code-transcripts, or None if unavailable.

    Prefers a directly-installed binary; falls back to `uvx` (which fetches and
    caches the tool on first use). Simon Willison's claude-code-transcripts
    converts a Claude Code JSONL session into a static multi-page HTML bundle.
    """
    if shutil.which("claude-code-transcripts"):
        return ["claude-code-transcripts"]
    if shutil.which("uvx"):
        return ["uvx", "claude-code-transcripts"]
    return None


def _resolve_transcripts_base(project_paths, override):
    """Pick the directory to write transcript bundles under.

    Default: <project canonical path that exists on disk>/time-report-transcripts.
    Prefer a path under ~/codes when several match (the user's working copy),
    else the first existing path, else the first matched path.
    """
    if override:
        return Path(override).expanduser()
    existing = [p for p in project_paths if Path(p).is_dir()]
    codes = [p for p in existing if "/codes/" in p]
    chosen = (codes or existing or project_paths or [str(Path.cwd())])[0]
    return Path(chosen) / "time-report-transcripts"


def generate_transcripts(sessions, project_paths, override, log):
    """Render each session's JSONL into a static HTML bundle and attach a
    file:// link (`session["transcript"]`) pointing at its index.html.

    Idempotent: a session whose bundle already exists is skipped (transcripts
    of past sessions don't change). Returns the base output directory, or None
    if the tool is unavailable.
    """
    base_cmd = _transcript_tool_cmd()
    if not base_cmd:
        log("  WARN: claude-code-transcripts not found and `uvx` unavailable — "
            "skipping transcript generation. Install uv (https://docs.astral.sh/uv) "
            "or `uv tool install claude-code-transcripts`.")
        return None

    base = _resolve_transcripts_base(project_paths, override)
    base.mkdir(parents=True, exist_ok=True)
    log(f"Transcripts → {base}")

    targets = [s for s in sessions if s.get("jsonlPath")]

    def _one(s):
        out = base / (s.get("fullId") or s["id"])
        index = out / "index.html"
        if index.exists():
            return s, index, True, None  # already generated — reuse
        out.mkdir(parents=True, exist_ok=True)
        # The `json` subcommand converts a single session file. (It has no
        # --include-agents in the current tool; subagent calls still show inline
        # as Task tool uses, just without their nested sub-transcripts.)
        cmd = base_cmd + ["json", s["jsonlPath"], "-o", str(out)]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        except (subprocess.TimeoutExpired, OSError) as e:
            return s, index, False, str(e)
        if r.returncode != 0 or not index.exists():
            return s, index, False, (r.stderr or r.stdout or "no index.html").strip()[:200]
        return s, index, True, None

    ok = failed = 0
    # Modest parallelism — each call spawns a subprocess (releases the GIL).
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
        for s, index, success, err in ex.map(_one, targets):
            if success:
                s["transcript"] = index.as_uri()
                ok += 1
            else:
                s["transcript"] = None
                failed += 1
                log(f"  WARN: transcript failed for {s['id']}: {err}")
    log(f"Transcripts: {ok}/{len(targets)} ready"
        + (f", {failed} failed" if failed else ""))
    return base


def build_report_data(sessions, date_from, date_to, project_name, project_paths):
    """Build the JSON data structure for the HTML template."""
    data = {
        "meta": {
            "project": project_name,
            "projectPaths": project_paths,
            "dateRange": {
                "from": date_from.isoformat(),
                "to": date_to.isoformat(),
            },
            "generatedAt": datetime.now(timezone.utc).isoformat(),
        },
        "sessions": sessions,
    }
    return data


def generate_html(data, template_path):
    """Read template, inject data, return HTML string."""
    with open(template_path, encoding="utf-8") as f:
        template = f.read()

    json_data = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    html = template.replace("__DATA_PLACEHOLDER__", json_data)
    return html


def format_duration(minutes):
    """Format minutes as 'Xh Ym'."""
    h = int(minutes // 60)
    m = int(minutes % 60)
    if h == 0:
        return f"{m}m"
    return f"{h}h {m}m"


def compute_active_time(timestamps, threshold_ms=15 * 60 * 1000):
    """Compute active time from sorted timestamps using gap-threshold algorithm."""
    if len(timestamps) < 2:
        return 0
    active_ms = 0
    for i in range(1, len(timestamps)):
        gap = timestamps[i] - timestamps[i - 1]
        if gap <= threshold_ms:
            active_ms += gap
    return active_ms / 60000  # return minutes


def print_summary(data):
    """Print text summary to terminal."""
    meta = data["meta"]
    sessions = data["sessions"]

    total_sessions = len(sessions)
    total_messages = sum(s.get("messageCount", 0) for s in sessions)

    # Active time = UNION across parallel sessions, computed per local day.
    # Summing per-session active time double-counts overlapping wall-clock when
    # sessions run concurrently (Claude + Codex + worktrees), which can push the
    # naive total past 24h/day. Merge each day's timestamps into one stream.
    #
    # Group each event by the LOCAL calendar day it actually falls on, so a
    # session that crosses midnight is split across both days. This mirrors the
    # HTML timeline (template.html: `dayTs = s.timestamps.filter(... per day)`).
    # The previous version bucketed a whole session under its FIRST event's day,
    # which made the terminal total drift from the HTML for cross-midnight
    # sessions (e.g. 33h here vs the HTML's 29h).
    day_streams = {}   # day -> all timestamps that day (merged → union)
    day_sessions = {}  # day -> [per-session ts lists] (for the parallel sum)
    for s in sessions:
        ts = s.get("timestamps", [])
        if not ts:
            continue
        per_day = {}
        for t in ts:
            d = datetime.fromtimestamp(t / 1000).date()
            per_day.setdefault(d, []).append(t)
        for d, dts in per_day.items():
            day_streams.setdefault(d, []).extend(dts)
            day_sessions.setdefault(d, []).append(dts)

    active_days = set(day_streams)
    total_minutes = sum(compute_active_time(sorted(v)) for v in day_streams.values())
    # Parallel sum = naive per-session-per-day total (overlap counted); the
    # union above collapses overlapping parallel work, so total_minutes ≤ this.
    parallel_sum = sum(
        compute_active_time(sorted(dts))
        for day_list in day_sessions.values()
        for dts in day_list
    )

    # Aggregate tokens + cost across sessions
    overall = _empty_tokens()
    fam_cost = {}
    total_cost = 0.0
    for s in sessions:
        tok = s.get("tokens") or {}
        for k in TOKEN_KEYS:
            overall[k] += tok.get(k, 0)
        total_cost += s.get("cost", 0.0)
        for fam, fd in (s.get("families") or {}).items():
            fam_cost[fam] = fam_cost.get(fam, 0.0) + fd.get("cost", 0.0)
    overall_total = sum(overall[k] for k in TOKEN_KEYS)

    print(f"\n{'=' * 50}")
    print(f"  {meta['project']} — Time Report")
    print(f"  {meta['dateRange']['from']} → {meta['dateRange']['to']}")
    print(f"{'=' * 50}")
    print(f"  Sessions:     {total_sessions}")
    print(f"  Messages:     {total_messages}")
    print(f"  Active time:  {format_duration(total_minutes)} (wall-clock, 15min threshold)")
    print(f"  Parallel sum: {format_duration(parallel_sum)} (per-session, overlap counted)")
    print(f"  Active days:  {len(active_days)}")
    if active_days:
        avg = total_minutes / len(active_days)
        print(f"  Daily avg:    {format_duration(avg)}")
    print(f"{'-' * 50}")
    print(f"  Input:        {fmt_tokens(overall['input']):>10}")
    print(f"  Output:       {fmt_tokens(overall['output']):>10}")
    print(f"  Cache write:  {fmt_tokens(overall['cache_write_5m'] + overall['cache_write_1h']):>10}")
    print(f"  Cache read:   {fmt_tokens(overall['cache_read']):>10}")
    print(f"  Total tokens: {fmt_tokens(overall_total):>10}")
    print(f"  Est. cost:    {fmt_cost(total_cost):>10}  (API-equivalent)")
    if len(fam_cost) > 1:
        for fam in sorted(fam_cost, key=fam_cost.get, reverse=True):
            print(f"    - {fam:<8} {fmt_cost(fam_cost[fam]):>10}")
    print(f"{'=' * 50}\n")


def print_table(data):
    """Print a per-session table report with tokens and API-equivalent cost."""
    sessions = data["sessions"]
    if not sessions:
        return

    rows = []
    for s in sessions:
        ts = sorted(s.get("timestamps", []))
        when = datetime.fromtimestamp(ts[0] / 1000).strftime("%m-%d %H:%M") if ts else "—"
        tok = s.get("tokens") or {}
        summary = re.sub(r"\s+", " ", s.get("summary") or "").strip()
        cw = tok.get("cache_write_5m", 0) + tok.get("cache_write_1h", 0)
        rows.append({
            "when": when,
            "start": ts[0] if ts else 0,
            "summary": summary[:40],
            "msgs": s.get("messageCount", 0),
            "in": tok.get("input", 0),
            "out": tok.get("output", 0),
            "cw": cw,
            "cr": tok.get("cache_read", 0),
            "total": tok.get("total", 0),
            "cost": s.get("cost", 0.0),
            "costNA": s.get("costNA", False),
        })
    # Default sort: most token usage first (matches the HTML table default)
    rows.sort(key=lambda r: r["total"], reverse=True)

    header = (
        f"{'Time':<12} {'Msgs':>5} {'In':>7} {'Out':>7} "
        f"{'CacheW':>7} {'CacheR':>8} {'Total':>8} {'Cost':>9}  Summary"
    )
    print(header)
    print("-" * len(header))
    tot = {"msgs": 0, "in": 0, "out": 0, "cw": 0, "cr": 0, "total": 0, "cost": 0.0}
    for r in rows:
        # Codex sessions are not priced (OpenAI model) → show "—", omit from $ total
        cost_str = "—" if r["costNA"] else fmt_cost(r["cost"])
        print(
            f"{r['when']:<12} {r['msgs']:>5} {fmt_tokens(r['in']):>7} "
            f"{fmt_tokens(r['out']):>7} {fmt_tokens(r['cw']):>7} "
            f"{fmt_tokens(r['cr']):>8} {fmt_tokens(r['total']):>8} "
            f"{cost_str:>9}  {r['summary']}"
        )
        for k in tot:
            tot[k] += r[k]
    print("-" * len(header))
    print(
        f"{'TOTAL':<12} {tot['msgs']:>5} {fmt_tokens(tot['in']):>7} "
        f"{fmt_tokens(tot['out']):>7} {fmt_tokens(tot['cw']):>7} "
        f"{fmt_tokens(tot['cr']):>8} {fmt_tokens(tot['total']):>8} "
        f"{fmt_cost(tot['cost']):>9}"
    )
    print()


def parse_date_arg(s):
    """Parse a date string in YYYY-MM-DD format."""
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except ValueError:
        raise argparse.ArgumentTypeError(f"Invalid date: {s}. Use YYYY-MM-DD format.")


def parse_month_arg(s):
    """Parse YYYY-MM and return (first_day, last_day) as date objects."""
    m = re.match(r"^(\d{4})-(\d{2})$", s)
    if not m:
        return None
    year, month = int(m.group(1)), int(m.group(2))
    first = datetime(year, month, 1).date()
    last_day = calendar.monthrange(year, month)[1]
    last = datetime(year, month, last_day).date()
    return first, last


def build_parser():
    parser = argparse.ArgumentParser(
        description="Generate interactive HTML time reports for Claude Code sessions.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("args", nargs="*", help="project [YYYY-MM | from-date to-date]")
    parser.add_argument("--list", action="store_true", help="List all projects")
    parser.add_argument("--json", action="store_true", help="Output JSON instead of HTML")
    parser.add_argument("-o", "--output", help="Output file path")
    parser.add_argument("--open", action="store_true", default=True,
                        help="Auto-open report in browser (default)")
    parser.add_argument("--no-open", action="store_true", help="Don't auto-open report")
    parser.add_argument("--transcripts", action="store_true",
                        help="Render each in-range session into a static HTML "
                             "transcript (via claude-code-transcripts) and link "
                             "it from the Title column of the cost table.")
    parser.add_argument("--transcripts-dir",
                        help="Override the transcript output directory "
                             "(default: <project path>/time-report-transcripts).")
    return parser


def main():
    parser = build_parser()
    args = parser.parse_args()

    if args.list or not args.args:
        list_projects()
        return

    # Parse positional args: project [YYYY-MM | from to]
    positional = args.args
    project_keyword = positional[0]

    date_from = None
    date_to = None

    if len(positional) == 2:
        month_range = parse_month_arg(positional[1])
        if month_range:
            date_from, date_to = month_range
        else:
            date_from = parse_date_arg(positional[1])
            date_to = date_from
    elif len(positional) == 3:
        date_from = parse_date_arg(positional[1])
        date_to = parse_date_arg(positional[2])
    elif len(positional) == 1:
        today = datetime.now().date()
        date_from = today.replace(day=1)
        date_to = today
    else:
        parser.error("Too many arguments. Use: <project> [YYYY-MM | from to]")

    # Resolve project from DB
    rows, project_paths, from_ms, to_ms = resolve_project(project_keyword, date_from, date_to)
    if not rows:
        # Check if project exists at all (without date filter)
        conn = _open_db()
        any_match = conn.execute(
            "SELECT COUNT(*) FROM projects WHERE LOWER(canonical_path) LIKE ?",
            (f"%{project_keyword.lower()}%",)
        ).fetchone()[0]
        conn.close()

        if any_match:
            print(f"No sessions found for '{project_keyword}' in {date_from} → {date_to}.")
            print(f"Sessions: 0")
            sys.exit(0)
        else:
            print(f"Error: No project matching '{project_keyword}' found.", file=sys.stderr)
            print("\nAvailable projects:", file=sys.stderr)
            list_projects()
            sys.exit(1)

    # Use stderr for status when JSON output is requested
    log = (lambda msg: print(msg, file=sys.stderr)) if args.json else print

    log(f"Project: {project_keyword}")
    log(f"Date range: {date_from} → {date_to}")
    log(f"DB sessions: {len(rows)}")

    # Dedup: the same session is registered under every project path it touched,
    # so a session that ran in a git worktree under the repo (e.g.
    # <repo>/.claude/worktrees/<name>) is matched twice by our `LIKE %keyword%`
    # query — once for the repo path, once for the worktree path. Both rows carry
    # the same external_id and resolve to the same transcript file, so counting
    # both double-counts that session's tokens/cost/message totals. Keep one row
    # per external_id. (This does NOT change Active Time: the per-day union is
    # idempotent over identical timestamps. Codex and Claude Code never share an
    # external_id, so this is also safe once Codex sessions are mixed in.)
    seen_ids = set()
    deduped_rows = []
    for row in rows:
        external_id = row[0]
        if external_id in seen_ids:
            continue
        seen_ids.add(external_id)
        deduped_rows.append(row)
    dup_count = len(rows) - len(deduped_rows)
    rows = deduped_rows
    if dup_count:
        log(f"Deduped: dropped {dup_count} duplicate session row(s) "
            f"(same session under repo + worktree paths) → {len(rows)} unique")

    # Build JSONL indexes (Claude Code under ~/.claude, Codex under ~/.codex)
    jsonl_index = _build_jsonl_index()
    codex_index = _build_codex_index()

    # Extract timestamps from JSONL files
    sessions = []
    # Diagnostics for the "matched in index but unreadable" case
    missing_codex = 0      # Codex sessions: transcript not found under ~/.codex
    missing_claude = 0     # Claude Code sessions with no local transcript (cleaned up)
    empty_jsonl = 0        # file present but no parseable timestamps
    for external_id, proj_path, branch, msg_count, first_at, last_at, source_type in rows:
        is_codex = (source_type == "codex")
        jsonl_path = jsonl_index.get(external_id)
        if not jsonl_path and is_codex:
            jsonl_path = codex_index.get(external_id)
        if not jsonl_path:
            if is_codex:
                missing_codex += 1
            else:
                missing_claude += 1
            continue

        result = (extract_codex_timestamps(jsonl_path) if is_codex
                  else extract_timestamps(jsonl_path))
        if not result["timestamps"]:
            empty_jsonl += 1
            continue

        # Filter timestamps to date range
        ts_in_range = [t for t in result["timestamps"] if from_ms <= t < to_ms]
        if not ts_in_range:
            continue

        # Aggregate token usage for events that fall inside the range. Codex runs
        # OpenAI models, so its tokens are counted but NOT priced (cost N/A) —
        # keeps the headline $ a pure Anthropic-API-equivalent figure.
        events_in_range = [
            ev for ev in result.get("token_events", [])
            if ev["ts"] is not None and from_ms <= ev["ts"] < to_ms
        ]
        usage = (aggregate_codex_tokens(events_in_range) if is_codex
                 else aggregate_tokens(events_in_range))

        sessions.append({
            "id": external_id[:8],
            "fullId": external_id,
            "source": source_type,
            "costNA": is_codex,  # cost not computed (OpenAI model) → render "—"
            "title": result.get("title") or "",
            "summary": result["summary"] or f"(session {external_id[:8]})",
            "branch": branch,
            "messageCount": msg_count or len(ts_in_range),
            "created": "",
            "timestamps": ts_in_range,
            "tokens": usage["tokens"],
            "cost": usage["cost"],
            "families": usage["families"],
            "jsonlPath": jsonl_path,
            "transcript": None,
        })

    log(f"Sessions with data: {len(sessions)}")

    # Always surface unreadable sessions, even when others were read — otherwise
    # a silently-dropped subset (e.g. Codex sessions whose transcript is gone)
    # makes the totals look complete when they aren't.
    if missing_codex or missing_claude or empty_jsonl:
        log("  Note: some matched sessions had no usable transcript and were "
            "excluded from the totals:")
        if missing_codex:
            log(f"    • {missing_codex} Codex session(s) — no transcript found "
                f"under ~/.codex (older sessions may have been cleaned up).")
        if missing_claude:
            log(f"    • {missing_claude} Claude Code session(s) — no local "
                f"transcript found (likely past Claude Code's ~30-day cleanup).")
        if empty_jsonl:
            log(f"    • {empty_jsonl} transcript(s) present but with no parseable "
                f"timestamps.")

    if not sessions:
        log("No sessions with readable transcripts in this date range.")
        if missing_codex or missing_claude or empty_jsonl:
            log("  Token/cost figures require the original JSONL transcript, so "
                "this period can't be reported. Recent ranges (within the "
                "retention window) work fully.")
        if args.json:
            print(json.dumps({"meta": {"project": project_keyword,
                                        "dateRange": {"from": str(date_from), "to": str(date_to)}},
                               "sessions": []}, indent=2))
        sys.exit(0)

    # Optionally render per-session transcript bundles and link them in the table
    if args.transcripts:
        generate_transcripts(sessions, project_paths, args.transcripts_dir, log)

    # Build report data
    project_display = _get_project_name(project_paths[0]) if project_paths else project_keyword
    report_data = build_report_data(sessions, date_from, date_to, project_display, project_paths)

    # JSON output mode
    if args.json:
        print(json.dumps(report_data, indent=2, ensure_ascii=False))
        return

    # Generate HTML
    template_path = Path(__file__).parent.parent / "references" / "template.html"
    if not template_path.exists():
        print(f"Error: Template not found at {template_path}", file=sys.stderr)
        sys.exit(1)

    html = generate_html(report_data, template_path)

    # Output path
    range_str = f"{date_from}_{date_to}"
    default_output = f"/tmp/time-report-{project_display}-{range_str}.html"
    output_path = args.output or default_output

    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html)

    print(f"Report saved to: {output_path}")

    # Print summary + per-session table report
    print_summary(report_data)
    print_table(report_data)

    # Auto-open
    if not args.no_open:
        if sys.platform == "darwin":
            subprocess.run(["open", output_path], check=False)
        elif sys.platform == "linux":
            subprocess.run(["xdg-open", output_path], check=False)
        elif sys.platform == "win32":
            os.startfile(output_path)


if __name__ == "__main__":
    main()
