# Prophet Frontend

Next.js interface for Prophet's portfolio, chat, research, source, knowledge,
risk, activity, verification, and paper-investing workflows.

## Run

Start the backend on `127.0.0.1:8000`, then:

```bash
npm ci
npm run dev -- --hostname 127.0.0.1 --port 3000
```

Open [http://127.0.0.1:3000](http://127.0.0.1:3000). The frontend proxies
`/api_proxy/*` to the backend's `/api/*` routes.

## Verify

```bash
npm run lint
npm run build
npm audit --audit-level=low
```

Runtime secrets belong in Prophet's Settings flow or deployment secret manager,
never in frontend source or `NEXT_PUBLIC_*` variables.
