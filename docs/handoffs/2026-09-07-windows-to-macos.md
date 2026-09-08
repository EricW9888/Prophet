# Windows To macOS Engineering Handoff

Prepared 2026-09-07. This is a code and verification handoff, not a transfer of
accounts, credentials, transcripts, or investment state. It records a checkpoint;
the linked pull requests and issues carry subsequent engineering state.

## Pickup Summary

Windows is the primary running host. macOS is an independent development and
verification clone unless the operator explicitly changes that arrangement.
GitHub is the canonical source repository. Do not copy checkouts, synchronize
live databases, import an old database over either machine, or enable another
autonomous production instance merely to review these changes.

Read `README.md`, `CONTRIBUTING.md`, `docs/architecture.md`,
`docs/limitations.md`, and `docs/mailbox-ingestion.md`. Read any existing local
agent instructions and private continuity notes on the destination as well.
Older instructions refer to `docs/setup_handoff.md`, `docs/product_memory.md`,
and `docs/completion_ledger.md`; those files are not present in this checkpoint.
Local `AGENTS.md` and `.ai/` are intentionally ignored, not missing public code.
Do not recreate the old documentation hierarchy or assume its claims remain true.

## Exact Changes To Review

Baseline before the Windows release: `8364165`.

| Change | Evidence | Owning files and behavior |
| --- | --- | --- |
| Canonical multi-machine workflow | [PR #85](https://github.com/EricW9888/Prophet/pull/85), `977bdfe` | `CONTRIBUTING.md`, repository policy and ignores; one source repo, protected setup bundles |
| Windows and mailbox hardening | [PR #90](https://github.com/EricW9888/Prophet/pull/90), `06214af` | `scripts/prophet.py`, portability, mailbox/provenance/portfolio services, Settings; read-only scoped fetch, shared lock, interrupted receipt recovery, review-gated model proposals, Decimal replay |
| Durable and fair mailbox retry | [PR #93](https://github.com/EricW9888/Prophet/pull/93), `2a22286` | `services/mailbox.py`, `test_mailbox_scan.py`, `test_mailbox_recovery.py`; persist deferred receipts, prioritize unattempted/least-recent attempts before the budget, reuse the receipt on retry |
| Dependency update policy | [PR #87](https://github.com/EricW9888/Prophet/pull/87), `cd13482` | Routine pip/npm/Docker version updates stay within minor/patch; major upgrades require explicit compatibility work |
| Due-aware shadow selection | Commit introducing this handoff; related [#77](https://github.com/EricW9888/Prophet/issues/77) | `services/shadow.py`, `services/automation.py`, `test_shadow_scheduling.py`; waiting or locked newest runs cannot block older eligible work; pending evidence survives scheduler restart |

Backend paths in the table are relative to `backend/investos/` for services and
`backend/tests/` for tests. The shadow fix changes selection, not trade rules,
semantic family identity, terminal evaluation, or experiment history.

Review the actual changes rather than cherry-picking the August work again:

```bash
git log --oneline 8364165..origin/main
git diff --stat 8364165..origin/main
git show 06214af -- backend/investos/services/mailbox.py
git show 2a22286 -- backend/investos/services/mailbox.py
git log -p -- backend/tests/test_shadow_scheduling.py
```

## Safe Checkout Pickup

From the intended Mac checkout, inspect its actual root, branch, uncommitted
changes and local-only commits before switching anything:

```bash
git rev-parse --show-toplevel
git status --short --branch
git log -5 --oneline
git fetch --prune origin
git log --oneline origin/main..HEAD
```

If clean and the local `main` can fast-forward:

```bash
git switch main
git pull --ff-only
git switch -c codex/mac-verification
```

If dirty or divergent, preserve that work and inspect its history first. Do not
reset, force-push, automatically reapply a stash, or create nested frontend/backend
repositories. A separate clean clone is preferable to overwriting a legacy
checkout. Do not publish recovery branches or all refs. Git credentials belong
in the normal credential manager/device login, never embedded in a remote URL.

Stop only the Mac's known old launcher before changing its runtime code; do not
kill arbitrary port owners. Back up its own database before applying migrations
to it. Windows already has private pre-upgrade and pre-release backups; those are
not a substitute for a Mac backup and are not available through GitHub.

## Repeatable Native Mac Checks

Use Python 3.11-3.14, isolated Poetry 2.3.2, Node 24 LTS and PostgreSQL 16 as
documented in the README. Do not reuse a Windows virtual environment or install
Poetry inside the environment it manages. Poetry's
[in-project environment setting](https://python-poetry.org/docs/configuration/#virtualenvsin-project)
supports a separate `backend/.venv` on each machine.

Run tests on a separate empty database and private settings/storage paths, never
on either production ledger. The following Bash recipe uses a unique Compose
project and its own named volume. Port 55432 must be unused; choose another unused
loopback port if necessary. Run in a dedicated terminal with shell tracing off.

```bash
set -e
set +x
umask 077
mkdir -p .prophet-local
export VERIFY_DIR="$(mktemp -d "$PWD/.prophet-local/mac-check.XXXXXX")"
export CHECK_PROJECT="prophet-check-$(date -u +%Y%m%d%H%M%S)"
export POSTGRES_USER=investos
export POSTGRES_DB=prophet_test_mac
export POSTGRES_SERVER=127.0.0.1
export POSTGRES_PORT=55432
export POSTGRES_PASSWORD="$(python3 -c 'import secrets; print(secrets.token_hex(24))')"
export RUNTIME_SETTINGS_PATH="$VERIFY_DIR/runtime_settings.json"
export STORAGE_DIR="$VERIFY_DIR/storage"
export MEDIA_TEMP_DIR="$VERIFY_DIR/media"
export BACKUP_DIR="$VERIFY_DIR/backups"
export LLM_ALLOW_LOCAL_PROVIDER=false
export AUTOMATION_ENABLED=true
export NVIDIA_API_KEY=
export TAVILY_API_KEY=
docker compose -p "$CHECK_PROJECT" up -d --wait db

cd backend
poetry config virtualenvs.in-project true --local
poetry install --with dev --no-interaction
.venv/bin/python -m alembic upgrade head
.venv/bin/python -m alembic check
.venv/bin/python -m coverage run --branch --source=investos -m pytest -q
.venv/bin/python -m coverage report --precision=1 --fail-under=40
.venv/bin/python -m black --check investos tests ../scripts
.venv/bin/python -m isort --check-only investos tests ../scripts
.venv/bin/python -m flake8 investos tests ../scripts --select=E9,F63,F7,F82
.venv/bin/python -m bandit -q -r investos -ll
.venv/bin/python ../scripts/audit_python_lock.py
.venv/bin/python ../scripts/repository_policy_check.py
cd ../frontend
npm ci
npm run lint
npm run test:unit
npm run build
npm audit --audit-level=low
cd ..
git diff --check
```

The test database password above is generated locally and never committed.
The automation flag matches the test suite's scheduler-registration assertions;
the separate smoke-test command below disables actual background automation.
Keep command output private until reviewed. Do not print environment values or
entire runtime settings. A failed command is a failed check, not a reason to run
the tests on a different production database.

For a credential-free UI smoke test in the same terminal/environment:

```bash
export AUTOMATION_ENABLED=false
backend/.venv/bin/python scripts/prophet.py --no-open
```

The launcher owns the backend and frontend, applies migrations, and performs a
production build when needed. The old Mac shell launchers delegate to it. Keep it
running while inspecting the UI, then stop it with Ctrl+C. Stop only this test
database with `docker compose -p "$CHECK_PROJECT" down` from the same terminal
and repository root. Do not add `--volumes`; retain the test volume and private
verification directory until failures are understood. Close the terminal so its
test database/settings environment is not accidentally reused for normal startup.

This Bash recipe requires native Mac execution; it was not executed on Windows.
It intentionally does not configure or test live connectors. Test helpers can
have local side effects, so a clean verification clone is preferred.

## What Passed And What That Proves

At the `2a22286` Windows release, 667 backend tests passed in 29.74 seconds.
With the due-aware shadow selection change, 686 passed in 29.86 seconds;
the focused shadow/automation suite passed 70 tests in 1.59 seconds. Tests used an isolated
Windows-local database. Required PR checks run Linux Python 3.11 and 3.14,
frontend lint/unit/build/audit, and Gitleaks. Inspect the handoff PR's checks for
its final result rather than treating this historical count as current CI state.

Windows release checks also passed repository policy, formatting, static-error
lint and Bandit. The previous merged frontend had 17 unit tests. Production
startup, backend health, frontend root, direct dashboard API and Next dashboard
proxy were checked separately. Settings was exercised on desktop and a 390px
viewport without horizontal overflow or browser errors.

Those are Windows and Linux results, not native macOS results, proof of perfect
security, or a completed external broker reconciliation. Hosted configuration
readiness is also not proof of successful generation, available quota, or
successful structured extraction.

## Independent Runtime Acceptance

Record commit, platform, exact commands, exit codes, test counts and limitations.
Use synthetic data for public evidence; redact any private diagnostics.

| Check | Expected result or acceptance boundary |
| --- | --- |
| Services | `scripts/prophet.py status` ready; health/root/direct `/api/dashboard/summary`/proxied `/api_proxy/dashboard/summary` each HTTP 200 |
| Binding | Backend 8000, frontend 3000 and database port listen only on loopback; no local model processes started |
| Settings | Clear missing/degraded/partial states, working setup links; no key values returned to browser responses |
| Hosted readiness | Separate optional generation and structured-output probe from configuration-presence checks; preserve failure details privately |
| Research | Real query, attributable source content and dates; snippets/discovery alone cannot corroborate or upgrade a claim |
| Gmail | Existing authorized scope only, idempotent retries, deferred != imported, shared-lock busy != success; do not activate a second host implicitly |
| Ledger | Internal chronological replay matches quantities/currency precision; compare against a broker export before declaring external balances correct |
| Knowledge | Filters/visible counts distinguished from stored counts, stable selection, no disappearing records or unexplained graph pruning |
| Chat | Follow-ups retain context; failures stay explicit, sources precede conclusions, no invented trades/portfolio or unrelated search query |
| Shadow | Newest-waiting/older-due and locked/due fixtures advance eligible work; no invented live account or real order |
| Media | Transcript acquisition, enrichment, source provenance and temporary-file cleanup checked as separate stages; no claim of full video understanding |
| Responsive UX | Inspect Portfolio, Settings, Research, Knowledge, Sources and Simulate at desktop and compact widths for overflow, inaccessible controls and console errors |

Do not expect identical graph or portfolio counts on independent machines. Empty
or different state is not evidence of data loss unless compared within the same
database, scope, filter and time. Do not populate or erase either live database
just to make screenshots match. No local inference or transcription model should
be installed or launched as part of this handoff.

## Remaining Work, Ordered By Risk

| Owner | Current boundary and next acceptance |
| --- | --- |
| [#92](https://github.com/EricW9888/Prophet/issues/92) | IMAP identities still lack account/UIDVALIDITY/cross-label isolation; require an auditable legacy migration before switching mailbox identity |
| External reconciliation | Gmail is incremental receipt evidence, not an authoritative account snapshot; unknown receipts and broker balances remain explicit until independently reconciled |
| [#97](https://github.com/EricW9888/Prophet/issues/97) | Completed shadow results currently label alpha as Sharpe and terminal loss as drawdown; replace these proxies with dated-path measurements or explicit unavailable values before relying on risk-adjusted simulation metrics |
| [#75](https://github.com/EricW9888/Prophet/issues/75) | New extraction failures can be deferred, but legacy terminal fallback recovery and complete media stage/retry visibility are not finished |
| [#77](https://github.com/EricW9888/Prophet/issues/77) | Due selection fixed here; terminal overdue evaluation, semantic family identity, duplicate reconciliation and cross-surface counts remain open |
| [#78](https://github.com/EricW9888/Prophet/issues/78) | Questions can remain investigating; finish explicit next actions, bounded retries, semantic uncertainty identity and retirement without deleting history |
| [#79](https://github.com/EricW9888/Prophet/issues/79) | Evidence worker still logs generic processed success; domain outcomes, provider degradation, cache/critique and eligibility counts need reconciliation |
| [#41](https://github.com/EricW9888/Prophet/issues/41) | Complete journey-level UI tests and progressive disclosure across investigation, evidence, watches, simulation and recovery |

Issue bodies may contain observations from an older host or earlier code. Recheck
each acceptance condition against current implementation; do not repeat their
old counts as current state. Do not close a multi-part issue after fixing one
selector or status label. Dependency PRs are separate compatibility work, not
permission to bulk-merge upgrades without fresh review and CI.
The merged dependency policy uses `ignore.update-types`, which GitHub documents
as applying to version updates, not security updates. Vulnerability audits and
security update review remain necessary. See the
[official option reference](https://docs.github.com/en/code-security/reference/supply-chain-security/dependabot-options-reference#ignore).

## Design Assessment

The appropriate workflow is one canonical code repository, lockfile-pinned
per-machine environments, reviewed PRs, isolated verification, one primary live
host and private state kept separate. Gmail remains a free supporting connector;
broker exports provide the independent reconciliation boundary. Neither an LLM
nor another search vendor can repair missing ledger evidence by guessing.

The agent should choose investigation angles dynamically. Deterministic limits
on money, source lineage, time, concurrency, retries and promotion protect
correctness; they are not a ticker-specific script or a fixed investment thesis.
The outstanding lifecycle and telemetry issues mean this system is not yet
demonstrably optimal or generally complete. Measure recovery, evidence quality
and user journeys, not just passing tests or the number of graph nodes.

## Suggested Mac Session Prompt

> Read this handoff and the current repository contracts before editing. Preserve
> local work and private state. Verify the referenced changes and rerun isolated
> native macOS checks; separate historical Windows/Linux results from new results.
> Windows remains the primary running host. Do not copy live data, enable another
> autonomous connector instance, or start local inference. Review current issues
> #92, #97, #75, #77, #78, #79 and #41 against actual code, then continue the highest-risk
> reproducible defect through regression tests and a focused PR. Record evidence
> and remaining limits without publishing personal data or claiming everything is
> complete. The original conversation and private Windows diagnostics are not
> included in this public handoff.
