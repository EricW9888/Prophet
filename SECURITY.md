# Security Policy

Prophet stores local portfolio state, broker/email evidence, provider settings,
and research artifacts. Treat every working copy as sensitive by default.
The public source tree is a personal side project, not a supported distribution
or an invitation to operate Prophet as a shared service.

## Supported Versions

Security fixes are applied to the current `main` branch. No released version is
currently supported independently of `main`.

## Reporting A Vulnerability

Do not open a public issue with an exploit, credential, private portfolio data,
mailbox content, or broker statement. Use GitHub's private vulnerability
reporting for the repository when it is enabled. Until a private reporting
channel is configured, contact the repository owner privately and include only
the minimum reproduction needed to explain the issue.

Do not test against another person's data or a network-exposed Prophet instance.

## Deployment Boundary

Prophet is a local-first, single-user application. It currently has no complete
multi-user authentication or authorization boundary. Keep the backend and
frontend bound to loopback. Before any shared or internet-facing deployment,
add and verify authentication, authorization, CSRF protection, secure session
handling, rate limits, TLS termination, tenant isolation, audit logging, backup
encryption, and an explicit deployment threat model.

## Local Runtime Files That Must Stay Private

- `data/runtime_settings.json`
- `data/runtime_settings.json.secrets`
- `backend/data/runtime_settings.json`
- `backend/data/runtime_settings.json.secrets`
- `data/storage/`
- ignored local agent, project-memory, and tool-state directories
- `backend/*.db` and `backend/*.sqlite*`
- `.env` and `.env.*`
- `backups/`, `tmp/`, and scratch diagnostics

## Docker Notes

Docker build contexts must exclude runtime data and local databases. The root,
backend, and frontend `.dockerignore` files are part of the security boundary;
do not weaken them to make a local build convenient.

`docker-compose.yml` requires `POSTGRES_PASSWORD` from `.env` or the shell
instead of shipping a default password, and publishes PostgreSQL only on
loopback. The application images run as unprivileged users. These controls do
not turn the current local-first application into a supported shared service.

The development hard-reset API is disabled by default. `DEV_RESET_ENABLED=true`
only enables it when `ENVIRONMENT=development`, and requests are still limited
to loopback clients. Do not enable it in a shared or containerized deployment.

The API also rejects non-loopback peers by default and rejects cross-origin
state changes outside `FRONTEND_ORIGINS`. `API_ALLOW_NON_LOOPBACK=true` is an
explicit compatibility override only; it does not provide authentication,
authorization, or a supported shared-deployment boundary.

## Secret Handling

Ignored runtime secret files may contain working credentials after they are
entered through Settings. They are acceptable for local use only and must not
be copied into docs, tests, screenshots, issues, fixtures, or chat transcripts.
The active location is `data/runtime_settings.json` plus its `.secrets` sidecar.
Duplicate settings files under app subdirectories should be removed.

If a key is found in a committed file or pushed branch:

1. Revoke or rotate the key immediately.
2. Remove it from the repository and reachable history before release.
3. Re-run Gitleaks and `scripts/repository_policy_check.py`.
4. Document only a placeholder in `.env.example`.
