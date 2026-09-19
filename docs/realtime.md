# SyncBoard — Realtime (Milestone 2)

Milestone 1 built a board that was correct but silent: your changes were durable
and ordered, and nobody else found out until they acted. This milestone adds the
delivery layer, and deliberately adds *only* that. No mutation moved, no state
model changed, and the event envelope is the one M1 already defined.

Read [`architecture.md`](architecture.md) first — sections 1, 4, 5 and 9 set up
everything below.

---

## 1. The shape of it

```
Browser A                                                     Browser B
    │ POST /api/cards/{id}/move                                    ▲
    ▼                                                              │
 FastAPI (worker 1)                                        FastAPI (worker 2)
    │ service layer: lock board, validate, write                   │
    ▼                                                              │
 Postgres  ── commit ──▶ event envelope, revision N                │
    │                                                              │
    │ publish AFTER commit                                         │ WebSocket
    ▼                                                              │
 Redis  ─────── board:{id}:events ────────────────────────────────▶┘
```

Two properties this arrangement buys:

**A mutation has exactly one implementation.** The socket carries events; it
never accepts a change. The only frames a client may send are `ping`, `resync`
and `editing`, none of which touch durable state. Every board change is still a
REST call into the same service layer, the same board-row lock and the same
transaction as in M1.

**An event never describes a change that did not happen.** `publish_event` is
called after `session.commit()`, never inside the transaction. Publishing early
would broadcast changes that could still roll back.

---

## 2. WebSocket protocol

Endpoint: `wss://host/ws/boards/{board_id}` — no query parameters.

It sits outside `/api` on purpose: it is a different protocol, not another
resource. It rides the same public port as the REST API, so a deployment opens
nothing extra.

**Identity comes from the session cookie**, which the browser sends with the
WebSocket handshake like any other request. Milestones 2-3 took an `actor_id`
and `actor_name` off the query string, which was fine on a laptop and
indefensible in public: anyone could attribute notes to anyone and drive
somebody else's cursor. There is now no way for a connection to state who it
is - it is whoever the server said it was when it issued the cookie. See
`backend/app/core/session.py`.

### Server → client

Every frame discriminates on `type`.

| frame | payload |
|---|---|
| `sync` | `connection_id`, `actor`, `board`, `cards`, `revision`, `presence[]` |
| `card.created` / `card.updated` / `card.moved` / `card.deleted` | **the M1 event envelope, unwrapped** |
| `presence.joined` / `presence.left` / `presence.updated` | `board_id`, `occurred_at`, `member` |
| `error` | `code`, `message` |
| `pong` | `server_time` |

Board events go on the wire **unwrapped**, because they already carry a `type`
field — the same field every other frame discriminates on. The consequence is
the one that matters: the JSON a client receives from a socket is byte-for-byte
the JSON it receives from its own REST mutation, so both feed one `applyEvent`.

`presence.*` frames deliberately carry **no `revision`**. Presence is not part
of the board's durable history, so it must not consume sequence numbers — a tab
opening must never make another client believe it missed a card change.

Presence frames carry exactly **one** member — a delta, not the whole room.
That is the opposite of the obvious design, and §5 explains why the obvious one
is wrong.

### Client → server

```jsonc
{"type": "ping"}                              // also the presence heartbeat
{"type": "resync"}                            // "send me a fresh sync frame"
{"type": "editing", "card_id": "…" | null}    // advisory, never a lock
```

Anything else fails Pydantic validation at the edge and is answered with
`{"type":"error","code":"invalid_event"}`. **The socket stays open** — one bad
frame is not a reason to drop a working connection.

### Errors

`unauthorized` · `board_not_found` · `invalid_event` · `stale_operation` ·
`rate_limited` · `message_too_large` · `server_error`

Close codes use the 4000–4999 application range: `4401`, `4404`, `4400`, `4429`,
plus `1011` for an internal failure. No exception or stack trace ever reaches a
client.

---

## 3. The handshake, and the hole it closes

The obvious order is "read the board, then subscribe". It is wrong. Anything
committed between the read and the subscribe is lost silently, and the client
believes it is current.

So [`ws.py`](../backend/app/api/routes/ws.py) inverts it:

```
1. accept
2. validate the board exists          ← access gate, before anything else
3. register with the hub              ← SUBSCRIBED from this instant;
                                        arriving events queue up
4. read the snapshot at revision R    ← one transaction: board + cards
5. join presence
6. send `sync`                        ← sent directly, ahead of the queue
7. start the writer                   ← flushes everything buffered in 3–5
```

Events buffered during steps 4–5 are delivered right after `sync`. Those with
`revision <= R` are already in the snapshot and the client drops them by
revision; those above R apply in order. Nothing slips through, because the
subscription predates the read.

This is why `Connection` has an outbound queue whose writer starts *late*: the
queue is what makes "subscribe first" safe.

**Sessions.** A socket lives for hours; a database connection must not. The
endpoint takes no session dependency — it opens a short-lived session for the
access check, the snapshot and each resync, and holds none in between.

---

## 4. Redis architecture

Redis finally does the job it was provisioned for in M1.

A WebSocket is held by one specific process. The HTTP mutation that should reach
that socket may land on a different one, and neither can see the other's memory.
An in-process subscriber set is correct at exactly one process, which is not
something you can promise in production.

- **Channel:** `board:{board_id}:events` — carries both board events and
  presence frames, already serialised as the exact text to send.
- **Subscription is dynamic.** A process subscribes on its *first* local
  connection to a board and unsubscribes on its *last*, so it only carries
  traffic for boards it actually serves. `RealtimeHub.local_connection_count`
  is the count of sockets *this process* holds — never the board's total.
- **Published events return through Redis even for local sockets.** The
  publisher does not also deliver directly. The loopback costs a millisecond and
  buys a single delivery path: no way to double-deliver to a local client, and
  every client on every process observes the same order.
- **Backpressure.** Each connection has a bounded outbound queue
  (`OUTBOUND_QUEUE_SIZE`). One stalled browser cannot back up the dispatcher for
  everyone else; on overflow that connection is dropped and reconnects.

Redis remains **not** the source of truth, **not** a cache of card data, and
**not** a durable queue. If a publish fails, the mutation still succeeded — the
write is already committed. Clients simply do not hear about it until their next
mutation exposes a revision gap and they resync. Degraded, not corrupt.

`test_hub.py` proves the cross-process path by running **two hub instances with
two Redis clients** in one test: publish from A, receive at B. That is the
worker-1-to-worker-2 path, verified without deploying two workers.

---

## 5. Presence model

**Presence is per connection. Frames are per-connection deltas. The UI groups
by actor.**

A connection is the thing that can be open or closed, so it is what the server
tracks. But a person with three tabs is one person, so `PresenceRail` groups
members by `actor.id` and shows a small count badge when someone has more than
one. Both halves stay honest.

Storage, per board:

```
board:{id}:presence            HASH  connection_id -> member JSON
board:{id}:presence:heartbeat  ZSET  connection_id -> last-seen epoch
```

The ZSET is what makes abrupt disconnects self-healing. A clean close removes
the entry immediately; a killed browser or a severed network sends no close
frame at all, so every read first evicts connections whose heartbeat is older
than `PRESENCE_TTL_SECONDS` (75s). The client pings every 20s, which doubles as
that heartbeat — so one dropped ping never evicts a healthy connection.

A hash field cannot carry its own TTL, which is why the timestamp lives in a
parallel sorted set rather than in the hash value.

None of this touches Postgres. Presence is the textbook ephemeral state: put it
in a durable store and every heartbeat becomes a write, while a crashed browser
leaves a permanent ghost on the board.

### Why deltas, not full state

The first implementation broadcast the whole member list on every change. It was
wrong, and the two-browser test caught it.

A full list has to be assembled at publish time, so two presence changes that
interleave race each other: a `left` published slightly *later* can carry a list
assembled slightly *earlier*, and overwrite a newer one. This is not
hypothetical. A reconnect — or React re-mounting a component in development —
closes one socket and opens another within milliseconds:

```
conn X closes ──▶ leave(X), read members → [A, B]  ─┐
conn Y opens  ──▶ join(Y),  read members → [A, B, Y] ┤ published in either order
                                                     ▼
              client applies [A, B, Y] then [A, B]  ✗  Y silently vanishes
```

Per-connection deltas have no such failure mode. Each frame concerns exactly one
`connection_id`, so frames for *different* connections commute, and frames for
the *same* connection are published in order by the single handler that owns it.
The client keys a map on `connection_id` and applies join/leave/update directly;
the `sync` frame supplies the baseline it starts from, and any drift is repaired
by the next reconnect or resync.

The general lesson: "broadcast whole state, it is simpler" is only safe when the
state has a version the receiver can order by. Without one, deltas over
independent keys are the safer primitive.

### Edge cases

| case | behaviour |
|---|---|
| same user, multiple tabs | N members, one avatar, count badge showing N |
| refresh | old connection closes, new one joins; both frames are full-state so order does not matter |
| abrupt disconnect (killed tab, dead network) | no close frame; the entry expires by heartbeat TTL |
| normal disconnect | `presence.left` immediately, in the endpoint's `finally` |
| duplicate presence frames | harmless — upserting or deleting the same `connection_id` twice is idempotent |
| you receive your own `presence.joined` | expected: it comes back through Redis like everything else, and replacing the list with itself is a no-op |

`editing_card_id` is **advisory**. It shows "Bob is editing" on a card. It is not
a lock, and it never blocks anyone — the server's `version` check is the real
conflict mechanism.

---

## 6. Reconnect strategy

[`socket.ts`](../frontend/src/lib/realtime/socket.ts) is a plain class, not a
hook, for one reason: React double-invokes effects in development, and a socket
opened in an effect body tends to become two sockets, two sets of listeners, and
every event applied twice. One object, one owner, one `connect()`.

- `connect()` returns immediately if a socket already exists — the guard that
  makes double-invocation harmless.
- On close (not initiated by us) it schedules a retry: exponential backoff from
  500ms, capped at 15s, with 50–100% jitter. The cap stops a dead server being
  hammered forever; the jitter stops every client that dropped simultaneously
  from reconnecting in lockstep and doing it again.
- On reconnect the full handshake runs again, so recovery is just §3 — the
  client gets a fresh `sync` and replaces its state. There is no separate
  "catch-up" path to get wrong.
- `close()` on unmount tears down the socket, the heartbeat and any pending
  retry, so a route change cannot leave a stale socket feeding a dead reducer.

---

## 7. What did NOT change

This is the part worth checking. Milestone 2 added a transport, not a second
system.

- **`applyEvent` is still the only way board state changes.** Socket frames go
  through the same `eventReceived` action as REST responses. There is no
  `handleRemoteMove` / `handleRemoteEdit` shadow path.
- **Duplicate suppression, gap detection and resync are M1 code, unchanged.**
  Events are dropped by `event.id` and by `revision <= state.revision`; a gap
  (`revision > state.revision + 1`) still sets `desynced` and still triggers a
  canonical REST snapshot. It already worked; sockets just make it fire more.
- **Optimistic updates are unchanged.** Same optimistic apply, same rollback,
  same `version_conflict` rebase onto the server's canonical card.
- **REST still works on its own.** The board loads and mutates with the socket
  closed; realtime is an enhancement layered on top, not a replacement.

### Three things that did change, and why

**1. One `op_id` per operation, shared by the optimistic record and the request.**
In M1 the reducer minted an id for its pending map while the API call minted a
different one for the server — harmless then, because nothing compared them.
With broadcasts it is load-bearing: the server echoes `op_id`, and that is how a
client recognises its own mutation coming back.

**2. `op_id` reconciliation in the reducer.** A client can receive the broadcast
of its own mutation *before* the HTTP response. When it does, the authoritative
card is already present under its real id, so the optimistic placeholder must be
retired right there — otherwise the card is visibly duplicated for the length of
the round trip.

**3. `expected_version` is the version the editor was opened at.** Previously
`CardTile` sent the card's *current* version on save. Once remote edits arrive
mid-edit, that silently became "I accept whatever just changed" and would
overwrite a collaborator without ever showing a conflict. It now captures the
version when the editor opens, which is what optimistic concurrency actually
means. The two-browser conflict test is what caught this.

Snapshots also now arrive from two places (the REST fetch and the `sync` frame),
so `snapshotLoaded` ignores any snapshot older than the state it already has — a
slower request started earlier must never move the board backwards.

---

## 8. Security posture

The actor is bound **once**, at the handshake, and the server stamps it onto
everything that connection produces.

A client cannot impersonate anyone by putting a different id in a message,
because **no client message has an actor field**. The three permitted shapes are
listed in §2; anything else is rejected before it reaches the rest of the system.

Also enforced on every inbound frame: a size cap (8 KiB, checked before the JSON
decoder sees it), a per-connection rate limit (60 messages / 10s — far above
normal use, since clients only ping and occasionally resync), and Pydantic
validation of the parsed shape. Board ids, card ids and anchors are typed as
UUIDs and validated by the same schemas the REST layer uses.

Authentication is a **server-issued guest session**: a signed, HTTP-only cookie
holding an id the client never chooses and cannot forge without the secret. It
is not an account system - there are no passwords and no users table - but it
does mean a visitor can only ever act as themselves. Replacing it with real
accounts is a change to `app/core/session.py` and `app/api/deps.py`; nothing
downstream reads identity from anywhere else.
