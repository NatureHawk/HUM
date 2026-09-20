# SyncBoard — Spatial canvas (Milestone 3)

The board stops being a list and becomes a place. Notes sit at arbitrary
positions on a large field, overlap, and are dragged around by several people at
once.

This is a change to the *object model and the interaction*, not to the
realtime architecture. Everything in [`realtime.md`](realtime.md) survives
intact: the same envelope, the same Redis channel, the same reducer, the same
revision and resync logic.

---

## 1. Migration plan

### What exists before this milestone

| piece | shape |
|---|---|
| table | `cards(id, board_id, column_key, title, body, rank, version, author_name, …)` |
| ordering | `column_key` enum + `rank` fractional index, sorted `(rank, id)` |
| events | `card.created` / `card.updated` / `card.moved` / `card.deleted` |
| REST | `POST /boards/{id}/cards`, `PATCH /cards/{id}`, `POST /cards/{id}/move`, `DELETE /cards/{id}` |
| service | lock board → validate → write → bump revision → build event → commit → publish |
| reducer | `cards: Record<id, Card>`, columns derived by filter + sort |
| socket | `sync` / board events / `presence.*`, one `applyEvent` |

### What changes

**`column_key` and `rank` are deleted, not hidden.** They encoded "which of three
lists, and where in it" — a question the canvas does not ask. Keeping them as
invisible required fields is exactly the dead concept worth removing. They are
replaced by real spatial state:

```
x, y          double precision   world coordinates, top-left of the note
width, height double precision   world units
rotation      double precision   degrees, small
z_index       integer            stacking order, board state not DOM order
tone          varchar(16)        which of the palette's note fills
```

`cards` becomes `notes`, because the product's object is a note. Carrying
"card" in the schema while the UI, the events and the API all say "note" is the
same kind of drift.

**Nothing is destroyed.** `id`, `title`, `body`, `author_name`, `version`,
`created_at` and `updated_at` survive untouched, so existing boards keep their
content, authorship and history. Existing cards are laid out deterministically
from the Kanban position they already had:

```sql
x        = 96 + column_index * 430       -- todo | in_progress | done → 0 | 1 | 2
y        = 96 + row_within_column * 232  -- ROW_NUMBER() OVER (… ORDER BY rank, id)
z_index  = global row number within the board
rotation = a small tilt derived from md5(id), ±1.2°
```

The tilt is deterministic rather than random so it is identical for every
client and stable across re-runs. Notes that all sit perfectly square read as a
table; the point is a pinboard.

One constraint had to relax with the model: `title` was `BETWEEN 1 AND 500`
characters, and a canvas note is **created empty and typed into**. Requiring a
character up front is a Kanban assumption — a row needs a label, a sticky note
does not — so the lower bound is gone.

So a migrated board opens looking like the board you left — three loose
clusters — and is immediately draggable out of that shape. The layout is a
starting position, not a structure.

**Events are renamed and split.** `card.moved` meant "changed column or rank".
Spatial work has genuinely different operations, and collapsing them would make
the next section impossible:

| event | carries |
|---|---|
| `note.created` | the note |
| `note.updated` | the note (title/body changed) |
| `note.moved` | the note + `from_x`, `from_y` |
| `note.resized` | the note |
| `note.reordered` | the note + `from_z_index` |
| `note.deleted` | `note_id` |

Same envelope, same `revision`, same `op_id`, same discriminated union. The
frontend's `applyEvent` gains cases, not a second code path.

### What does not change

The service layer's shape (lock the board row, validate against current state,
write, bump `revision`, build the event, commit, publish after commit), Redis
fan-out, the WebSocket handshake, duplicate suppression, gap detection, resync,
optimistic rollback, and presence. Those were built to be transport- and
shape-agnostic, and this milestone is the test of that claim.

---

## 2. Field-aware concurrency

This is the substantive new correctness problem.

> Alice drags note A while Bob edits its text.

Under M2's rules both operations bumped `version` and both wrote the whole row,
so one would clobber the other and Bob would get a spurious conflict for a
change that never touched his field.

Two rules fix it.

**A mutation writes only the fields it owns.** `move` writes `x`/`y`. `resize`
writes `width`/`height`. `reorder` writes `z_index`. `update` writes
`title`/`body`. There is no endpoint that takes a whole note and writes all of
it, so a stale value in one field can never be carried in on the back of an
edit to another.

**`version` guards content only.** Only `PATCH /notes/{id}` requires
`expected_version`, and only it increments the counter. Dragging a note does not
invalidate anybody's in-progress text edit, because position is not part of what
`version` describes.

Position therefore resolves **last-writer-wins**, and that is the correct
semantics rather than a compromise: two people dragging the same note are
physically fighting over it, and the right answer is that it ends up where the
last person let go. The board-row lock still serialises the writes, so "last"
is well defined and every client converges on the same value.

Events still carry the **whole note**, which is safe for the opposite reason:
the payload is the committed server state, assembled after the write, so it is
internally consistent no matter which fields the operation touched. Whole-note
payloads keep `applyEvent` idempotent, which is what makes replay-after-resync
work.

---

## 3. Dragging without writing to Postgres 60 times a second

A drag produces a position every animation frame. Persisting those would be
thousands of writes and thousands of revisions per minute, and would make the
revision sequence meaningless.

Three tiers, by durability:

| tier | path | persisted | revision |
|---|---|---|---|
| the dragger's own view | local state, every pointer event | no | no |
| other people's view | `drag` frame over WebSocket, throttled to ~30/s | **no** | **no** |
| the committed result | `POST /notes/{id}/position` on release | yes | yes |

While a note is held, the client streams throttled `{type:"drag", note_id, x, y}`
frames over the socket. The server validates them, stamps the connection's
actor, and re-broadcasts as `presence.drag` — ephemeral, unpersisted, carrying
no `revision`, exactly like a cursor. Other clients render the note at that
position without touching their board state.

On release, one REST call commits the final position. That is the only write,
and the resulting `note.moved` event is what actually updates everyone's board
state. If the socket dropped mid-drag, nothing is lost: the release still
commits over REST.

This keeps the rule that **the socket never mutates durable state**. A `drag`
frame is not a mutation; it is a rumour about where a note is being carried, and
it expires the moment it stops arriving.

---

## 4. World coordinates vs screen coordinates

Two spaces, and every bug in a canvas comes from confusing them.

**World** is the board's own coordinate system. A note at `(1200, 640)` is at
that point forever, for everybody, at every zoom level. This is what Postgres
stores and what events carry.

**Screen** is pixels in the viewport right now. It depends entirely on where
*this* person happens to be looking.

```
screen = (world - camera) * zoom
world  = screen / zoom + camera
```

The camera — `{x, y, zoom}` — is **local to the browser and never persisted**.
Alice reading the top-left of a board and Bob zoomed into the bottom-right are
both correct, and neither should move the other's view. Writing the camera to
Postgres would be the same category error as writing presence there: it is
viewport state, not board state.

Rendering is one transformed layer, not a per-note calculation:

```css
transform: scale(var(--zoom)) translate(calc(-1px * var(--cam-x)), calc(-1px * var(--cam-y)));
transform-origin: 0 0;
```

Each note is absolutely positioned at its **world** `x`/`y` inside that layer
and never knows the camera exists. Pan and zoom change one CSS variable, the
compositor does the work, and no note re-renders.

Cursor-anchored zoom falls out of the same two formulas: record the world point
under the pointer, apply the new zoom, then move the camera so that point is
under the pointer again.

---

## 5. The HUM visual language, as actually observed

Taken from `priyanshughosh.in/design`'s own stylesheet rather than inferred:

| token | value |
|---|---|
| ground | `#f1eee5` · sheet `#fbfaf6` · recess `#e2ddcf` |
| ink | `#15150f` · soft `#55534a` · faint `#6e6b5d` |
| hairline | `rgba(21,21,15,0.16)` · soft `rgba(21,21,15,0.08)` |
| accent | **one**: `#cbf03c`, deep `#b3d621`, wash `rgba(203,240,60,0.28)` |
| radius | `3px`, large `6px` — "milled, not moulded" |
| display / sans / mono | Bricolage Grotesque / Instrument Sans / DM Mono |
| ground texture | fractal-noise turbulence at `0.13` **plus** a 26px dot grid |
| easing | rise `cubic-bezier(.16,1,.3,1)` · soft `(.4,0,.2,1)` · snap `(.7,0,.28,1)` |

And the object anatomy, which SyncBoard's notes follow:

- an **outer** element owning placement and drag, and nothing else writing its
  transform;
- an **inner** sheet carrying its own resting rotation plus a hover lean;
- a mono, wide-tracked, uppercase micro label;
- a three-dot grab handle that spreads on hover — the affordance that makes
  people believe the thing moves before they try;
- `--lift-card` at rest, `--lift-held` while held;
- notes are the one object that is **not** white paper: they carry a fill with
  ink on top, set in the sans so they read as somebody's thought rather than a
  system message. Milestone 5 widened that fill from one accent to a closed set
  of four — see [`canvas-polish.md`](canvas-polish.md) §1.

M1's palette was directionally right and specifically wrong — two earthy
accents where HUM has one highlighter, a serif display where HUM sets a
grotesque. The tokens above replace it.


---

## 6. Presence keys expire two ways

Per-connection eviction is **lazy**: `PresenceStore.members()` drops any
connection whose heartbeat is older than `PRESENCE_TTL_SECONDS` (75s), which is
how a killed browser disappears without ever sending a close frame.

That alone leaks. A board whose last connection died abruptly and which nobody
ever opens again is never read again, so its hash sits in Redis forever — an
afternoon of testing left 128 orphaned keys behind, which is how this was
found. So both keys also carry a **whole-key expiry**, refreshed on every
write and comfortably longer than the per-connection TTL.

Lazy eviction cleans the boards people come back to. The key TTL cleans the
ones they do not.
