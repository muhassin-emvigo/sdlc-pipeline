#!/usr/bin/env python3
"""PostToolUse hook (matcher: Task).

Logs token usage of every subagent dispatch to .claude/token-usage.csv.
Receives the hook payload as JSON on stdin. Fails silently — a broken
tracker must never block the pipeline.
"""
import csv
import datetime
import json
import os
import sys


def find_usage(obj):
    """Recursively find the first dict that looks like an API usage block."""
    if isinstance(obj, dict):
        if "input_tokens" in obj or "output_tokens" in obj:
            return obj
        for v in obj.values():
            r = find_usage(v)
            if r:
                return r
    elif isinstance(obj, list):
        for v in obj:
            r = find_usage(v)
            if r:
                return r
    return None


def main():
    data = json.load(sys.stdin)
    if data.get("tool_name") != "Task":
        return

    tool_input = data.get("tool_input") or {}
    tool_response = data.get("tool_response") or {}

    usage = find_usage(tool_response) or {}
    inp = usage.get("input_tokens") or 0
    out = usage.get("output_tokens") or 0
    cache_read = usage.get("cache_read_input_tokens") or 0
    cache_create = usage.get("cache_creation_input_tokens") or 0

    total = None
    if isinstance(tool_response, dict):
        total = tool_response.get("totalTokens")
    if not total:
        total = inp + out + cache_read + cache_create

    project_dir = os.environ.get("CLAUDE_PROJECT_DIR", ".")
    csv_path = os.path.join(project_dir, ".claude", "token-usage.csv")
    is_new = not os.path.exists(csv_path)

    with open(csv_path, "a", newline="") as f:
        w = csv.writer(f)
        if is_new:
            w.writerow([
                "timestamp", "session_id", "agent", "description",
                "input_tokens", "output_tokens",
                "cache_read_tokens", "cache_creation_tokens", "total_tokens",
            ])
        w.writerow([
            datetime.datetime.now().isoformat(timespec="seconds"),
            data.get("session_id", ""),
            tool_input.get("subagent_type", "unknown"),
            (tool_input.get("description") or "")[:80],
            inp, out, cache_read, cache_create, total,
        ])


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass  # never block the pipeline
    sys.exit(0)
