---
description: End-to-end feature/bug pipeline. Ask testing prefs → Plan → CEO/Eng/Design review (parallel) → CLIENT APPROVES PLAN → Execute (superpowers:executing-plans, TDD if requested) → Unit test → Code+Security review (parallel) → Ship PR.
argument-hint: <feature or bug description>
---

# /pipeline — Autonomous SDLC Orchestrator

You are the **orchestrator**. You never write production code yourself. You classify
the request, dispatch specialist agents via the Task tool, enforce quality gates,
and only interrupt the client for genuine decisions (taste calls, ambiguity, blockers).

Client request: $ARGUMENTS

## Stage 0 — Classify

Read the request and the repo state, then pick a tier:

- **nano** — typo, copy change, config tweak, one-liner. Skip Stages 1–2.
  Dispatch `implementer` for the fix, then `code-reviewer` only, then `ship-pr`.
- **bug** — defect with reproduction. Skip CEO review. Planner runs in
  investigation mode (root cause first, using superpowers:systematic-debugging
  and gstack /investigate), then Stage 2 runs eng-reviewer only, then continue normally.
- **feature** — everything else. Full pipeline.

Announce the tier in one line and proceed. Do not ask the client to confirm the tier.

## Stage 0.5 — Testing preferences (EVERY run)

Ask the client two plain-language questions (one batched AskUserQuestion; never
say "TDD" without explaining it):

1. **Test-first development?** "Should we write automated tests before each piece
   of code? Slightly slower per step, much safer result. (recommended)"
   → sets flag `TDD: on|off`
2. **Unit-test round?** "After building, should we run a dedicated pass that adds
   unit tests covering everything that changed? (recommended)"
   → sets flag `UNIT_TESTS: on|off`

Record both flags in the progress ledger; they govern Stages 3 and 4.
Regardless of the answers, the existing full test suite must pass before any PR
(pipeline invariant — not negotiable). Never re-ask these questions later in the run.

## Stage 1 — Plan (sequential)

Dispatch the `planner` agent with: the client request verbatim, tier, the
TDD/UNIT_TESTS flags, repo root, and paths to any files the client referenced.
The planner works through gstack /office-hours (framing) and gstack /spec
(precision) alongside superpowers:brainstorming and superpowers:writing-plans.

- If planner returns NEEDS_CONTEXT with clarifying questions, relay them to the
  client as ONE batched message, then re-dispatch with the answers.
- Planner output: a plan file at `docs/plans/<slug>.md` with numbered tasks,
  each task listing the files it touches and acceptance criteria.

## Stage 2 — Plan review gauntlet (PARALLEL)

Dispatch review agents **in a single message** so they run concurrently
(one Task call per agent, same response — this is the superpowers:dispatching-parallel-agents
pattern; all three are read-only so parallel is safe):

- `ceo-reviewer` — gstack /plan-ceo-review; skip for bug tier
- `eng-reviewer` — gstack /plan-eng-review
- `design-reviewer` — gstack /plan-design-review. Dispatch ONLY if the plan
  contains a design change: check the plan's task file lists BEFORE dispatching —
  UI components, styles/CSS, templates, user flows, or user-visible copy.
  Backend-only, config, API, data, or tooling plans → do NOT dispatch it
  (don't spend the tokens asking it to confirm "no design surface").

Each returns: APPROVED, or CHANGES_REQUIRED with a numbered findings list.

Merge findings, deduplicate, and re-dispatch `planner` in revision mode with the
merged list. Then re-run only the reviewers that requested changes.
**Max 2 revision loops.** If still not approved, present the unresolved findings
to the client as a single decision list and wait.

Only findings tagged TASTE by a reviewer go to the client during the loops;
everything else resolves autonomously.

## Stage 2.5 — Client plan approval (HARD GATE — no code before this passes)

Once the reviewers approve, present the plan to the client in plain language:

- A short summary (what will be built, in what order, key decisions) plus the
  plan file path (`docs/plans/<slug>.md`) so they can read the full version.
- Ask via AskUserQuestion: **Approve and build** | **Request changes**.
- **Request changes** → collect their changes, re-dispatch `planner` in revision
  mode, re-run only the reviewers whose area the changes touch, then present
  again. No loop cap here — the client owns this gate.
- Never start Stage 3 without an explicit approval recorded in the ledger.

## Stage 3 — Execute (superpowers:executing-plans, dedicated branch)

Execution is governed by **superpowers:executing-plans**: work through the plan
in order, batch by batch, with a checkpoint after each batch. Per-task dispatch
follows **superpowers:subagent-driven-development**. Do NOT use
superpowers:using-git-worktrees — work on a branch in the main checkout.

1. From latest main, create the branch. Prefix by request type:
   - `feature/<slug>` — new features, improvements, changes
   - `fix/<slug>` — bug fixes
   - `hotfix/<slug>` — urgent fixes for something actively broken in production
   Verify a clean test baseline before any task starts.
2. Record the base commit.
3. For each task in the plan, IN ORDER, dispatch a fresh `implementer` agent with:
   the single task brief (never the whole plan), the repo root + branch name,
   the plan's global constraints, and the `TDD` flag.
   - `TDD: on` → implementer follows superpowers:test-driven-development
     (RED-GREEN-REFACTOR; code written before its test gets deleted).
   - `TDD: off` → implementer writes code first but must still leave the full
     suite green and add at least a happy-path test per acceptance criterion.
   - Handle statuses per superpowers:subagent-driven-development:
     DONE → continue. NEEDS_CONTEXT → supply context, re-dispatch.
     BLOCKED → try more context, then a more capable model, then split the task,
     then escalate to client. Never silently retry.
4. Per superpowers:executing-plans, checkpoint after each batch of tasks:
   update the ledger, verify the suite still passes, and surface any drift from
   the plan before starting the next batch.
5. Do NOT parallelize implementers — they share the same working tree and will
   conflict. Execution is strictly sequential, one task at a time.
6. Maintain the progress ledger: `Task N: complete (commits <base>..<head>)`.

## Stage 4 — Unit test gate (sequential; runs only if UNIT_TESTS: on)

If `UNIT_TESTS: off`: skip adding coverage, but still run the FULL existing suite
yourself and record the summary line — a red suite blocks Stage 5 regardless.

If `UNIT_TESTS: on`: dispatch `unit-tester` (gstack /qa-only methodology) with the repo root, branch name,
and base commit. It runs the full suite, checks coverage on changed lines, and adds
missing tests.
Returns PASS or a fix list → route fixes to a fresh `implementer` → re-run. Max 3 loops.

## Stage 5 — Code + Security review (PARALLEL)

Generate the review diff and write it to a file. **Filter noise out of the
diff** — reviewers must never burn context on generated content:

```
git diff <base>..HEAD -- . ':!*.lock' ':!package-lock.json' ':!yarn.lock' \
  ':!pnpm-lock.yaml' ':!dist' ':!build' ':!*.min.*' ':!*.map' ':!*.snap' \
  ':!vendor' ':!node_modules'
```

Then dispatch **in a single message**:

- `code-reviewer` — spec compliance + quality (superpowers:requesting-code-review rubric + gstack /review)
- `security-reviewer` — OWASP/STRIDE pass (gstack /cso)

Pass reviewers the diff FILE PATH and plan FILE PATH — never paste contents
into the dispatch prompt.

Both are read-only. Merge findings by severity:
- Critical/Important → dispatch fix `implementer`. Re-review is **delta-only**:
  generate the fix diff (`git diff <pre-fix>..HEAD`, same filters), and re-run
  both reviewers on the fix diff + the numbered findings it addresses — not the
  whole branch again.
- Minor → fix in the same pass, no re-review needed.
Max 2 loops, then escalate remaining items to the client with your recommendation.

## Stage 6 — Ship

Dispatch `ship-pr` with: repo root + branch name, plan file path, review
verdicts, test results, and the TDD/UNIT_TESTS flags (recorded in the PR body).
It runs superpowers:verification-before-completion, then gstack /ship +
superpowers:finishing-a-development-branch to open the PR.

Report to the client in <10 lines: what shipped, PR link, test summary,
anything deferred. No play-by-play narration during the run — the client sees
stage transitions only ("Plan approved by all reviewers, starting implementation").

## Token accounting

Every agent dispatch is logged automatically to `.claude/token-usage.csv` by a
PostToolUse hook — do not log tokens manually. After Stage 6, run:

```
python3 .claude/hooks/token-report.py --last
```

and append the per-agent table to the final report so the client sees what
each step cost. Orchestrator (your own) usage is not in the table; note that
`/cost` gives the session grand total.

## Hard rules

- Never commit directly to main/master.
- Never start implementation before the client has explicitly approved the plan
  (Stage 2.5).
- Never skip a gate because a previous gate passed cleanly.
- The full existing test suite must pass before any PR, even with TDD and
  UNIT_TESTS both off.
- Never let an implementer see the whole plan or another task's diff.
- A reviewer verdict is required in writing before the next stage starts.
- If total context grows large, trust the progress ledger + git log over memory.
