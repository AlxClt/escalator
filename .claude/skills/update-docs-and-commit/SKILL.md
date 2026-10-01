---
name: update-docs-and-commit
description: Commit workflow for this repo. Updates the project docs when the pending change is significant, then commits code and docs together. Use for every commit instead of running git commit directly.
when_to_use: Whenever changes are about to be committed, whether the user asks for a commit or a task you finished should end in one.
argument-hint: "[intent or message hint] | release <X.Y.Z>"
allowed-tools: Read Edit Write Glob Grep Bash(git status *) Bash(git diff *) Bash(git log *) Bash(git add *) Bash(git commit *) Bash(date *)
---

# Update docs and commit

Commit the pending changes. Before committing, update the managed docs only if the change is significant (step 3).

## Context

- Date: !`date +%F`
- Status: !`git status --long`
- Staged: !`git diff --cached --stat`
- Unstaged: !`git diff --stat`
- Arguments: $ARGUMENTS

## Managed docs

| Doc | Content | Nature |
|---|---|---|
| `project_spec.md` | Requirements, tech stack | Normative: what the system must be |
| `docs/architecture.md` | System design | Descriptive: how the system is built |
| `docs/changelog.md` | Version history | Descriptive: what changed |
| `docs/project_status.md` | Progress, next steps | Descriptive: where the project stands |

Each fact lives in exactly one doc.

## 1. Preconditions

Stop and report if any of these holds:
- A merge, rebase, cherry-pick or revert is in progress.
- There is nothing to commit.
- The changes contain conflict markers.

## 2. Scope and diff

- If anything is staged, the scope is exactly the staged changes. Stage nothing else except managed docs.
- If nothing is staged, run `git add -u`, then stage untracked files that belong to the change. Never stage secrets or artifacts: `.env*`, keys, credentials, build output, large binaries.
- Read the diff to be committed, excluding the managed docs:
  `git diff --cached -- . ':!project_spec.md' ':!docs/architecture.md' ':!docs/changelog.md' ':!docs/project_status.md'`
  If the diff is large, go file by file from `--stat`. Note lockfile and generated-file changes without reading them.
- Run `git log --oneline -15` for recent context and the repo's message conventions. If it fails because there are no commits yet, treat this as the initial commit.
- If the scope contains only managed docs, go to step 5 with commit type `docs`.

## 3. Decide what to document

List the logical changes, grouped by intent rather than by file. For each one, record:
- a behavior-level summary;
- its type: feat, fix, perf, refactor, test, build, ci, style or chore;
- its impact: breaking, user-facing or internal;
- the paths that support it.

Record only what the diff shows. The arguments and the conversation give intent, but the diff is the source of truth.

Document a change only if, without the update, a reader of the docs would be misinformed or would miss something that matters.

| Change | changelog | architecture | spec | status |
|---|---|---|---|---|
| New feature or capability | Added | If it adds a component or data flow | Flag if outside spec scope | Progress |
| Behavior change | Changed | If a flow changes | Documented contracts | Progress |
| Planned step or milestone completed | If user-visible | — | — | Mark done, update next steps |
| Breaking change (API, CLI, config, schema, public types) | Changed or Removed, prefixed **BREAKING:** | If a boundary moved | Documented contracts | — |
| Deprecation | Deprecated | — | — | — |
| Security fix | Security | — | — | — |
| Fix to released or documented-as-working behavior | Fixed | — | — | Close item if tracked |
| Component added, removed, split or merged | If user-visible | Yes | — | — |
| Dependency added or removed; major or minor framework or runtime bump | If it affects users | If it is a new integration (DB, queue, external API) | Tech stack | — |
| Schema change or migration | Changed | Data model | Data model, if documented | — |
| New env var or config key | Added, if user-facing | — | Configuration | — |
| New TODO or FIXME, stub, skipped test | — | — | — | Next steps |

Never document:
- Fixes for syntax, type, lint, build or test failures.
- Typos, formatting and comments.
- Renames and refactors that change neither behavior nor structure.
- Test-only changes.
- Patch-level dependency bumps and lockfile churn.
- Fixes to work added in the current `[Unreleased]` cycle. If such a fix makes that work's existing entry inaccurate, correct the entry; don't add a new one.

If nothing qualifies, go to step 5 without touching any doc.

## 4. Update the docs

Read each doc in full before editing it, and edit only the docs a change routes to.
- Edit surgically: preserve structure, headings, tone and formatting, and leave unrelated sections alone.
- Write nothing speculative. Copy paths, identifiers and versions exactly from the diff.
- If a managed doc in scope already has user edits, treat those edits as authoritative and merge into them.

Per doc:
- **Changelog**:
  - Use Keep a Changelog: entries go under `## [Unreleased]` in the sections Added, Changed, Deprecated, Removed, Fixed and Security. If the file already follows another established format, follow that instead.
  - Write one line per change, for users of the project.
  - If an existing entry already covers the change, update it; never duplicate.
  - Only when the arguments are `release X.Y.Z`: rename `[Unreleased]` to `[X.Y.Z] - <date>`, add an empty `[Unreleased]` above it, and update the comparison links if the file has them.
- **Architecture**: edit the affected component's section, keep diagrams consistent with the prose, and remove descriptions of anything the diff deletes.
- **Spec**:
  - Sync tech-stack facts to the code: dependencies, versions, config and documented contracts.
  - Never rewrite a functional requirement to match the code. If the diff contradicts or exceeds a requirement, leave the requirement as is and report the discrepancy.
- **Status**: mark completed items, add next steps the diff makes concrete, remove obsolete ones, and update the "last updated" marker if the file has one.
- **Missing docs**: create the changelog or status doc with a minimal skeleton. Don't create a missing spec or architecture doc; report it instead.

## 5. Commit

- Stage the docs you edited, then run `git diff --cached --stat` and confirm the staged set is the scope plus those docs.
- Write the message in Conventional Commits format, unless `git log` shows the repo uses another convention:
  - Subject: `type(scope): summary`, imperative, at most 72 characters, no trailing period. The type is the dominant type from step 3; add `!` for breaking changes.
  - Body: short bullets, favoring why over what. If any docs were updated, end with a line `Docs: <updated docs>`.
  - Breaking changes: add a `BREAKING-CHANGE: <description>` footer.
- Commit with `git commit -m "<subject>" -m "<body>" --trailer "Docs-Checked: true"`.
- Never use `--no-verify` or `--amend`, and never push.
- If a hook fails: if it only reformatted files, re-stage them and retry once. Otherwise stop and report the hook's output.

## 6. Report

In one or two lines:
- `<short hash> <subject>`
- which docs were updated, or "docs: no update needed"
- any discrepancies
- anything left out of the commit
