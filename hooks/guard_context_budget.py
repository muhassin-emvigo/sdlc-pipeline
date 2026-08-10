#!/usr/bin/env python3
"""v6.2 regression tests: the four fixes, plus proof rules 0-7 still hold.

Run: python3 test_guard_context_budget.py
"""
import json, os, subprocess, sys, tempfile, shutil

GUARD = os.path.join(os.path.dirname(os.path.abspath(__file__)), "guard_context_budget.py")
passed = failed = 0

def run(payload, cwd):
    env = dict(os.environ, GUARD_AUDIT="0",
               TMPDIR=os.path.join(cwd, ".state"), TEMP=os.path.join(cwd, ".state"))
    os.makedirs(env["TMPDIR"], exist_ok=True)
    p = subprocess.run([sys.executable, GUARD], input=json.dumps(payload),
                       capture_output=True, text=True, cwd=cwd, env=env)
    return p.returncode, (p.stdout + p.stderr)

def check(name, got_code, want_deny, out="", not_reason=None):
    """want_deny: expected allow/deny. not_reason: substring that must NOT appear."""
    global passed, failed
    denied = got_code == 2
    okay = denied == want_deny
    if okay and not_reason and not_reason in out:
        okay = False
        out = f"wrong rule fired (contains {not_reason!r}): " + out
    if okay:
        passed += 1; print(f"  PASS  {name}")
    else:
        failed += 1
        print(f"  FAIL  {name}: wanted {'deny' if want_deny else 'allow'}, "
              f"got {'deny' if denied else 'allow'}\n        {out.strip()[:200]}")

def repo():
    d = tempfile.mkdtemp()
    os.makedirs(os.path.join(d, "docs/features"), exist_ok=True)
    os.makedirs(os.path.join(d, "docs/plans/product-list-page"), exist_ok=True)
    os.makedirs(os.path.join(d, "lib/features/product_list/presentation"), exist_ok=True)
    os.makedirs(os.path.join(d, "test/features/product_list/presentation"), exist_ok=True)
    w = lambda rel, text: open(os.path.join(d, rel), "w").write(text)
    w("docs/features/product-list-page.contract.md", "# contract\n" * 20)
    w("docs/features/product-list-page-brd.md", "# original BRD\n" * 600)          # big spec
    w("docs/plans/product-list-page/task-08-product-list-page.md", "# task 8\n" * 40)
    w("docs/plans/product-list-page/eng-slice.md", "# eng slice\n" * 30)
    w("lib/features/product_list/presentation/product_list_page.dart", "// code\n" * 400)
    w("test/features/product_list/presentation/product_list_page_test.dart", "// test\n" * 400)
    w("lib/small.dart", "// tiny\n" * 5)
    w("lib/product_list_page.dart", "// small but feature-named\n" * 5)
    w("CLAUDE.md", "# project\n" * 10)
    return d

R = lambda role, path, aid="a1", **kw: {
    "tool_name": "Read", "session_id": "s", "agent_id": aid,
    "agent_type": f"sdlc-pipeline:{role}", "tool_input": dict(file_path=path, **kw)}
B = lambda role, cmd, aid="a1": {
    "tool_name": "Bash", "session_id": "s", "agent_id": aid,
    "agent_type": f"sdlc-pipeline:{role}", "tool_input": {"command": cmd}}

print("\n--- FIX A: Rule 2 must not fire on source code named after the feature ---")
d = repo()
DISTILLED = "has been distilled"
# small source file named after the feature: must be fully allowed
for role in ("implementer", "unit-tester", "qa-tester"):
    c, o = run(R(role, "lib/product_list_page.dart", role), d)
    check(f"{role} reads small product_list_page.dart", c, False, o, not_reason=DISTILLED)
# large source file named after the feature: Rule 1 may ask for a window, but Rule 2
# must never claim it "has been distilled" and redirect to the contract.
for role in ("implementer", "unit-tester"):
    c, o = run(R(role, "lib/features/product_list/presentation/product_list_page.dart", role + "L"), d)
    check(f"{role} large source file: not mis-flagged as spec", c, True, o, not_reason=DISTILLED)
    c, o = run(R(role, "lib/features/product_list/presentation/product_list_page.dart",
                 role + "W", offset=1, limit=60), d)
    check(f"{role} windowed read of that source file allowed", c, False, o, not_reason=DISTILLED)
c, o = run(R("unit-tester", "test/features/product_list/presentation/product_list_page_test.dart",
             "u2", offset=1, limit=60), d)
check("unit-tester windowed read of *_test.dart allowed", c, False, o, not_reason=DISTILLED)

print("\n--- FIX B: derived plan artifacts are not 'the original spec' ---")
c, o = run(R("implementer", "docs/plans/product-list-page/task-08-product-list-page.md", "i9"), d)
check("implementer reads task-08-product-list-page.md", c, False, o)
c, o = run(R("eng-reviewer", "docs/plans/product-list-page/eng-slice.md", "e9"), d)
check("eng-reviewer reads its own eng-slice.md", c, False, o)

print("\n--- Rule 2 still works on the real spec document ---")
c, o = run(R("implementer", "docs/features/product-list-page-brd.md", "i10"), d)
check("implementer denied the distilled BRD", c, True, o)
c, o = run(R("planner", "docs/features/product-list-page-brd.md", "p10", offset=1, limit=200), d)
check("planner still allowed the BRD (windowed)", c, False, o)

print("\n--- FIX D: shell dump cannot bypass the read rules ---")
c, o = run(B("unit-tester", "cat -n lib/features/product_list/presentation/product_list_page.dart", "u3"), d)
check("cat -n of a 400-line source file denied", c, True, o)
c, o = run(B("implementer", 'grep -n "" lib/features/product_list/presentation/product_list_page.dart', "i3"), d)
check('grep -n "" full-file dump denied', c, True, o)
c, o = run(B("implementer", "cat docs/features/product-list-page-brd.md", "i4"), d)
check("cat of the distilled spec denied", c, True, o)
c, o = run(B("implementer", "cat -n lib/small.dart", "i5"), d)
check("cat of a small file still allowed", c, False, o)
c, o = run(B("implementer", "cat lib/features/product_list/presentation/product_list_page.dart | head -40", "i6"), d)
check("cat piped to head (bounded) allowed", c, False, o)
c, o = run(B("implementer", "grep -n 'class ProductListPage' lib/features/product_list/presentation/product_list_page.dart", "i7"), d)
check("normal targeted grep allowed", c, False, o)

print("\n--- FIX E: full test suite once per dispatch ---")
c, o = run(B("implementer", "flutter test", "t1"), d)
check("first full suite run allowed", c, False, o)
c, o = run(B("implementer", "flutter test", "t1"), d)
check("second full suite run in same dispatch denied", c, True, o)
c, o = run(B("implementer", "flutter test test/features/product_list/x_test.dart", "t1"), d)
check("targeted test run still allowed after the block", c, False, o)
c, o = run(B("implementer", "flutter test", "t2"), d)
check("full suite allowed again in a NEW dispatch", c, False, o)
c, o = run(B("unit-tester", "flutter test", "t3"), d)
c, o = run(B("unit-tester", "flutter test", "t3"), d)
check("unit-tester (gate role) exempt from the suite cap", c, False, o)

print("\n--- regression: rules 0/1/5/6 unchanged ---")
c, o = run(R("implementer", "CLAUDE.md", "r0"), d)
check("Rule 0 CLAUDE.md still denied", c, True, o)
c, o = run(R("implementer", "lib/features/product_list/presentation/product_list_page.dart", "r1"), d)
check("Rule 1 still blocks an unbounded 400-line read", c, True, o)
c, o = run(R("implementer", "lib/small.dart", "r5"), d)
check("Rule 5 first read of small file allowed", c, False, o)
c, o = run(R("implementer", "lib/small.dart", "r5"), d)
check("Rule 5 repeat read denied", c, True, o)
c, o = run(R("eng-reviewer", "lib/small.dart", "r6"), d)
check("Rule 6 eng-reviewer denied a non-slice file", c, True, o)

print("\n--- fail-open safety ---")
for name, payload in [("malformed payload", "NOT JSON"),
                      ("empty payload", {}),
                      ("no file_path", {"tool_name": "Read", "tool_input": {}}),
                      ("unknown tool", {"tool_name": "Frobnicate", "tool_input": {}})]:
    env = dict(os.environ, GUARD_AUDIT="0")
    body = payload if isinstance(payload, str) else json.dumps(payload)
    p = subprocess.run([sys.executable, GUARD], input=body, capture_output=True, text=True, cwd=d, env=env)
    check(f"fails open: {name}", p.returncode, False, p.stdout + p.stderr)

shutil.rmtree(d, ignore_errors=True)
print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
