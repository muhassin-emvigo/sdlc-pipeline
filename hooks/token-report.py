#!/usr/bin/env python3
"""Summarize .claude/token-usage.csv per agent.

Usage:
  python3 .claude/hooks/token-report.py               # everything ever logged
  python3 .claude/hooks/token-report.py <session_id>  # one workflow run
  python3 .claude/hooks/token-report.py --last        # most recent session only
"""
import csv
import os
import sys
from collections import defaultdict

project_dir = os.environ.get("CLAUDE_PROJECT_DIR", ".")
csv_path = os.path.join(project_dir, ".claude", "token-usage.csv")

if not os.path.exists(csv_path):
    print("No token usage logged yet (.claude/token-usage.csv missing).")
    sys.exit(0)

with open(csv_path, newline="") as f:
    rows = list(csv.DictReader(f))

if not rows:
    print("Token log is empty.")
    sys.exit(0)

arg = sys.argv[1] if len(sys.argv) > 1 else None
if arg == "--last":
    last_session = rows[-1]["session_id"]
    rows = [r for r in rows if r["session_id"] == last_session]
elif arg:
    rows = [r for r in rows if r["session_id"].startswith(arg)]
    if not rows:
        print(f"No rows for session '{arg}'.")
        sys.exit(0)

per_agent = defaultdict(lambda: {"runs": 0, "in": 0, "out": 0, "cache": 0, "total": 0})
for r in rows:
    a = per_agent[r["agent"]]
    a["runs"] += 1
    a["in"] += int(r["input_tokens"] or 0)
    a["out"] += int(r["output_tokens"] or 0)
    a["cache"] += int(r["cache_read_tokens"] or 0) + int(r["cache_creation_tokens"] or 0)
    a["total"] += int(r["total_tokens"] or 0)

print(f"{'agent':<20} {'runs':>5} {'input':>10} {'output':>10} {'cache':>12} {'total':>12}")
print("-" * 73)
grand = 0
for agent, a in sorted(per_agent.items(), key=lambda kv: -kv[1]["total"]):
    grand += a["total"]
    print(f"{agent:<20} {a['runs']:>5} {a['in']:>10,} {a['out']:>10,} {a['cache']:>12,} {a['total']:>12,}")
print("-" * 73)
print(f"{'WORKFLOW TOTAL (subagents)':<47} {grand:>25,}")
print("\nNote: orchestrator tokens are not in this table — run /cost in the")
print("session for the grand total; orchestrator = /cost total minus the above.")
