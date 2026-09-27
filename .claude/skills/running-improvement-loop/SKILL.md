---
name: running-improvement-loop
description: >
  This skill should be used when the user wants to run the self-improving quality loop on
  the current build — e.g. "run a quality sweep", "detect and fix extraction quality issues",
  "what's wrong with extraction and can you fix it", or "improve the pipeline and verify no
  regressions". It detects issues, plans fixes, applies reversible ones behind a regression
  gate (with rollback), and proposes code fixes as verified diffs. It does NOT run
  post-deployment monitoring/scheduling and is NOT the production extraction path.
version: 0.1.0
---

# Running the Improvement Loop

Orchestrate the on-demand control & correction loop: **detect → diagnose → plan → apply
(reversible, gated) / propose (code) → validate → record**. The backend `/improvement/*`
endpoints are the sensors/actuators; the subagents are the playbooks. Reversible data/config
fixes auto-apply only behind the regression gate (with rollback); code fixes are proposed as
verified diffs for human review (never auto-merged).

## Inputs
- A running API (or the in-process app) with the `/improvement` router; `admin` scope.
- Optional: a focus (issue kind, severity floor) to scope the run.

## Steps
1. **Sweep:** `POST /api/v1/improvement/sweep` → new `Issue`s (deduped by fingerprint).
   List them: `GET /api/v1/improvement/issues?status=open`.
2. **Diagnose** each issue: spawn the **issue-diagnostician** agent (depth-1, isolated) with
   the issue JSON → root cause + affected scope + fix_class.
3. **Plan:** spawn the **remediation-planner** agent with the diagnosis → a RemediationProposal
   (`config_tune` / `variation_promotion` = reversible; `code_fix_proposal` = propose-only).
4. **Apply / propose:**
   - Reversible (`config_tune`): `POST /api/v1/improvement/issues/{id}/apply` with the
     proposal `config` and `?run_tests=true` so the **regression gate** runs the full suite
     before committing. On a gate fail the change is not applied (status `failed`).
   - Code (`code_fix_proposal`): spawn the **code-fix-applier** agent — it edits in a git
     worktree, runs the gate, and returns the diff + gate report. **Do not merge**; surface
     the diff for human review.
5. **Validate the loop didn't regress:** confirm the apply's `gate_result.passed` is true and
   `candidate_health` shows no NEW qualifier mismatches. If a reversible apply later proves
   bad, `POST /api/v1/improvement/actions/{id}/rollback`.
6. **Report:** summarize issues found, fixes applied (with gate results), diffs proposed, and
   anything rolled back. Reference each `RemediationAction` id.

## Output
A run summary: per issue → diagnosis → action taken (applied+gated / proposed-diff / dismissed)
→ gate verdict. Plus the list of proposed code diffs awaiting review and any rollbacks. Cite
`Issue`/`RemediationAction` ids so the run is auditable.

## Edge Cases
- Sweep finds nothing new → report "clean" and stop.
- A reversible apply's gate fails → leave the issue open, record the `failed` action, surface
  the gate failures; do not retry blindly.
- A code fix improves the target but fails another test (a cross-regression) → keep it
  proposed-only with the failing checks shown; never merge.
- Never auto-merge code, never push, never run the durable `promote_variations` YAML write
  without explicit confirmation.

## Gotchas
- `run_tests=false` (the apply default) skips the pytest gate — only for quick/dev checks; the
  real loop must pass `run_tests=true` so cross-regressions are caught.
- Reversible ≠ safe-by-default: a `config_tune` still changes extraction behavior — it is
  gated + recorded with before/after precisely so it can be rolled back.
- Issues dedupe by `fingerprint`; re-sweeping won't recreate an already-open/dismissed issue.
