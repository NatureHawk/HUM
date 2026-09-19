# SyncBoard — Architecture (Milestone 1)

SyncBoard is a shared Kanban workspace. The interesting problem is not CRUD; it is
keeping several connected browsers in agreement about one board without letting them
silently diverge.

Milestone 1 builds the foundation that Milestone 2's WebSocket layer plugs into.
Everything below describes the *whole* design; the "Status in M1" notes say what is
actually wired today.

---

## 1. Durable state vs ephemeral realtime state

This distinction drives almost every other decision, so it comes first.

**Durable state** is what the board *is*. If every server process died and every
browser closed, this is what must survive. It is the boards, the cards, their text,
their column, their order. It lives in **PostgreSQL**, it is transactional, and it is
the single source of truth.

**Ephemeral realtime state** is what is *happening right now*. Who is connected, where
their cursor is, which card someone has focused for editing, which server process holds
which socket. It is worthless five seconds after it is produced, and nobody should care
if it is lost on restart. It lives in **Redis** (and in process memory), never in
Postgres.

The failure mode this avoids is concrete: if you put presence in Postgres, every mouse
move becomes a durable write, autovacuum starts fighting your workload, and a crashed
browser leaves a permanent ghost user on the board. If you put card text in Redis, one
eviction loses a user's work.

A useful test: *would I be upset if this vanished on deploy?* Yes -> Postgres. No -> Redis.

**Status in M1:** Postgres holds all durable state and is fully wired. Redis is
provisioned, connected, and health-checked, but nothing publishes to it yet — that is
Milestone 2. It is deliberately not used as a cache; Postgres is fast enough at this
size and a cache would only add an invalidation bug surface.

---

## 2. Database schema

Two tables. Deliberately small.

### `boards`

| column       | type          | notes                                         |
|--------------|---------------|-----------------------------------------------|
| `id`         | `uuid` PK     | generated server-side                          |
| `title`      | `text`        | not null                                       |
| `revision`   | `bigint`      | **monotonic mutation counter for this board**  |
| `created_at` | `timestamptz` | server default `now()`                         |
| `updated_at` | `timestamptz` | bumped on every mutation                       |

### `cards`

| column        | type                             | notes                                              |
|---------------|----------------------------------|----------------------------------------------------|
| `id`          | `uuid` PK                        |                                                    |
| `board_id`    | `uuid` FK                        | to `boards.id`, `ON DELETE CASCADE`, indexed       |
| `column_key`  | `varchar(20)`                    | `CHECK` in (`todo`, `in_progress`, `done`)         |
| `title`       | `text`                           | not null, 1..200 chars enforced in Pydantic        |
| `body`        | `text`                           | not null, default `''`                             |
| `rank`        | `varchar(128)` **`COLLATE "C"`** | fractional index — see section 7                   |
| `version`     | `integer`                        | **optimistic-concurrency token for this card**     |
| `author_name` | `varchar(40)`                    | display name of whoever created it (real, client-set) |
| `created_at`  | `timestamptz`                    |                                                    |
| `updated_at`  | `timestamptz`                    |                                                    |

Index: `ix_cards_board_column_rank` on `(board_id, column_key, rank)` — the exact shape
of the only hot read (fetch one board's cards in display order).

### Three schema choices worth defending

**Columns are a `CHECK`-constrained string, not a table and not a PG `ENUM`.**
A separate `columns` table buys per-board custom columns, which the MVP does not have,
at the cost of a join on every read and a second ordering problem. A native PG `ENUM`
is the other tempting option, but adding a value later means `ALTER TYPE ... ADD VALUE`,
which historically could not run inside a transaction and is awkward in Alembic.
A `varchar` + `CHECK` gives the same integrity with a one-line migration to change it.

**`revision` on the board, not a separate `events` table.**
`revision` is a gapless, per-board sequence number. Every mutation increments it inside
the same transaction as the data change. This one integer is what makes Milestone 2
tractable: it is the event sequence number, the dedupe key, the gap detector, and the
resync cursor, all at once. A full event-log table is the natural next step if we ever
want replay or audit — but it is not needed to be correct, and an unused log is a
liability. Note we do *not* use a Postgres `SEQUENCE`: sequences are non-transactional
and leave gaps on rollback, and a gap is indistinguishable from a lost event.

**`version` on the card, separate from `revision`.**
They answer different questions. `revision` = "how much has this *board* changed?"
(ordering and resync). `version` = "is the card I am editing the one I last saw?"
(conflict detection). Collapsing them would mean any change anywhere on the board
invalidates your in-progress edit of an unrelated card.

---

## 3. REST API boundaries

REST handles anything a client can do *on its own behalf*, with a request/response
shape. WebSockets (M2) handle anything the server must *push*. The rule: a mutation is
never sent over the socket. Sockets are a fan-out channel, not a second write path.

```
GET    /api/health                        liveness + postgres + redis probes
POST   /api/boards                        create a board
GET    /api/boards                        list boards
GET    /api/boards/{board_id}             full snapshot (board + all cards + revision)
DELETE /api/boards/{board_id}             delete board and its cards

POST   /api/boards/{board_id}/cards       create card
PATCH  /api/cards/{card_id}               edit title/body
POST   /api/cards/{card_id}/move          move between/within columns
DELETE /api/cards/{card_id}               delete card
```

**Why `POST .../move` instead of `PATCH` with a position field.** Moving is not a field
assignment; it is an operation with its own arguments (target column + two anchors) and
its own failure mode (anchor vanished). Giving it a URL lets it have an honest request
body and an honest error, rather than smuggling intent into a generic patch.

**Every mutation returns the same envelope:**

```jsonc
{
  "event": {
    "id": "...",              // uuid, per-event, used for dedupe
    "type": "card.moved",
    "board_id": "...",
    "revision": 42,           // board revision AFTER this mutation
    "occurred_at": "...",
    "op_id": "...",           // echoed from the client's request
    "actor": { "id": "...", "name": "Amber Fox" },
    "payload": { /* typed per event type */ }
  },
  "board_revision": 42
}
```

This is the single most important interface decision in Milestone 1. **The HTTP
response to your own mutation is byte-for-byte the same event that Milestone 2 will
broadcast to everyone else.** So the frontend has exactly one function that mutates
board state — `applyEvent(state, event)` — and it does not care whether the event
arrived over `fetch` or over a socket. Milestone 2 adds a transport, not a second
code path.

---

## 4. Event structure

Events are explicit and typed (Pydantic on the server, discriminated unions in
TypeScript). No arbitrary JSON blobs cross the boundary in either direction.

| type                                | payload                                   |
|-------------------------------------|-------------------------------------------|
| `card.created`                      | the full card                             |
| `card.updated`                      | the full card (title/body changed)        |
| `card.moved`                        | the full card + `from_column`, `to_column`|
| `card.deleted`                      | `card_id`, `column`                       |
| `presence.joined` / `presence.left` | actor + connection id *(M2)*              |

Two conventions:

- **Card events carry the whole card, not a diff.** A diff makes the receiver's
  correctness depend on having every prior event. A whole-card payload is idempotent:
  applying it twice is the same as applying it once, which is what you want when a
  client reconnects and replays.
- **`presence.*` events carry no `revision`.** Presence is ephemeral (section 1); it is
  not part of the board's durable history, so it must not consume sequence numbers — a
  cursor move should never make another client think it missed a card change.

**Status in M1:** all four `card.*` events are produced and consumed for real, over
HTTP. `presence.*` is defined but not emitted.

---

## 5. Redis responsibility

Redis exists for exactly one architectural reason: **fan-out between backend
processes**.

The moment you run two uvicorn workers, a WebSocket held by worker A cannot be reached
from worker B, where the mutating HTTP request happened to land. An in-process
subscriber set is correct only at exactly one process, which is not a thing you can
promise in production.

The M2 design:

- `board:{id}:events` — pub/sub channel. After a mutation commits, the process
  publishes the event; every process with sockets on that board relays to its clients.
- `board:{id}:presence` — a hash of `connection_id -> actor`, with heartbeat-refreshed
  TTLs so a hard-disconnected browser expires instead of lingering.

Redis is explicitly **not** the source of truth, **not** a cache of card data, and
**not** a durable queue. If Redis is lost, clients see a stale board until they resync
from Postgres — degraded, not corrupted.

**Alternative considered:** Postgres `LISTEN/NOTIFY` would remove the Redis dependency
entirely and is genuinely good at this scale. It was rejected because the payload is
capped at 8000 bytes, it needs a dedicated held connection per process, and there is no
path from it to presence-with-TTL — so we would end up adding Redis anyway.

**Status in M1:** connection pool is created and `PING`ed by `/api/health`. Nothing is
published yet.

---

## 6. Concurrency strategy

No CRDTs. Two plain mechanisms, each deliberately visible.

### 6a. Per-board serialization (ordering)

Every mutation begins with:

```sql
SELECT id, revision FROM boards WHERE id = :id FOR UPDATE
```

This takes a row lock on the board for the duration of the transaction. Consequences:

- All mutations to one board are placed in a **total order** by the database.
- `revision` increments by exactly 1 per mutation, with **no gaps** (it rolls back with
  the transaction).
- Two concurrent moves cannot interleave halfway and produce a nonsensical order.

The cost is honest: writes to a single board are serialized. That is acceptable because
a board is edited by a handful of humans at human speed — tens of writes per second at
the absolute worst. Different boards do not contend at all. If a single board ever
became a bottleneck, the next step would be per-column locks or an append-only op log
with ordering resolved on read — both strictly more complex, so neither is justified yet.

### 6b. Per-card optimistic concurrency (conflict detection)

`PATCH /api/cards/{id}` requires `expected_version`. The update is:

```sql
UPDATE cards SET ..., version = version + 1
WHERE id = :id AND version = :expected_version
```

If zero rows match, the card changed under us. The server responds **409** with
`code: "version_conflict"` and the *current* canonical card in the body, so the client
can show a real conflict instead of guessing.

**What happens on simultaneous operations — the actual answers:**

| scenario | outcome |
|---|---|
| A and B both edit card X from `version 3` | Whoever's transaction commits first wins and produces `version 4`. The other gets `409 version_conflict` carrying card v4. **First-committer-wins, loser is told.** Nothing is silently lost. |
| A edits X while B moves X | Both succeed. Edits touch `title`/`body`; moves touch `column_key`/`rank`. They are disjoint fields, so there is no conflict to resolve — but both bump `version`, so a *third* stale writer is still correctly rejected. |
| A and B both move X to the same slot | Serialized by the board lock. The second one re-resolves its anchors against the state the first one produced. If its anchor is still valid the move lands beside it; if not, see below. |
| A moves X next to card Y while B deletes Y | A gets **409** `anchor_missing` with the current column contents. A's client resyncs and the user re-drops. |
| A edits X while B deletes X | Delete is terminal. A gets **404** `card_gone` and the client removes the card locally. |

This is the deliberate choice: **surface conflicts, do not auto-merge them.** A CRDT
would make both edits "succeed" and silently produce interleaved text nobody wrote. For
short card titles, telling the user "someone else changed this, here is their version"
is both simpler and more correct.

---

## 7. Ordering model

Card order must survive concurrent inserts without renumbering anything. Options:

| approach | why not |
|---|---|
| Integer `position`, renumber siblings | One insert writes N rows. Two concurrent inserts race into the same integer. Worst option. |
| Client-sent array index | The client's array is *already stale* by the time the request lands. This is exactly the "silent divergence" the project is about. Never trust it. |
| Float midpoint | `(a+b)/2` runs out of float64 precision after ~50 inserts at the same spot, then silently stops separating. |
| **Lexicographic fractional index** (chosen) | `O(1)` write, unbounded precision, no coordination. |

**Chosen: base-62 fractional index.** A `rank` is a string read as a base-62 fraction
(`"U"` is about 0.48). To insert between two cards, compute the midpoint of their ranks;
the string simply grows a character when it needs more resolution. Inserting between
`"U"` and `"V"` yields `"UU"`. Nothing else is rewritten.

Implementation is in [`backend/app/core/ordering.py`](../backend/app/core/ordering.py),
mirrored for optimistic client-side placement in
[`frontend/src/lib/rank.ts`](../frontend/src/lib/rank.ts).

Three details that matter:

1. **`COLLATE "C"` is mandatory on the `rank` column.** Postgres's default locale
   collation (e.g. `en_US.UTF-8`) does not sort by byte value — it folds case and
   reorders punctuation. Under it, `'a' < 'B'`, and the whole scheme breaks in a way
   that only shows up with mixed-case ranks in production. `COLLATE "C"` forces plain
   byte ordering, which is what the algorithm assumes.
2. **Canonical form: a rank never ends in `'0'`.** `"U"` and `"U0"` are the same
   number but different strings; allowing both breaks the invariant that lexicographic
   order equals numeric order, and then "midpoint" can land on an endpoint. Trailing
   zeros are stripped on generation and the result is asserted to be strictly between
   its bounds.
3. **The sort key is `(rank, id)`, not `rank`.** Ranks are not unique by construction.
   Adding `id` as a tiebreak makes the order *total* and identical on every client,
   even in the pathological case where two ranks collide.

**Anchor-based moves.** The move request says *what it wants to be next to*, not where
it wants to sit:

```jsonc
{ "column": "in_progress", "after_card_id": "...", "before_card_id": null }
```

The server resolves those anchors against **current** state and computes the rank
itself. A stale client index cannot corrupt the order, because the client never sends
one. If an anchor no longer exists or has left the target column, the server returns
`409 anchor_missing` rather than guessing — the project's goal is to make concurrency
legible, not to paper over it.

The four anchor shapes mean:

| request | meaning |
|---|---|
| both anchors | place strictly between these two cards |
| `after` only | place **immediately** after that card |
| `before` only | place **immediately** before that card |
| neither | append to the end of the column |

Single-anchor requests deliberately derive the *missing* bound from the column's
current contents rather than treating it as an open end. This was a real bug during
implementation: "before X" with an open lower bound means "somewhere above X", so every
repeated insert before the same card computed the identical rank and collided. The
integration test `test_many_inserts_at_the_same_spot_keep_their_order` covers it.
The anchorless case has the same shape — it must append relative to the column *as it
is now*, or twenty concurrent anchorless inserts all receive the same rank.

**Rank exhaustion and rebalancing.** Fractional indexing trades unbounded precision for
growing strings. In practice ranks stay tiny (5000 random inserts peak at 6 characters;
5000 consecutive appends at 83), but repeatedly inserting into *one fixed gap* halves it
each time and costs a character every ~6 inserts. Past `MAX_RANK_LENGTH` (128) the
generator raises `RankExhaustedError` rather than returning something subtly wrong, and
the service answers it by respacing that column with `evenly_spaced_ranks` and
resolving again. That respace is the O(N) write fractional indexing exists to avoid,
which is exactly why it is a rare repair path and not the normal one. It deliberately
does not bump `version`: no user-visible field changed, so no client's in-flight edit
should be invalidated by it.

---

## 8. Frontend state model

State is normalized and event-driven:

```ts
type BoardState = {
  board: Board | null;
  cards: Record<string, Card>;          // by id — no duplicated column arrays
  revision: number;                     // last applied board revision
  seenEventIds: string[];               // bounded ring for dedupe
  pending: Record<string, PendingOp>;   // op_id -> snapshot for rollback
  status: "loading" | "ready" | "desynced" | "error";
};
```

Columns are **derived** by filtering and sorting on `(rank, id)`, not stored. Two copies
of ordering information is two things to keep in sync, and they will drift.

The reducer in [`frontend/src/lib/board/reducer.ts`](../frontend/src/lib/board/reducer.ts)
handles the five cases the brief calls for:

- **Optimistic update** — apply locally, stash the previous card under an `op_id`.
- **Server acknowledgement** — `applyEvent` with the returned event; idempotent, so
  re-applying over the optimistic guess is harmless, and it corrects the guess (the
  server's rank is authoritative).
- **Remote update** — same `applyEvent`. M2 passes socket frames into it unchanged.
- **Rejected operation** — roll back from the stashed snapshot and surface the reason.
- **Duplicate events** — dropped by `event.id` in `seenEventIds`, and by
  `event.revision <= state.revision`.

**Never refetch the whole board on an event.** Events are applied in place. A full
snapshot is fetched in exactly two situations: first load, and recovery from desync.

---

## 9. Reconnect and resync

`revision` does the work. On every applied event the client checks:

```
event.revision === state.revision + 1   ->  in order, apply
event.revision <= state.revision        ->  duplicate/stale, drop
event.revision  > state.revision + 1    ->  GAP: we missed something
```

A gap means the local copy is provably incomplete, so the client marks itself
`desynced` and refetches the snapshot from `GET /api/boards/{id}`, which returns state
and revision atomically. That one comparison is why `revision` had to be gapless
(section 2).

**This already fires in Milestone 1.** Open two browsers on one board; make changes in
A; the next mutation in B returns a revision higher than B expected, B detects the gap
and resyncs. There is no push yet, but the detection and recovery path is real code on
a real trigger, not a stub.

In Milestone 2 the same logic covers socket reconnection: reconnect -> compare revision
-> either continue or snapshot. The client also always has a manual **Resync** control,
because "I think the screen is wrong" is a real user state.

---

## 10. Server authority

The client may optimistically render anything. The server decides:

- whether the operation is **valid** (Pydantic at the edge, `CHECK`s in the DB),
- whether it is **permitted** (auth is not in the MVP; the check lives in the service
  layer so there is one place to add it),
- what the **resulting state** is — notably the `rank`, which the client is never
  allowed to choose,
- what **event** is broadcast.

Layering enforces this: `api/routes` (HTTP shape only) -> `services` (rules, locking,
event construction) -> `repositories` (SQL only) -> `models`. Routes contain no business
logic, so there is no way to reach the database while skipping the rules.

---

## 11. Platform notes worth knowing

Two constraints that cost real time to diagnose, recorded so they do not cost it twice.

**psycopg cannot use Windows' default event loop.** Since Python 3.8 the default asyncio
loop on Windows is `ProactorEventLoop`, and psycopg's async driver raises
`InterfaceError` on its first connection there. Since uvicorn 0.36 the loop is chosen by
a *factory*, not the global asyncio policy, and `uvicorn/loops/asyncio.py` hardcodes
`ProactorEventLoop` on win32 — so neither `asyncio.set_event_loop_policy(...)` nor
`--loop asyncio` fixes it. `backend/run.py` overrides `Server.run` to supply
`asyncio.SelectorEventLoop` instead. Consequence: on Windows the server is limited by
`select()` to roughly 512 sockets, which is fine for development but is a reason not to
load-test Milestone 2's WebSocket fan-out on Windows. Linux is unaffected — its default
loop is already selector-based.

**`onupdate` + async + Pydantic needs `eager_defaults`.** A column with
`onupdate=func.now()` is expired after an UPDATE and refetched lazily. Under the async
engine, a lazy load triggered from synchronous code — such as Pydantic reading
`updated_at` while serialising an event — raises `MissingGreenlet`, because there is no
greenlet context to await the IO in. Both models set
`__mapper_args__ = {"eager_defaults": True}`, which fetches those values with a
`RETURNING` clause as part of the write itself.
