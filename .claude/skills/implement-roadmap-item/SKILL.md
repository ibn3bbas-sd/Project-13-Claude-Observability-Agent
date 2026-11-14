---
name: implement-roadmap-item
description: Implement one item from docs/ai-agent-enhancement-roadmap.md end-to-end for the claude-agent — pick an item, design it, implement, verify against the real agent/.venv packages (no cluster/API key needed), update docs, commit. Use when asked to continue, resume, or pick up roadmap work in this repo (claude-observability-agent).
---

# Implementing a claude-agent roadmap item

This captures the workflow used to implement all P0/P1/P2 items in
`docs/ai-agent-enhancement-roadmap.md`. Reuse it for
any remaining or future roadmap item in this file.

## 1. Pick the item

Read `docs/ai-agent-enhancement-roadmap.md`. Each tier (P0/P1/P2) has a status
blockquote at the top once items are done, and each item has its own status
blockquote once implemented (`> **Status (date): done.** ...` with a commit hash).

Also check `docs/open-items.md` — incident-driven detection gaps (`OI-N`), each
with a "Still open" checklist. Those are equally valid work items; for a *new*
incident (user pastes kubectl output / logs the agent missed), use the sibling
skill `incident-to-detection` instead of this one.

If more than one undone item exists, or an item's scope/approach is genuinely
ambiguous (e.g. P1-4 needed a design decision on where correlated-timeline logic
should live), ask the user which one via `AskUserQuestion` before writing code —
don't guess. Flag items with an explicit stated precondition in the roadmap text
(e.g. P2-7 says "revisit only after single-agent investigation quality is
measured" — that's a real objection, not a formality; surface it rather than
building it anyway).

## 2. Design

- **Read the actual current code first** — line numbers and function shapes drift
  as prior items land. Don't trust remembered line numbers from earlier in a long
  session; re-grep.
- **Reuse existing patterns before inventing new ones.** This repo has clear
  conventions: ConfigMap-backed small stores (see `_load_cooldown_map`/
  `_save_cooldown_map` in `agent/monitor.py`, later generalized into
  `agent/outcome_store.py`); optional integrations gated on a config URL being set
  (`if self.cfg.tempo_url:` style, throughout `context_collector.py`); tiered
  action policies (`agent/monitor.py`'s `ACTION_POLICY`).
- **Extract a shared module once 2+ consumers need the same logic** — don't
  duplicate a second time "to be safe." `agent/observability_clients.py` was
  extracted specifically because both `claude_analyzer.py`'s tools and
  `context_collector.py`'s correlated-timeline pre-fetch needed identical
  Prometheus/Loki/Tempo HTTP logic.
- For anything nontrivial, it's fine to fork a Plan-mode-style pass (Explore →
  Plan agent) even outside formal plan mode — but for most items in this repo,
  direct implementation after reading the relevant files was sufficient.

## 3. Implement

- Follow the "no comments unless non-obvious" and "no premature abstraction"
  house rules already in effect for this repo.
- When adding a new optional feature, default it to a **complete no-op** when
  unconfigured (env var unset / mapping absent) — see `agent/prepass.py`,
  `agent/outcome_store.py`, `agent/gitops_remediation.py`. Never assume unverified
  cluster facts (e.g. whether app logs carry trace IDs) — degrade gracefully
  instead of asserting.
- If a new capability needs new Kubernetes RBAC (e.g. `kubectl_describe` needing
  node/pvc read, or a new ConfigMap needing get/patch), update `k8s/rbac.yaml` in
  the same commit — don't leave it for later, it's easy to forget.
- **Never run `kubectl` yourself in-session.** The user runs/handles kubectl
  commands themselves — always surface the command, don't execute it via Bash.
  This does not apply to the claude-agent bot's own remediation logic
  (`CommandExecutor`/`execute_with_policy`), only to your own actions.

## 4. Verify — no test suite exists in this repo

There's a real venv at `agent/.venv` with `kubernetes==36.0.2`, `anthropic==0.105.2`,
`pyyaml==6.0.3` actually installed — but its own `bin/python` is often missing or
points at a stale path. Use the **system** `python3` (matching version) with `PYTHONPATH` pointed at
the venv's site-packages instead:

```bash
cd agent
PYTHONPATH="$PWD/.venv/lib/python3.14/site-packages" python3 -c "
import sys; sys.path.insert(0, '.')
import monitor, claude_analyzer, context_collector  # real imports, no stubbing needed
"
```

Patterns that worked well across every item this session:

- **Mock `requests` at the module level** to test HTTP-calling code without a live
  cluster or API key: `import observability_clients as obs; obs.requests.get = fake_get`
  (or `obs.requests.request = fake_request` for the GitHub REST calls in
  `gitops_remediation.py`). Assert on both the returned value and the actual
  request params/bodies sent.
- **Bypass `__init__` for classes that need live K8s clients**: 
  `az = ClaudeAnalyzer.__new__(ClaudeAnalyzer); az.cfg = FakeCfg(); az.core_v1 = FakeCoreV1()`.
  Lets you unit-test prompt-building/dispatch logic without a kubeconfig.
- **Test the exact degradation paths**, not just the happy path — unconfigured
  URL, HTTP error, empty result, no matching data. Every optional feature in this
  codebase is expected to fail silently-but-loggably, never raise.
- **Regression-test refactors against the pre-refactor behavior** — when
  extracting shared logic (e.g. the observability HTTP clients), assert the new
  code produces byte-for-byte identical output/error strings to the old inline
  version before trusting the refactor.
- `python3 -m py_compile agent/*.py` first, always — catches syntax errors for
  free before spending time on logic tests.

## 5. Update docs in the same commit

- `docs/ai-agent-enhancement-roadmap.md`: add a `> **Status (date): done.** ...`
  blockquote directly under the item, naming the actual module/function added and
  any real limitation (unverified assumptions, scope deliberately excluded, not
  yet exercised against production). Update the tier's header status line once
  every item in that tier is done.
- `CLAUDE.md`: add or update a section under "Claude Agent" documenting the new
  capability, matching the existing table/prose style. **Check for staleness**
  introduced by earlier items in the same session — e.g. a "Prometheus queries"
  table referencing a pre-fetch method that a prior commit already deleted.
- `README.md`: add new env vars to its config table if you introduced any.
- `docs/open-items.md`: if the item closes an `OI-N` checklist entry, tick it
  (`- [x]`) with the commit hash.

## 6. Commit

One commit per roadmap item, not batched. Message format used throughout:
`type(scope): summary` subject, a body explaining what changed and why (not just
what), ending with:

```
Roadmap: docs/ai-agent-enhancement-roadmap.md <tier> item <N>.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
```

Only commit when the user has asked to (or already established that pattern in
the conversation) — this repo's session convention has been "implement one item →
show the diff → commit" without re-asking every time once that rhythm is set, but
confirm at the start of a new session.

## 7. If the user toggles Claude Code's plan mode mid-task

If it happens with uncommitted
edits already on disk: don't discard or restart. Instead, launch an Explore agent
to independently review the actual working-tree diff (it hasn't seen your
reasoning, so it's a real second opinion), then write the plan file as a
retrospective summary — Context / Approach-as-implemented / Files touched /
Verification-already-run — and call `ExitPlanMode` to get approval to commit.
Treat the plan file as documentation-for-approval, not a pre-implementation design
doc, when work is already done.

## 8. Deploying afterward

Not part of this skill's scope — see the "Build & Deploy" GitHub Actions workflow
(`.github/workflows/deploy.yml`, trigger via `gh workflow run deploy.yml --ref main`)
if asked to deploy. Deploying is a distinct, separate step from implementing —
don't deploy unless explicitly asked to.
