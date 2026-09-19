# SyncBoard

A shared spatial canvas built to get real-time collaboration right.

The interesting problem here is not CRUD. It is keeping several connected browsers in
agreement about one board without letting them silently diverge — which means being
explicit about ordering, concurrency, server authority, and what happens when two people
touch the same card at the same moment.

**Milestone 1** built the durable foundation: schema, REST API, event model, ordering,
concurrency, and the board UI.
**Milestone 2** added WebSockets, Redis fan-out and real presence — a transport, not a
second system.
**Milestone 3** replaced the Kanban board with a freeform canvas: sticky notes at arbitrary
world positions, overlapping, dragged around by several people at once.
**Milestone 4 (current)** finished the MVP and made it deployable: server-issued guest
sessions instead of client-declared identity, environment-driven configuration throughout,
a production container, and deployment documentation.

Design rationale, with the alternatives that were rejected and why:
[`docs/architecture.md`](docs/architecture.md), [`docs/realtime.md`](docs/realtime.md),
[`docs/canvas.md`](docs/canvas.md).
To put it on the internet: [`docs/deployment.md`](docs/deployment.md).

---

## Stack

| layer | choice |
|---|---|
| Frontend | Next.js (App Router) · TypeScript · Tailwind v4 |
| Backend | FastAPI · Pydantic v2 · SQLAlchemy 2 (async) · Alembic |
| Database | PostgreSQL 16 |
| Canvas | CSS transforms over a world coordinate space |
| Realtime | WebSockets |
| Coordination | Redis 7 pub/sub |
| Infrastructure | Docker Compose |

A modular monolith on purpose. No Kafka, no Kubernetes, no microservices, no CRDTs.

---

## Running it

Prerequisites: Docker, Python 3.12+, Node 20+.

**Ports.** SyncBoard uses 5434 / 6380 / 8001 / 3001 rather than the defaults, so it can
run alongside another project already holding 5432 / 6379 / 8000 / 3000.

```bash
cp .env.example .env
```

**1. Infrastructure**

```bash
docker compose up -d --wait
```

Starts Postgres on `5434` and Redis on `6380`, and waits for both healthchecks.

**2. Backend**

```bash
cd backend
python -m venv .venv
.venv/Scripts/python -m pip install -e ".[dev]"     # Windows
# .venv/bin/python -m pip install -e ".[dev]"       # macOS / Linux

.venv/Scripts/python -m alembic upgrade head
.venv/Scripts/python run.py
```

API on <http://localhost:8001>, interactive docs at <http://localhost:8001/docs>.

Use `run.py`, not `uvicorn` directly — it supplies an event loop psycopg can actually use
on Windows. See `run.py` and section 11 of the architecture doc for why.

**3. Frontend**

```bash
cd frontend
npm install
npm run dev
```

App on <http://localhost:3001>. Open it in two windows on the same board to see realtime
collaboration.

**Check it is alive**

```bash
curl http://localhost:8001/api/health/live    # {"status":"ok"} - no I/O at all
curl http://localhost:8001/api/health/ready   # probes Postgres and Redis, 503 if either is down
```

Liveness deliberately touches nothing, so a database blip cannot make a platform restart
healthy replicas. Readiness really probes both dependencies, under a timeout.

---

## Checks

```bash
# backend
cd backend
.venv/Scripts/python -m pytest          # 66 tests, real Postgres + real Redis
.venv/Scripts/python -m ruff check .
.venv/Scripts/python -m ruff format --check .
.venv/Scripts/python -m mypy app        # strict

# frontend
cd frontend
npm run typecheck && npm run lint && npm run test && npm run build

# the two-browser realtime test (needs the stack running)
npm run test:e2e
```

`test:e2e` drives two real Chromium browser contexts against the running app, API,
Postgres and Redis. Nothing in it is mocked — it is the verification that realtime
collaboration actually works.

The backend tests create and drop their own `syncboard_test` database, so they never
touch development data. They run against real PostgreSQL and real Redis rather than mocks,
because the behaviour most worth testing — `SELECT ... FOR UPDATE`, the field-scoped writes,
`ON DELETE CASCADE`, the compare-and-swap on `version`, and pub/sub fan-out — *is*
infrastructure behaviour. `tests/test_hub.py` goes furthest: it runs two `RealtimeHub`
instances with two Redis clients, sharing no Python memory, to prove an event published by
one backend process reaches a socket held by another.

---

## The five ideas worth understanding

**1. Durable state and ephemeral state are different things.**
What the board *is* (cards, text, order) lives in Postgres: transactional, the single
source of truth. What is *happening right now* (who is connected, whose cursor is where)
is worthless five seconds later and will live in Redis. Put presence in Postgres and
every mouse move becomes a durable write; put card text in Redis and one eviction loses
someone's work.

**2. One event shape, two transports.**
Every mutation returns the same envelope, and it is byte-for-byte the frame broadcast over
the WebSocket. So the frontend has exactly one function that advances board state,
`applyEvent`, and it does not care whether the event arrived by `fetch` or by socket.
Milestone 2 added a transport, not a second code path.

**3. Events reach other people through Redis, not through a second code path.**
A WebSocket is held by one process; the HTTP mutation that should reach it may land on
another, and neither can see the other's memory. So a committed event is published to
`board:{id}:events`, and every process holding a socket for that board relays it to its
clients. The socket never accepts a mutation — it only carries events — so there is still
exactly one write path. Redis is not the source of truth: if a publish is lost, clients
find out on their next revision gap and resync.

**4. Position is world state; the camera is yours alone.**
A note's `x`/`y` are WORLD coordinates — the board's own space, the same for everyone at
every zoom level. The camera that turns them into screen pixels (`{x, y, zoom}`) lives only
in your browser and is never persisted, so you and a collaborator can look at different
parts of the same board without fighting over the view. One CSS transform moves the whole
layer, so panning re-renders no notes at all.

**5. Conflicts are surfaced, not merged.**
Every mutation serialises on a `SELECT ... FOR UPDATE` of the board row, which gives all
writes to one board a total order and makes `revision` gapless. Text edits additionally
carry `expected_version` and compare-and-swap on it: the first to commit wins and **the
loser is told**, with the server's canonical note attached so the UI can show a real
conflict. A CRDT would let both "succeed" and quietly produce interleaved text nobody wrote.

Crucially, `version` guards **content only**. Moving, resizing and restacking write
different columns and do not touch it — so dragging a note past someone does not invalidate
the sentence they are part-way through typing. Position instead resolves last-writer-wins,
which is the right answer for two people physically fighting over one object.

---

## What works today

- Create, list, open and delete boards
- Create, edit (title and body), move, resize, restack and delete notes
- **Real-time collaboration**: two browsers on one board see each other's creates, edits,
  moves and deletes immediately, with no refresh
- **Real presence**: live collaborator avatars, multi-tab aware, plus an advisory
  "someone is editing this note" hint
- Automatic reconnect with bounded backoff, and canonical resync on reconnect
- Pan, zoom, fit-to-content, and a per-browser camera that nobody else can move
- Live collaborative cursors and drag-streaming that never touch the database
- Field-aware concurrency, verified across two browsers
- Gapless per-board revisions, client-side gap detection and resync
- Board state surviving a refresh, and a guest identity surviving a return visit
- Health checks that really probe Postgres and Redis

## Limitations

- **No accounts.** Identity is a server-issued *guest* session: a signed, HTTP-only cookie
  the browser can neither read nor forge. That is enough to stop impersonation — a visitor
  can only ever act as themselves — but there are no passwords, no sign-in and no per-board
  access control. **Any board is readable and writable by anyone with its link**, which is a
  deliberate MVP choice for a public demo, not an oversight.
- **One process in development.** The Redis fan-out is written and tested for multiple
  processes (`test_hub.py` proves it with two hubs), but the dev stack runs a single
  uvicorn worker.
- **Windows socket ceiling.** psycopg needs a selector event loop on Windows, which caps
  out around 512 sockets. Irrelevant locally, and irrelevant on Linux.
- **No message history.** A client that misses events resyncs from a full snapshot rather
  than replaying a log. Simple and correct; an append-only event table is the natural next
  step if replay or audit is ever needed.
