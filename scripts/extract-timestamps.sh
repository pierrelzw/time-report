#!/bin/bash
# Extract timestamps from JSONL session files (user/assistant records only)
# Usage: extract-timestamps.sh <file1.jsonl> [file2.jsonl ...]
# Output: one timestamp per line (epoch ms)

for f in "$@"; do
  if [ ! -f "$f" ]; then
    echo "WARN: $f not found" >&2
    continue
  fi
  grep -E '"type"\s*:\s*"(user|assistant)"' "$f" | \
    grep -oE '"timestamp"\s*:\s*"[^"]+"' | \
    sed 's/"timestamp"\s*:\s*"//;s/"//' | \
    while read -r ts; do
      # Convert ISO to epoch ms using date command
      if command -v gdate &>/dev/null; then
        gdate -d "$ts" +%s000 2>/dev/null
      else
        date -j -f "%Y-%m-%dT%H:%M:%S" "${ts%%.*}" +%s000 2>/dev/null
      fi
    done
done
