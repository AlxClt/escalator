# Claude.md

## Projects Goals

Build a natural language to SQL agent, with a small to frontier model escalation router, and record the cost vs accuracy of several escalation signals.

**Current milestone**: Step 1: Infrastructure (see [project-specs.md](project-specs.md))

## Architecture

[docs/architecture.md](docs/architecture.md)

## Constraints and policies

**Security - MUST follow:**

- ALWAYS use environment variables for secrets
- ALWAYS add any file starting with `.env` to the .gitignoe BEFORE commiting, EXCEPT `.env.example`
- NEVER trigger a run calling online anthropic's models, this will always be manually triggered

**Code quality:**

- Use typing
- No `Any` types without justification

**Strict rules:**

- ALWAYS version prompts files, one file per version.
- NEVER edit scorer fixtures to make a test pass

## Repository etiquette

**Branching:**

- ALWAYS create a feature branch before starting major changes
- Branch naming: `feature/description` or `fix/description`, keep description short

**Commit rule**
Commit only through the `update-docs-and-commit` skill; never run `git commit` directly.

**Git workflow for major changes:**

1. Create a new branch
2. Develop and commit on the feature branch
3. Run the unit test locally before pushing
4. Push the branch
5. Create a PR to merge into main
6. Use the `/update-docs-and-commit`slash command for commits - this ensure docs are updated alogside code changes

## Documentation

- [Project Spec](project_spec.md) - Full requirements, tech stack specs and details
- [Architecture](docs/architecture.md) - System design
- [Changelog](docs/changelog.md) - Version history
- [Project Status](docs/project_status.md) - Current progress
- Update files in the docs foler after major milestones and major additions to the project
- Use the /update-docs-and-commit slash command when making git commits.
