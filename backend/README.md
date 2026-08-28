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

From `backend/`:

```bash
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

## Guardrails

- Deterministic services own identities, timestamps, portfolio truth,
  calculations, and simulated execution.
- Raw model output cannot promote accepted state without policy checks.
- Evidence retains source lineage and event, publication, and ingestion times.
- All fixtures and documentation examples must be synthetic or public.
- The API is local-first and is not an authenticated shared-service boundary.
