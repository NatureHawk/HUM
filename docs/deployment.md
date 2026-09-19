# SyncBoard — Deployment

This is the deployed shape, as opposed to the local one in the README. It matters
because the deployed topology is genuinely different from the local one — frontend
and API on different hosts, different origins, HTTPS everywhere — and several settings
in `app/core/config.py` exist *specifically* to make that safe. This doc explains what
each of them does and why, not just what to paste in.

---

## 1. Architecture

```
Browser
  │  HTTPS (page, REST)
  │  WSS   (realtime)
  ▼
Next.js  ──────────────────────────────  Vercel (or any static/edge host)
  │
  │  HTTPS (REST, same origin as the WS below)
  │  WSS   (/ws/boards/{id})
  ▼
FastAPI  ──────────────────────────────  Render / Railway / Fly (a long-running container)
  │                          │
  │ Postgres wire protocol   │ Redis wire protocol (rediss:// if TLS)
  ▼                          ▼
Managed Postgres          Managed Redis
```

The frontend and the API are on **different origins** in this topology — e.g.
`https://syncboard.vercel.app` and `https://syncboard-api.onrender.com`. That single
fact is what drives almost everything below: it is why CORS has to name an explicit
origin, why the session cookie needs `SameSite=None; Secure`, and why the whole chain
has to be HTTPS end to end (a mixed HTTP/HTTPS chain breaks the cookie, and a browser
on an HTTPS page refuses a plain `ws://` socket outright). None of this is exotic
configuration bolted on for production — it is the direct consequence of the frontend
and the API not sharing a host, which is true the moment they are deployed separately.

---

## 2. Why this platform split

The binding constraint is the WebSocket at `/ws/boards/{board_id}`
(`backend/app/api/routes/ws.py`). A connection there is expected to live for minutes
or hours, holding an open socket, a presence entry and a subscription to a Redis
channel the whole time. A serverless or edge function runtime (Vercel functions,
Cloudflare Workers, AWS Lambda) is built around the opposite assumption — a request
runs, returns, and the runtime is free to recycle it — so it cannot hold a long-lived
connection at all. That rules out running the API as serverless functions regardless
of how appealing "one platform for everything" sounds.

The API therefore needs a normal, long-running container service: Render, Railway or
Fly all work, because all three run the Docker image as a persistent process rather
than an invoked function. This doc uses Render as the concrete example, but nothing
here is Render-specific beyond the exact dashboard field names.

The Next.js frontend has no such constraint — it serves pages and makes outbound
`fetch`/WebSocket calls, it never holds a long-lived connection itself — so it can run
anywhere, including an edge/serverless platform like Vercel. Vercel is the default
recommendation here because it is the natural fit for Next.js specifically, not
because the other platforms can't serve it.

**If one dashboard is preferred over two:** Railway can host both the API (as a
container service) and the frontend (as a static/Node service) side by side. The
trade-off is real but small — one bill and one place to look at logs, versus Vercel's
purpose-built Next.js build pipeline (preview deployments per branch, edge caching,
image optimisation) that a generic Node host does not replicate. Either is a legitimate
choice; this doc documents the two-platform split because it is what most clearly
separates "things that must be a long-running server" from "things that need not be."

---

## 3. Environment variables

### Backend — secrets and config (server-side only, never sent to the browser)

| Variable | What it is | Example | Required |
|---|---|---|---|
| `ENVIRONMENT` | `development` or `production`. Gates the startup checks below and the session cookie's flags (`app/core/session.py`). | `production` | Yes in deployment |
| `DATABASE_URL` | Postgres connection string. Accepts `postgres://` or `postgresql://` as handed out by managed providers — see note below. | `postgresql://user:pass@host:5432/syncboard` | Yes |
| `REDIS_URL` | Redis connection string for realtime fan-out and presence. `rediss://` (TLS) works unchanged. | `rediss://default:pass@host:6379` | Yes |
| `SESSION_SECRET` | Signs the guest session cookie (`itsdangerous`). Must be stable across restarts and identical across every replica, or guests get logged out and replicas disagree about who anyone is. | output of the command below | Yes in production — startup fails without it |
| `FRONTEND_URL` | The deployed frontend's origin. Becomes an allowed CORS origin and is the common case for the single-frontend deployment. | `https://syncboard.vercel.app` | Yes in production (or `CORS_ORIGINS`) |
| `CORS_ORIGINS` | Optional extra allowed origins, comma-separated — preview deployments, a second domain. Parsed by a validator that also accepts a JSON array. | `https://preview-123.vercel.app,https://staging.example.com` | No |
| `PORT` | The port uvicorn binds. Injected by the platform (Render, Railway); `entrypoint.sh` reads it and defaults to `8000` if unset. | `10000` | Set by the platform, not you |
| `WEB_CONCURRENCY` | Number of uvicorn worker processes. See §6 for why this is safe here. | `2` | No — defaults to `1` |
| `MAX_NOTES_PER_BOARD` | Abuse ceiling for a public demo with no auth: caps notes on one board. | `500` | No — has a default |
| `MAX_REQUEST_BYTES` | Caps the size of a single mutation request body. | `65536` | No — has a default |
| `SQL_ECHO` | Logs every SQL statement. Useful locally, noisy and slightly slower in production. | `false` | No — defaults to `false` |

### Frontend — public config (compiled into the client bundle, world-readable)

| Variable | What it is | Example | Required |
|---|---|---|---|
| `NEXT_PUBLIC_API_URL` | The API's origin — scheme, host, port, **no path**, no trailing `/api`. Both the REST base (`{origin}/api`) and, by default, the WebSocket base are derived from this one value (`frontend/src/lib/config.ts`), which is what keeps them from drifting apart. | `https://syncboard-api.onrender.com` | Yes in production — defaults to `http://localhost:8001` locally |
| `NEXT_PUBLIC_WS_URL` | Overrides the derived WebSocket origin, for the unusual case where sockets terminate somewhere other than the API's own origin. Normally left unset — one variable that cannot disagree with itself is the point. | `wss://syncboard-api.onrender.com` | No |

**Nothing secret may ever go in a `NEXT_PUBLIC_*` variable.** Next.js inlines every
`NEXT_PUBLIC_*` value into the JavaScript it serves — it is not read at request time
on a server, it is baked into the bundle at build time and shipped to every visitor's
browser. Anyone who opens devtools can read it. There is no backend secret that
belongs here; `SESSION_SECRET`, `DATABASE_URL` and `REDIS_URL` are backend-only for
exactly this reason and must never be prefixed `NEXT_PUBLIC_`.

**`get_settings()` refuses to start in production** (`backend/app/core/config.py`)
without `SESSION_SECRET`, and without either `FRONTEND_URL` or `CORS_ORIGINS` — the
alternative is a service that starts, logs guests out on every restart because the
secret regenerated, or serves an API no browser can actually call because
`allowed_origins` is empty. Generate a secret with:

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

**On `DATABASE_URL`:** managed providers (Render, Railway, Neon) hand out
`postgres://` or `postgresql://`. A `field_validator` on `Settings` rewrites either to
`postgresql+psycopg://` automatically, so the value copied straight from a provider's
dashboard works unchanged — no manual driver-scheme editing needed.

---

## 4. Database and migrations

Migrations run automatically on every container start: `entrypoint.sh` runs
`alembic upgrade head` before handing off to uvicorn, so a fresh deploy and a restart
of an existing one both leave the schema current. There is no separate migration step
to remember to run.

To run migrations manually against a fresh database — for a one-off check, or before
pointing a container at it — from `backend/`:

```bash
DATABASE_URL=postgresql+psycopg://user:pass@host:5432/dbname \
  .venv/Scripts/python -m alembic upgrade head
```

**Never run `alembic downgrade` against a production database.** It is destructive by
design, not by accident: the `0002` downgrade (`backend/alembic/versions/0002_spatial_notes.py`)
folds notes back onto `column_key`/`rank` by bucketing their `x` position into three
columns and re-ranking by `y`, and that cannot faithfully restore the state a Kanban
board actually had, because a canvas holds strictly more information than three
ordered lists — arbitrary `x, y`, overlap, rotation and free z-ordering do not fit
back into "which of three lists, and where in it" without genuinely losing
information. Content, ids, authorship and timestamps survive; the spatial layout does
not round-trip.

---

## 5. Redis

Redis is read from `REDIS_URL` with no scheme-specific handling required —
`rediss://` (TLS, what most managed Redis providers require) works exactly like
`redis://`.

Be honest with yourself about what Redis is for here: realtime coordination only,
never the source of truth. Board state — notes, positions, text, authorship — lives
durably in Postgres. If a publish to Redis fails or a connection drops, no data is at
risk; a connected client simply does not hear about the change immediately. It finds
out on its next mutation or heartbeat, when the revision it holds no longer matches
the server's and it resyncs from a REST snapshot (see `docs/realtime.md` §4 and §9 of
`docs/architecture.md`). That is degraded — a client briefly shows a stale board — not
corrupt. This is also why a Redis outage should never be treated as a reason to
restart the API: liveness does not depend on it (§8).

---

## 6. Backend deployment (Render or equivalent)

1. Deploy `backend/Dockerfile` as a Docker-based web service — not buildpacks, the
   Dockerfile is the deployment artifact.
2. Health check path: `/api/health/ready` (see §8 for why not `/live`).
3. Start command: none needed. The image's `ENTRYPOINT` (`entrypoint.sh`) already
   runs migrations and then `exec`s uvicorn bound to `0.0.0.0:$PORT` — overriding it
   would skip the migration step.
4. The service must bind `0.0.0.0` and read `PORT` from the environment, which it
   already does; nothing to configure here beyond setting the environment variables
   in §3.
5. Set `WEB_CONCURRENCY` to more than one worker if the expected load calls for it.
   Multiple workers are correct here *specifically because* Redis bridges them: a
   WebSocket held by worker 1 and an HTTP mutation that lands on worker 2 still reach
   each other, because the mutating worker publishes to `board:{id}:events` and every
   worker with a local socket on that board relays it. `backend/tests/test_hub.py`
   (`test_event_published_on_hub_a_is_delivered_through_hub_b`) proves exactly this
   path — two separate hub instances, two separate Redis clients, sharing no Python
   memory — so scaling workers is not a leap of faith.

---

## 7. Frontend deployment (Vercel or equivalent)

- Root directory: `frontend`.
- Build command: `npm run build`.
- Environment variable: `NEXT_PUBLIC_API_URL` set to the deployed API's origin, with
  **no trailing `/api`** — `frontend/src/lib/config.ts` appends `/api` itself, and a
  double `/api/api` is a common paste error here.

---

## 8. WebSockets

This is the single most failure-prone part of a first deployment, almost always for
the same reason: the socket URL and the page's scheme disagree.

The client never hardcodes a WebSocket URL. It derives one from `NEXT_PUBLIC_API_URL`
by swapping the leading `http` for `ws` (`wsOrigin()` in `frontend/src/lib/config.ts`),
so `https://api.example.com` becomes `wss://api.example.com` automatically. This is
deliberate: a browser on an HTTPS page refuses to open a plain `ws://` connection at
all (mixed content), so if `NEXT_PUBLIC_API_URL` is `https://...`, the socket
following it must also upgrade to `wss://` — there is no correct configuration where
the page is HTTPS and the socket is `ws://`. Getting the origin variable right is
therefore the entire fix; there is no separate WebSocket URL to configure unless
sockets terminate somewhere else entirely, which is what `NEXT_PUBLIC_WS_URL` is for.

The socket rides the **same public HTTP port** as the REST API, at
`/ws/boards/{board_id}` (`backend/app/api/routes/ws.py`). There is no second port to
open, no separate service to deploy, and no separate ingress rule to write — anything
that can reach `/api/health/ready` can reach `/ws/boards/{id}`.

The one thing to check on the platform side: it must not impose a short idle timeout
that kills long-lived connections. A board can sit open with no traffic beyond a
20-second ping for hours; a reverse proxy or load balancer with (for example) a
60-second idle timeout on a passthrough connection will silently sever every socket
and force constant reconnects. Render, Railway and Fly's default proxies handle
long-lived WebSockets without extra configuration; if fronting the API with a custom
proxy or CDN, confirm its WebSocket idle timeout explicitly.

---

## 9. CORS and cookies

`FRONTEND_URL` (plus any `CORS_ORIGINS`) sets `Settings.allowed_origins`
(`backend/app/core/config.py`), the exact list of origins the API answers CORS
preflights for. The wildcard `*` is never used and cannot be made to work here: the
session cookie travels with `credentials: include`, and browsers categorically forbid
combining a credentialed request with a wildcard `Access-Control-Allow-Origin`. The
API must name the frontend's real origin, which is the whole reason `FRONTEND_URL`
exists as its own variable rather than leaving CORS to a blanket allow.

The cookie's flags depend on `ENVIRONMENT` (`backend/app/core/session.py`,
`cookie_params()`). In production it is issued as `SameSite=None; Secure; HttpOnly`.
`SameSite=None` is required because the frontend and API are cross-origin (§1); a
browser will not attach a `SameSite=Lax` or `Strict` cookie to a cross-origin request
at all, which would make every guest un-identifiable. Browsers only honour
`SameSite=None` together with `Secure`, which is the other reason the whole chain must
be HTTPS end to end — an API served over plain HTTP in production would have its
session cookie silently refused by every browser. Locally, where frontend and API
share nothing but different ports over plain HTTP, the cookie instead uses
`SameSite=Lax; Secure=False`, because `Secure` over HTTP means the cookie is never
stored at all.

---

## 10. Identity for the public demo

There are no accounts, passwords or OAuth. A guest's identity is a signed, HTTP-only
cookie (`sb_session`, `backend/app/core/session.py`) holding a server-issued guest id
and display name. The browser cannot read it (`HttpOnly`) and cannot forge or rename
it (`itsdangerous` signs it with `SESSION_SECRET`) — the only way to become "someone"
is for the server to have issued that identity. This is deliberately the smallest
thing that is actually sound, not a placeholder for something stronger arriving later
by default.

Boards are public to anyone holding the link — there is no per-board access control.
This is a deliberate MVP choice for a public demo, not an oversight: say so plainly to
anyone deploying this, because it means a board's contents and its guest list are
visible to whoever has the URL.

---

## 11. Health checks

Two endpoints, because a platform uses them for different purposes
(`backend/app/api/routes/health.py`):

- `/api/health/live` does no I/O at all. It answers `{"status": "ok"}` unconditionally
  from a running process. **Point the platform's restart/liveness probe here.** If it
  instead depended on Postgres or Redis, a brief database blip would make the platform
  kill and restart every healthy replica simultaneously — turning a recoverable outage
  into a crash loop.
- `/api/health/ready` actually probes Postgres and Redis, each under a 3-second
  timeout, and returns 503 if either is down. This is the right check for **routing**
  decisions (e.g. Render's health check used to gate traffic during a deploy) — a
  replica that cannot reach its dependencies should not receive traffic even though the
  process itself is alive.

Render's single "health check path" setting should point at `/api/health/ready`: it
gates whether a new deploy is considered healthy enough to receive traffic, which is a
readiness question, not a liveness one.

---

## 12. Deployment checklist

1. Provision managed Postgres and managed Redis; copy their connection strings.
2. Generate a session secret: `python -c "import secrets; print(secrets.token_urlsafe(32))"`.
3. Deploy the backend from `backend/Dockerfile` (Render/Railway/Fly), setting
   `ENVIRONMENT=production`, `DATABASE_URL`, `REDIS_URL`, `SESSION_SECRET`, and
   `FRONTEND_URL` (the frontend's URL from step 5 — circular on a first deploy, so set
   it to the frontend's *intended* URL now and correct it if the platform assigns a
   different one).
4. Point the platform's health check at `/api/health/ready`. Confirm the platform does
   not impose a short idle timeout on long-lived connections (§8).
5. Deploy the frontend (Vercel/equivalent) with root directory `frontend`, build
   `npm run build`, and `NEXT_PUBLIC_API_URL` set to the backend's URL from step 3, no
   trailing `/api`.
6. If the frontend's final URL differs from what was guessed in step 3, update
   `FRONTEND_URL` on the backend and redeploy it.
7. Open the deployed frontend, create a board, open it in a second browser or private
   window, and confirm a note created in one appears in the other — this exercises the
   cookie, CORS, and the WebSocket path in one check.
8. Check `/api/health/ready` returns `{"status": "ok", ...}` with both dependencies
   `ok: true`.

---

## 13. Local development

Local development is unchanged by any of the above: `docker compose up -d --wait` for
Postgres and Redis, then the backend and frontend run directly on the host, exactly as
the README describes. `docker-compose.yml` only ever provisions the two stateful
services for a laptop — it is not how anything here is deployed, and it isn't a
production tool at all; there is no `docker compose up` step in the checklist above.
