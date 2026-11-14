# Prophet Backend

FastAPI and PostgreSQL backend for Prophet. The Python package retains the
historical name `investos`.

## Runtime

From `backend/`, after installing dependencies and applying migrations:

```bash
poetry run alembic upgrade head
poetry run uvicorn investos.main:app --host 127.0.0.1 --port 8000 --no-proxy-headers
```

The health endpoint is `http://127.0.0.1:8000/health`. The Next.js frontend
proxies `/api_proxy/*` to the backend's `/api/*` routes.

The FastAPI lifespan starts `AutomationCoordinator` and `LiveJobTracker`.
Application routers are mounted in `investos/api/routes/__init__.py`; add a new
domain router there rather than creating a second routing tree.

## Structure

```text
investos/api/routes/  HTTP request boundaries
investos/models/      SQLAlchemy models
investos/schemas/     Pydantic request and response contracts
investos/services/    domain behavior, policy, and orchestration
investos/workers/     research and evidence extraction
alembic/              schema migrations
tests/                synthetic regression tests
```

The main flow is:

1. Portfolio, mailbox, file, URL, source, and research routes accept input.
2. Domain services preserve portfolio provenance or raw evidence in PostgreSQL.
3. Research workers extract dated facts, claims, events, profiles, and links.
4. The agent resolves a subject and asks `RetrievalService` for a bounded packet.
5. All model calls pass through `investos/core/llm.py`.
6. Corroboration, inference, canonical-state, and verification services guard
   accepted-state changes.
7. Watches, activity, graph, risk, and shadow services expose follow-up state.

See `../docs/architecture.md` for the complete current data flow and invariants.

## Settings

`investos/config.py` owns static environment settings. User-editable connector
and provider settings are layered by `RuntimeSettingsStore` from the configured
runtime-settings path. In the default local setup, settings and their secret
sidecar are under the ignored repository-root `data/` directory.

The LLM provider registry supports NVIDIA NIM, Codex CLI, and Ollama. NVIDIA NIM
uses an API key and configurable hosted model/base URL, and supports streaming.
Codex CLI uses an installed, authenticated CLI; Prophet does not manage its API
key, model, or base URL, and this adapter is non-streaming. Ollama supports a
configurable local model/base URL and streaming, but remains unavailable unless
the operator explicitly enables local-provider use. Prophet never starts it.

External source discovery uses an ordered provider registry: an explicitly
configured SearXNG endpoint can run first, with Tavily as the optional metered
fallback. Search candidates are not the retrieval store or an LLM result;
research ingestion attempts to fetch the underlying source page before creating
evidence. Prophet does not install or start SearXNG. Connector secrets can be
supplied through Settings or environment variables such as `NVIDIA_API_KEY` and
`TAVILY_API_KEY`. Responses expose key-presence and readiness flags, never secret
values.

## Tests and Checks

Create an empty, disposable PostgreSQL database whose name starts with
`prophet_test_` (CI uses `prophet_ci`). Set `POSTGRES_DB` in the test process
environment to that database. Keep the usual connection settings for your
PostgreSQL server. The suite refuses to run against the default application
database and uses temporary runtime settings and storage.

From `backend/` (PowerShell uses `$env:POSTGRES_DB = 'prophet_test_local'`
instead of `export`):

```bash
export POSTGRES_DB=prophet_test_local
poetry run alembic upgrade head
poetry run coverage run --branch --source=investos -m pytest -q
poetry run coverage report --precision=1 --fail-under=40
poetry run alembic check
poetry run black --check investos tests ../scripts
poetry run isort --check-only investos tests ../scripts
poetry run flake8 investos tests ../scripts --select=E9,F63,F7,F82
poetry run bandit -q -r investos -ll
poetry run python ../scripts/repository_policy_check.py
poetry run python ../scripts/audit_python_lock.py
```

Use `poetry run python -m ...` or the virtual environment's Python module form
when a checkout move leaves an old console-script shebang behind.

An opt-in synthetic model screen exercises the real adapters without saving
investment conclusions or changing provider settings. From the repository root:

```bash
backend/.venv/Scripts/python.exe scripts/benchmark_structured_generation.py --provider ollama --model qwen3.5:9b --allow-local --output .prophet-local/runtime/model-screen.json
```

Start an already installed local server separately. The screen never downloads
a model. It records the corpus hash, contract/semantic results and end-to-end
latencies, stops on a provider failure and refuses to overwrite a prior report.
Repeat `--case` to select frozen checks, including relationship co-occurrence,
role/cause boundaries and subject kinds. The report hashes the selected corpus.
Small synthetic cases are an initial screen; they do not qualify a model
for long evidence packets, autonomous actions or investment conclusions.

## Retained-source relationship recovery

New extraction retains source-attributed relationships automatically. Existing
processed sources can be re-read without recreating their other extraction
objects. From the repository root:

```bash
backend/.venv/Scripts/python.exe scripts/backfill_source_relationships.py --evidence-id <uuid>
backend/.venv/Scripts/python.exe scripts/backfill_source_relationships.py --evidence-id <uuid> --apply
```

The default is a reversible preview, including the proposed assertions, using
the configured model. `--apply` checkpoints and retains cited, unverified
claims. Omit the evidence ID for a bounded recent-source pass; `--scan-limit`
and `--max-model-calls` control work. The pass stops on a provider failure and
never enables a local model or changes provider settings. Derived conversation
or reflection records and ineligible sources are skipped. Original source text
or its retained excerpt is required; a generated summary is not a substitute.
Completed and partial passes are skipped unless `--retry-completed` is explicit;
partial passes retain their review gaps rather than repeatedly spending model calls. Recovery
is assisted reprocessing, not evidence of autonomous or pre-event discovery.

## Guardrails

- Deterministic services own identities, timestamps, portfolio truth,
  calculations, and simulated execution.
- Raw model output cannot promote accepted state without policy checks.
- Evidence retains source lineage and event, publication, and ingestion times.
- All fixtures and documentation examples must be synthetic or public.
- The API is local-first and is not an authenticated shared-service boundary.
