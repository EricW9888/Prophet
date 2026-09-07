# Contributing To Prophet

Prophet is a source-available, All Rights Reserved personal project. Its original
source code is owned solely by Eric Wang. Public visibility does not make it an
open-source or community-maintained project.

## Contribution Boundary

Issues, bug reports, and suggestions are welcome. Do not submit source code or a
pull request unless Eric Wang has agreed in writing to the contribution and
ownership terms before submission. Unsolicited code may be closed without review
so Prophet's ownership and source-availability boundary remain unambiguous.

Never include credentials, private portfolio details, mailbox content, broker
statements, or unsanitized logs in an issue or proposed change. Use the issue
forms so reports include enough context to reproduce without exposing local data.

An approved change should preserve Prophet's time-aware evidence model,
accepted-state promotion policy, and separation between private runtime state
and publishable code.

## Maintainer Workflow

The public repository is the canonical development repository. Ordinary work
starts from a concrete GitHub Issue or an explicitly documented maintenance
task, continues on a focused branch, and is merged through a pull request after
review and required CI checks pass. The repository does not use a second active
source tree or a public/private synchronization workflow.

### Working Across Machines

Each development machine is an ordinary clone of this repository. Before new
work, update `main` with a fast-forward-only pull and create a focused branch:

```bash
git switch main
git pull --ff-only
git switch -c issue-123-short-description
```

Push the branch before changing machines. On the other machine, fetch and
continue that same remote branch rather than copying a checkout or starting a
parallel implementation:

```bash
git fetch origin
git switch --track origin/issue-123-short-description
```

Refresh remote state again before opening or merging the pull request. Do not
force-push shared work or develop directly on `main`. After a squash merge,
return each clone to `main`, fast-forward it, and remove the merged local branch.

Git carries reviewed source, migrations, tests, and public documentation. It
does not carry `.env`, databases, portfolio or mailbox data, backups, logs,
generated setup bundles, or `.prophet-local`. Keep those values per-machine;
move reusable credentials through a password manager or another encrypted
private channel, never through a branch, issue, pull request, or private code
mirror. A private notes repository must not contain Prophet source or become an
engineering backlog; durable engineering work belongs here as code, docs, or
GitHub Issues.

## Start Here

1. Read `README.md`, `docs/architecture.md`, and `docs/limitations.md`.
2. Search the existing issues before opening a new report or proposal.
3. Follow the setup in `README.md` using the committed Poetry and npm lockfiles.
4. Keep edits scoped to the behavior being changed.
5. Add focused tests for behavioral changes and broader tests for shared policy
   or cross-module contracts.

## Engineering Rules

- Route model calls through `backend/investos/core/llm.py`.
- Never let raw model output promote accepted state without policy checks.
- Preserve evidence lineage and event/public/ingest timestamps.
- Use structured parsers and repo-native services before adding new machinery.
- Keep credentials, portfolio data, mailbox content, backups, logs, and ignored
  local agent or tool state out of commits and fixtures.
- Use synthetic examples in public tests and documentation.
- Do not add an internet-facing deployment path without a reviewed threat model
  and authentication boundary.

## Before Proposing An Approved Change

Run the full verification block in `README.md`. For UI changes, also exercise
the affected workflow in a real browser at desktop and mobile widths and check
for horizontal overflow, inaccessible controls, and console errors.

Describe the behavior changed, the verification performed, and any remaining
external dependency or credential requirement. Do not describe incomplete work
as complete.
