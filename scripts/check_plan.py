#!/usr/bin/env python3
"""
check_plan.py - stack-agnostic plan consistency linter for the sdlc-pipeline plugin.

WHAT IT IS FOR
--------------
Reviewer agents verify *conformance*: "does the plan say what the contract says?" That cannot
find an ABSENCE - a value returned but never stored, a store written to but never created, a
security requirement dropped in compression. Across six measured runs every defect class below
was written as an instruction to a reviewer and shipped anyway at least once.

This script checks the absences. It reads only the plan directory, the contract, and optionally
the original spec. It knows nothing about any particular project, database, or framework.

USAGE
-----
    python check_plan.py <plan-dir> <contract.md> [original-spec.md]

Exit 0 = clean · 1 = findings printed · 2 = could not run (NOT a pass).

Findings are formatted to paste straight into a `planner` targeted-mode dispatch.

TUNING PER ORG OR REPO
----------------------
Drop a `.plan-lint.json` next to the plan directory, the contract, or in the repo root:

    {
      "disable": ["upsert-without-uniqueness"],
      "security_terms": ["csrf", "hmac", "pii", "gdpr"],
      "test_file_hint": "\\\\.(test|spec)\\\\.[jt]sx?$",
      "min_tests_per_task": 3
    }

Everything is optional. Unknown keys are ignored.

DESIGN RULES FOR ADDING CHECKS
------------------------------
1. No project, table, column, vendor or service names. Ever.
2. Detect idioms by CLASS (see IDIOMS below), never by one dialect's keyword.
3. A check that cannot tell whether it applies must stay silent. False positives make a linter
   ignorable, which is worse than not having one.
4. Every check needs a one-line explanation of the failure mode in its message, because the
   output is read by an agent that has to act on it.
"""

import json
import os
import re
import sys

# --------------------------------------------------------------------------- #
# Idiom classes. Add dialects here, never in the checks themselves.
# --------------------------------------------------------------------------- #

IDIOMS = {
    # writing to a durable store, any technology
    "persist": [
        r"\bINSERT\s+INTO\b", r"\bUPDATE\s+\w+\s+SET\b", r"\bUPSERT\b",
        r"ON\s+DUPLICATE\s+KEY\s+UPDATE", r"ON\s+CONFLICT\b",          # MySQL / Postgres
        r"\.(?:save|create|update|updateOne|updateMany|insertOne|insertMany|"
        r"findOneAndUpdate|bulkWrite|upsert|put|set|persist|merge)\s*\(",  # ORM / ODM method
        # Bare repository functions - the common JS shape. `upsertCredentials(...)` has no dot,
        # so a dot-only pattern silently matched nothing and a written field looked unwritten.
        r"\b\w*(?:upsert|persist|insert|store|save|writ)\w*\s*\(",
        r"\bMERGE\s+INTO\b",
        r"\b(?:writ|sav|persist|stor)\w*\s+\w[\w.\s`]{0,30}?\b(?:in|into|to)\s+"
        r"(?:the\s+)?(?:db|database|store|table|collection|cache|index|repo|repository)\b",
    ],
    # declaring that a store exists
    "schema_ddl": [
        r"\bCREATE\s+TABLE\b", r"\bALTER\s+TABLE\b", r"\bCREATE\s+COLLECTION\b",
        r"\bmigrations?\b", r"\.sql\b", r"schema\.(?:sql|prisma|graphql|ts|js|json)\b",
        r"\bprisma\b", r"\bknex\b", r"\bsequelize\b", r"\btypeorm\b", r"\bmongoose\b",
        r"\bcreateIndex\b", r"\bensureIndex\b", r"\bdefine\s*\(",
    ],
    # an operation whose correctness depends on a uniqueness guarantee
    "upsert": [
        r"ON\s+DUPLICATE\s+KEY\s+UPDATE", r"ON\s+CONFLICT\b", r"\bUPSERT\b",
        r"\bupsert\w*\s*\(", r"upsert\s*:\s*true", r"\bfindOneAndUpdate\b",
        r"\bMERGE\s+INTO\b", r"\bidempotent\s+(?:insert|write)\b",
    ],
    # declaring that uniqueness
    "uniqueness": [
        r"\bUNIQUE\s+(?:KEY|INDEX|CONSTRAINT)\b", r"\bUNIQUE\s*\(",
        r"\bPRIMARY\s+KEY\b", r"@@unique", r"@unique", r"\bunique\s*:\s*true",
        r"\bcreateIndex\([^)]*unique", r"\bALTER\s+TABLE[^.\n]{0,80}\bUNIQUE\b",
    ],
    # reading previously-stored state back
    "read_back": [
        r"\bSELECT\b", r"\.(?:find|findOne|findFirst|findUnique|get|load|fetch|query)\s*\(",
        r"\bread[^.\n]{0,40}\b(?:stored|existing|previously|persisted)\b",
        r"\bresolve[^.\n]{0,40}\bfrom\s+(?:the\s+)?(?:stored|existing|db|database|record)\b",
        r"\blook\s*up\b", r"\bexisting\s+\w+\s+(?:is|are)\s+(?:read|loaded|fetched)\b",
    ],
    # a second execution of the same flow
    "rerun": [
        r"\bre-?auth", r"\bre-?install", r"\bre-?run", r"\bretry\b", r"\bidempoten",
        r"\balready\s+(?:exists|registered|installed|present)\b",
        r"\bsecond\s+(?:time|call|run)\b", r"\bduplicate\s+(?:request|delivery|event)\b",
        r"\bat[- ]least[- ]once\b", r"\breplay",
    ],
}

DEFAULT_SECURITY_TERMS = [
    "csrf", "xsrf", "hmac", "nonce", "replay", "signature verif", "verify signature",
    "sanitiz", "injection", "xss", "encrypt", "single-use", "rate limit",
    "constant-time", "timing attack", "authoriz", "least privilege", "pii",
]

DEFAULT_CONFIG = {
    "disable": [],
    "security_terms": DEFAULT_SECURITY_TERMS,
    "test_file_hint": r"\.(?:test|spec)\.[jt]sx?$|__tests__/|\btest/",
    "min_tests_per_task": 1,
}


def any_idiom(kind, text):
    return any(re.search(p, text, re.I) for p in IDIOMS[kind])


def idiom_near(kind, token, text, window=140):
    """Does an idiom of this class appear within `window` chars of `token`?"""
    tok = re.escape(token)
    for p in IDIOMS[kind]:
        if re.search(p + r"[^\n]{0," + str(window) + r"}\b" + tok + r"\b", text, re.I):
            return True
        if re.search(r"\b" + tok + r"\b[^\n]{0," + str(window) + r"}" + p, text, re.I):
            return True
    return False


def read(p):
    try:
        with open(p, encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError:
        return ""


def load_config(*dirs):
    cfg = dict(DEFAULT_CONFIG)
    for d in dirs:
        if not d:
            continue
        p = os.path.join(d, ".plan-lint.json")
        if os.path.exists(p):
            try:
                cfg.update(json.loads(read(p)))
            except ValueError:
                print(f"warning: {p} is not valid JSON, ignoring", file=sys.stderr)
    return cfg


# --------------------------------------------------------------------------- #
# Tasks are parsed once into a structure the checks share.
# --------------------------------------------------------------------------- #

class Task:
    def __init__(self, name, text):
        self.name = name
        self.text = text
        self.num = int(m.group(1)) if (m := re.search(r"task-(\d+)", name)) else None
        self.exports = set(re.findall(r"`([A-Za-z_$][\w$]{2,})\s*\(", self._section("Exports")))
        self.calls = set(re.findall(r"`([A-Za-z_$][\w$]{2,})\s*\(", text)) - self.exports
        self.files = re.findall(r"`([^`\n]+\.[A-Za-z0-9]{1,6})`", self._section("Files") or text)
        # Collect EVERY number on the depends-on line. A non-greedy match stopped at the first,
        # so "depends-on: Task 1, Task 9" silently lost the dangling reference to Task 9.
        self.depends = set()
        for line in re.findall(r"^[^\n]*depends-on:([^\n]*)$", text, re.M | re.I):
            if re.search(r"\bnone\b", line, re.I):
                continue
            self.depends |= {int(n) for n in re.findall(r"\b(\d{1,3})\b", line)}
        self.n_tests = len(re.findall(r"^\s*\d+\.", self._section("Tests"), re.M))

    def _section(self, title):
        m = re.search(r"##+\s*" + title + r"\b(.*?)(?=\n##+\s|\Z)", self.text, re.S | re.I)
        return m.group(1) if m else ""


def parse_tasks(plan_dir):
    out = []
    for f in sorted(os.listdir(plan_dir)):
        if f.startswith("task-") and f.endswith(".md"):
            out.append(Task(f, read(os.path.join(plan_dir, f))))
    return out


# --------------------------------------------------------------------------- #
# Checks. Each appends (id, severity, message).
# --------------------------------------------------------------------------- #

def c_upsert_without_uniqueness(ctx, out):
    """An upsert is only idempotent if something guarantees uniqueness on its key."""
    if not any_idiom("upsert", ctx["tasks_text"]):
        return
    if any_idiom("uniqueness", ctx["tasks_text"]):
        return
    out.append((
        "upsert-without-uniqueness", "CRITICAL",
        "A task performs an upsert / conflict-update, but no task declares a uniqueness "
        "guarantee (UNIQUE constraint, unique index, primary key, @unique). Without one the "
        "conflict is never detected and every re-run creates a duplicate record instead of "
        "updating. Name the task that declares it and the field(s) it covers."
    ))


def c_store_never_created(ctx, out):
    """Something is written to a durable store that no task ever creates."""
    if not any_idiom("persist", ctx["tasks_text"]):
        return
    if any_idiom("schema_ddl", ctx["tasks_text"]):
        return
    if not re.search(r"\b(?:table|collection|schema|database|migration|index)\b",
                     ctx["contract"], re.I):
        return
    out.append((
        "store-never-created", "CRITICAL",
        "Tasks write to a persistent store and the contract describes one, but no task creates "
        "it - no DDL, migration, schema file or ORM model definition anywhere in the plan. Every "
        "write fails against a fresh environment. Add the creation step to the scaffolding task, "
        "including any uniqueness constraint the writes depend on."
    ))


def c_contract_persist_unimplemented(ctx, out):
    """Contract sentences that say "store X in Y" need a task that writes Y."""
    seen = set()
    for line in ctx["contract"].split("\n"):
        if line.strip().startswith("|") or line.strip().startswith(">"):
            continue  # tables and quotes use these verbs descriptively
        if not re.search(r"\b(?:stor|persist|sav|writ)\w*\b", line, re.I):
            continue
        if not re.search(r"\b(?:in|into|to)\b", line, re.I):
            continue
        # Prefer an explicit `store.field` destination, then "as `alias`", then the object.
        m = (re.search(r"\b(?:in|into|to)\s+`?[\w-]+\.(\w+)`?", line)
             or re.search(r"\bas\s+`?(\w+)`?", line)
             or re.search(r"\b(?:stor|persist|sav|writ)\w*\s+(?:the\s+)?(?:returned\s+)?"
                          r"`?([A-Za-z_$][\w$.]{2,})`?", line, re.I))
        if not m:
            continue
        target = m.group(1).lstrip("_.")
        if len(target) < 3 or target.lower() in seen:
            continue
        if target.lower() in ("the", "this", "that", "it", "them", "value", "result", "data"):
            continue
        seen.add(target.lower())
        if not idiom_near("persist", target, ctx["tasks_text"]):
            out.append((
                "contract-persist-unimplemented", "IMPORTANT",
                f"The contract says: \"{line.strip()[:110]}\" - no task performs a write of "
                f"`{target}`. Returning a value from a function is not persisting it; name the "
                f"task and the write that stores it."
            ))


def c_security_lost_in_distillation(ctx, out):
    """Compression is lossy. Anything security-relevant that vanished must come back."""
    if not ctx["spec"]:
        return
    for kw in ctx["cfg"]["security_terms"]:
        n_spec = len(re.findall(re.escape(kw), ctx["spec"], re.I))
        if n_spec and not re.search(re.escape(kw), ctx["contract"], re.I):
            out.append((
                "security-lost-in-distillation", "CRITICAL",
                f"'{kw}' appears {n_spec}x in the original spec and 0x in the contract, so no "
                f"downstream agent can know about it - they are forbidden from reading the "
                f"original. Re-dispatch the distiller in top-up mode for this term before "
                f"planning continues."
            ))


def c_unresolved_wiring(ctx, out):
    """A task cannot call what no task exports."""
    exported = set()
    for t in ctx["tasks"]:
        exported |= t.exports
    builtins = {
        "require", "import", "expect", "describe", "it", "test", "beforeEach", "afterEach",
        "jest", "fn", "mock", "spyOn", "resolve", "reject", "then", "catch", "map", "filter",
        "forEach", "push", "join", "split", "parse", "stringify", "log", "error", "warn",
        "startsWith", "endsWith", "includes", "toString", "valueOf", "constructor",
    }
    for t in ctx["tasks"]:
        missing = sorted(
            c for c in t.calls
            if c not in exported and c not in builtins and not c[0].isupper()
            and re.search(r"`" + re.escape(c) + r"\s*\([^)]*\)`[^\n]{0,40}"
                          r"(?:from|per|via)?\s*(?:Task|task)?", t.text)
        )
        # Exclude names that appear inside a destructured dependency-injection parameter list -
        # those are parameters the task receives, not calls to undeclared functions. This
        # produced a false positive on `handleCallback({..}, { fetchBusinessLocations, .. })`.
        di = set()
        for blk in re.findall(r"\{([^{}]*)\}", t.text):
            if "," in blk and "(" not in blk:
                di |= {w.strip().strip("`") for w in blk.split(",")}
        missing = [m for m in missing if m not in di]
        # only report symbols that look like plan-owned helpers: appear in a wiring context
        missing = [m for m in missing
                   if re.search(r"(?:call|invoke|wire|use)s?\b[^\n]{0,60}`" + re.escape(m),
                                t.text, re.I)]
        if missing:
            out.append((
                "unresolved-wiring", "IMPORTANT",
                f"{t.name} calls " + ", ".join(f"`{m}()`" for m in missing)
                + " but no task declares them under `## Exports`. Either add the export to the "
                "owning task or correct the call - otherwise each implementer invents its own "
                "signature and the wiring task has nothing to wire against."
            ))


def c_rerun_reads_nothing(ctx, out):
    """A second-execution branch that reads no stored state is dead code."""
    if not any_idiom("rerun", ctx["contract"]):
        return
    if any_idiom("read_back", ctx["tasks_text"]):
        return
    out.append((
        "rerun-path-reads-nothing", "IMPORTANT",
        "The contract describes a repeat-execution path (re-auth, retry, reinstall, duplicate "
        "delivery or idempotency), but no task reads previously-stored state back - no query, "
        "lookup or 'resolve from existing record'. The branch cannot behave differently on the "
        "second run without reading what the first one wrote."
    ))


def c_dangling_dependencies(ctx, out):
    """depends-on must name tasks that exist, and must not cycle."""
    nums = {t.num for t in ctx["tasks"] if t.num is not None}
    if not nums:
        return
    for t in ctx["tasks"]:
        bad = sorted(d for d in t.depends if d not in nums)
        if bad:
            out.append((
                "dangling-dependency", "IMPORTANT",
                f"{t.name} declares depends-on Task " + ", ".join(str(b) for b in bad)
                + ", which does not exist in this plan. Either the task is missing or the "
                "reference is stale."
            ))
    # cycle detection
    graph = {t.num: {d for d in t.depends if d in nums} for t in ctx["tasks"] if t.num}
    state = {}

    def walk(n, path):
        if state.get(n) == "done":
            return
        if state.get(n) == "open":
            out.append((
                "dependency-cycle", "CRITICAL",
                "Circular dependency between tasks " + " -> ".join(str(p) for p in path + [n])
                + ". Execution order is impossible as declared."
            ))
            return
        state[n] = "open"
        for d in graph.get(n, ()):
            walk(d, path + [n])
        state[n] = "done"

    for n in list(graph):
        walk(n, [])


def c_task_index_matches_files(ctx, out):
    """Every indexed task needs a file and vice versa."""
    ov = ctx["overview"]
    if not ov:
        return
    m = re.search(r"##+\s*Task index\b(.*?)(?=\n##+\s|\Z)", ov, re.S | re.I)
    if not m:
        return
    indexed = {int(n) for n in re.findall(r"^\|\s*(\d+)\s*\|", m.group(1), re.M)}
    present = {t.num for t in ctx["tasks"] if t.num is not None}
    if indexed - present:
        out.append((
            "indexed-task-has-no-file", "CRITICAL",
            "The Task index lists task(s) "
            + ", ".join(str(n) for n in sorted(indexed - present))
            + " with no corresponding task-NN file. The plan claims work it does not specify."
        ))
    if present - indexed:
        out.append((
            "task-file-not-indexed", "IMPORTANT",
            "Task file(s) " + ", ".join(str(n) for n in sorted(present - indexed))
            + " are not in the Task index, so anything reading only the overview will miss them."
        ))


def c_tests_and_test_files(ctx, out):
    """With TDD on, a task needs numbered tests and a test file to put them in."""
    tdd_on = bool(re.search(r"TDD:\s*on", ctx["overview"] + ctx["tasks_text"], re.I))
    if not tdd_on:
        return
    hint = ctx["cfg"]["test_file_hint"]
    floor = int(ctx["cfg"]["min_tests_per_task"])
    thin, no_file = [], []
    for t in ctx["tasks"]:
        if t.n_tests < floor:
            thin.append(f"{t.name} ({t.n_tests})")
        if t.files and not any(re.search(hint, f) for f in t.files):
            no_file.append(t.name)
    if thin:
        out.append((
            "tdd-task-without-tests", "CRITICAL",
            "TDD is on but these tasks specify fewer than "
            f"{floor} numbered test case(s): " + ", ".join(thin)
            + ". The implementer writes these before any code, so an unspecified test is an "
            "unspecified requirement."
        ))
    if no_file:
        out.append((
            "tdd-task-without-test-file", "IMPORTANT",
            "TDD is on but these tasks list no test file in their Files section: "
            + ", ".join(no_file) + ". State where the tests live."
        ))


def c_output_derived_field_never_written(ctx, out):
    """Fields the contract documents as coming from an operation's OUTPUT, that no task writes.

    Stack-agnostic. A field whose value is described as "from the X response" / "returned by" /
    "result of" cannot be part of a record assembled before that call - so a bulk write of the
    record does not cover it. If no task writes it near its name, either a later write is missing
    or the plan handles it somewhere this check cannot see.

    Reported as IMPORTANT rather than CRITICAL: the check cannot always tell a bulk write from a
    missing one, so it asks for confirmation instead of asserting a defect. Being specific about
    WHICH fields keeps the ask cheap to answer.
    """
    tables = re.findall(r"(\|[^\n]*\|\n\|[\s:|-]+\|\n(?:\|[^\n]*\|\n?)+)", ctx["contract"])
    audit_ish = re.compile(r"^(?:id|_id|seq\w*|created_?at|updated_?at|createdAt|updatedAt|"
                           r"version|__v)$", re.I)
    output_src = re.compile(
        r"\bfrom\b[^|]{0,60}\b(?:response|reply|result|output|payload)\b"
        r"|\breturn(?:ed|s)?\b[^|]{0,40}\b(?:by|from)\b"
        r"|\bresult\s+of\b"
        r"|\bfrom\s+`?(?:POST|GET|PUT|PATCH|DELETE|mutation|query|rpc)\b", re.I)
    suspects = []
    for tbl in tables:
        rows = [r for r in tbl.strip().split("\n") if r.strip().startswith("|")]
        if len(rows) < 3:
            continue
        header = rows[0].lower()
        if not re.search(r"\b(?:column|field|property|attribute)\b", header):
            continue
        if not re.search(r"\b(?:type|null|nullable|notes|description)\b", header):
            continue
        for r in rows[2:]:
            cells = [c.strip() for c in r.strip().strip("|").split("|")]
            if len(cells) < 2:
                continue
            name = cells[0].strip("`")
            notes = " ".join(cells[1:])
            if not re.match(r"^\w{3,}$", name) or audit_ish.match(name):
                continue
            if not output_src.search(notes):
                continue
            if idiom_near("persist", name, ctx["tasks_text"]):
                continue
            suspects.append(name)
    suspects = sorted(set(suspects))
    if suspects:
        shown = ", ".join(f"`{s}`" for s in suspects[:10])
        more = f" (+{len(suspects) - 10} more)" if len(suspects) > 10 else ""
        out.append((
            "output-derived-field-never-written", "IMPORTANT",
            f"The contract documents {shown}{more} as coming from an operation's output, but no "
            "task writes them. A value returned by a function is not persisted by returning it. "
            "For each: name the task and the write that stores it, or state which bulk write "
            "already covers it."
        ))


CHECKS = [
    c_upsert_without_uniqueness,
    c_store_never_created,
    c_contract_persist_unimplemented,
    c_security_lost_in_distillation,
    c_unresolved_wiring,
    c_rerun_reads_nothing,
    c_dangling_dependencies,
    c_task_index_matches_files,
    c_tests_and_test_files,
    c_output_derived_field_never_written,
]


def main():
    if len(sys.argv) < 3:
        print(__doc__)
        return 2
    plan_dir, contract_path = sys.argv[1], sys.argv[2]
    spec_path = sys.argv[3] if len(sys.argv) > 3 else None

    if not os.path.isdir(plan_dir):
        print(f"PLAN LINT: not a directory: {plan_dir}")
        return 2

    tasks = parse_tasks(plan_dir)
    if not tasks:
        print(f"PLAN LINT: no task-*.md files in {plan_dir} - nothing to check")
        return 2
    contract = read(contract_path)
    if not contract.strip():
        print(f"PLAN LINT: could not read contract at {contract_path} - contract-based checks "
              f"skipped, so this result is NOT a pass")
        return 2

    overview = ""
    for cand in ("00-overview.md", "overview.md", "00-plan.md"):
        if os.path.exists(os.path.join(plan_dir, cand)):
            overview = read(os.path.join(plan_dir, cand))
            break

    ctx = {
        "tasks": tasks,
        "tasks_text": "\n".join(t.text for t in tasks) + "\n" + overview,
        "contract": contract,
        "spec": read(spec_path) if spec_path else "",
        "overview": overview,
        "cfg": load_config(plan_dir, os.path.dirname(contract_path) or ".", "."),
    }

    findings = []
    for chk in CHECKS:
        if chk.__name__.replace("c_", "").replace("_", "-") in ctx["cfg"]["disable"]:
            continue
        try:
            chk(ctx, findings)
        except Exception as e:  # a broken check must never block a pipeline
            print(f"warning: check {chk.__name__} errored: {e}", file=sys.stderr)

    findings = [f for f in findings if f[0] not in ctx["cfg"]["disable"]]
    seen, uniq = set(), []
    for fid, sev, msg in findings:
        if (fid, msg[:60]) in seen:
            continue
        seen.add((fid, msg[:60]))
        uniq.append((fid, sev, msg))

    if not uniq:
        print(f"PLAN LINT: clean ({len(tasks)} tasks, {len(CHECKS)} checks)")
        return 0

    order = {"CRITICAL": 0, "IMPORTANT": 1}
    uniq.sort(key=lambda f: order.get(f[1], 2))
    print(f"PLAN LINT: {len(uniq)} finding(s) across {len(tasks)} tasks\n")
    for i, (fid, sev, msg) in enumerate(uniq, 1):
        print(f"{i}. {sev} [{fid}] {msg}\n")
    return 1


if __name__ == "__main__":
    sys.exit(main())
