# Private Access

Prophet normally binds its frontend and API to loopback. To use the installed
web app from another owner-controlled device, keep those bindings unchanged and
put [Tailscale Serve](https://tailscale.com/docs/features/tailscale-serve) in
front of the Next.js frontend. Do not expose the frontend, API, Postgres, or
storage directories directly to a LAN or the public internet.

## Security boundary

This path relies on three independent controls:

1. Tailscale authenticates the connecting tailnet identity and applies the
   tailnet's access policy.
2. Next.js accepts a non-loopback origin only when Tailscale's
   `Tailscale-User-Login` header exactly matches the configured operator.
3. FastAPI continues to require an explicitly allowed browser Origin for state
   changes. The API remains reachable only from the loopback Next.js proxy.

Tailscale Serve removes client-supplied identity headers before adding its own.
The identity header is trustworthy here only because Next.js still listens on
loopback; a process exposed directly on the LAN could be sent a forged header.
This is owner access, not a general multi-user authentication system.

## Configure

1. Install and sign in to Tailscale on the Prophet host and the device that will
   use Prophet. Restrict the host and service to the operator in the tailnet
   access policy. Do not share the host with other users.
2. Start Prophet normally with `python scripts/prophet.py`. Confirm
   `python scripts/prophet.py status` reports both services on `127.0.0.1`.
3. Add the following private values to the host's ignored `.env`:

   ```dotenv
   PROPHET_REMOTE_ACCESS_USER=owner@example.com
   FRONTEND_ORIGINS=http://localhost:3000,http://127.0.0.1:3000,https://prophet-host.example-tailnet.ts.net
   # Optional when the remote-access login is an email address:
   # WEB_PUSH_VAPID_SUBJECT=mailto:owner@example.com
   ```

   Use the exact login shown by `tailscale status` and the exact HTTPS origin
   Tailscale assigns to the host. Restart Prophet after changing `.env`.
4. On the host, publish only the loopback frontend to the tailnet:

   ```bash
   tailscale serve --bg 3000
   tailscale serve status
   ```

5. Open the reported HTTPS URL on the authorized device. The browser can then
   install Prophet with **Add to Home Screen** or **Install app**.

## Mobile notifications

After opening the installed app over HTTPS, go to **Settings > System > Owner
notifications** and enable the current device. Permission is requested only from
that explicit action. On iPhone and iPad, Web Push requires a Home Screen web
app; a normal browser tab does not expose the same permission path.

Prophet creates one private VAPID identity under its ignored storage directory
and keeps each browser subscription in the local database. A durable delivery
outbox deduplicates watcher transitions, retries temporary push-service
failures, and retires subscriptions rejected as permanently invalid. Use **Send
test** in Settings to verify a device after enabling it.

Web Push also requires a valid VAPID contact URI. Prophet uses the configured
remote-access email as a `mailto:` contact by default. If that login is not an
email address, set `WEB_PUSH_VAPID_SUBJECT` explicitly to a contact such as
`mailto:owner@example.com`. Invalid sender configuration is reported in
Settings and fails immediately rather than being retried as a network outage.

Lock-screen messages deliberately omit tickers, positions, values, objectives,
and adjustment plans. They say only that a monitored condition needs review and
link back to Prophet. Notification delivery still depends on the host being on,
Prophet automation running, and the host having network access; the phone does
not run the research system itself.

An unconfigured identity, a missing Tailscale identity header, or a different
login receives HTTP 403. To remove access, run `tailscale serve reset`, remove
the two remote-access values from `.env`, and restart Prophet.

## Threat model and limits

- Never use Tailscale Funnel for Prophet. Funnel is public internet exposure.
- Keep the launcher-managed bindings on `127.0.0.1`; do not replace them with
  `0.0.0.0` or a LAN address.
- Keep tailnet membership, device sharing, and access rules narrow. A shared
  device can extend who reaches a Serve endpoint.
- The allowed Origin must match the browser URL exactly. This preserves the
  backend's cross-origin mutation check; the identity gate does not replace it.
- The PWA does not make background automation run on the phone. Automation,
  connectors, data, and secrets remain on the Prophet host.
- If the host is asleep, stopped, offline, or disconnected from Tailscale, the
  client shows the static offline notice and no live state. New alerts also
  wait until the host and its push dispatcher are running again.
- Compromise of the host or the operator's tailnet identity is outside this
  boundary. Revoke the device/session and rotate affected credentials.
