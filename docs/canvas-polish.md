# SyncBoard — Palette, pen and stacking (Milestone 5)

Three complaints about a canvas that already worked:

1. Notes were nearly all near-black, so the board read as flat.
2. There was no way to draw on it.
3. Stacking felt arbitrary — the thing you were touching was not the thing on top.

None of these is an architectural problem, and this milestone deliberately did
not treat them as one. The transport, the concurrency model, the coordinate
system and the event envelope are all untouched; what changed is the object
palette, one new object type, and the rule that decides who is on top.

Read [`canvas.md`](canvas.md) first — sections 2, 3 and 4 set up everything
below.

---

## 1. The note palette

`NoteTone` was `accent | paper | ink`. `ink` — a near-black fill with pale text
— was the direct cause of complaint (1): a board of near-black notes has the
material presence of a terminal. It is gone, replaced by two more muted fills:

| tone | token | reads as |
|---|---|---|
| `accent` | `--color-accent` `#cbf03c` | the existing highlighter, unchanged |
| `paper` | `--color-paper-deep` `#e2ddcf` | the existing recess tone, unchanged |
| `peach` | `--color-note-peach` `#e2c2ac` | muted terracotta |
| `sky` | `--color-note-sky` `#c9cde3` | muted blue-lavender |

Two of the four are tokens the design system already had; only two are new, and
both were chosen at roughly `paper-deep`'s saturation and lightness so they sit
in the same material family rather than arriving as colour. All four take dark
ink text, which removed the pale-text and pale-pin special cases `ink` needed.

The set stays closed at four for the reason it was closed at three: HUM has one
accent, and a free colour field is how a coherent surface becomes confetti. The
accent in particular is meant to stay *rare* — it is a highlighter, and a board
where everything is highlighted has highlighted nothing.

### Existing notes were not left black

Migration `0003` reassigns every `ink` note to one of the three non-accent
tones, chosen by a hash of the note's own id:

```sql
UPDATE notes SET tone = CASE (('x' || substr(md5(id::text), 1, 8))::bit(32)::bigint % 3)
    WHEN 0 THEN 'paper' WHEN 1 THEN 'peach' ELSE 'sky' END
WHERE tone = 'ink';
```

Hash, not `random()`, for the same reason migration 0002's note tilt is hashed:
the same note must get the same colour on every run and after every restart. A
board that reshuffles its own colours on deploy is worse than a board that is
monochrome.

`accent` is excluded from the reassignment so the highlighter stays deliberate
rather than becoming a third of the board by accident. Notes that already had a
tone somebody chose (`accent`, `paper`) keep it — the migration only touches
the rows that were the problem.

The constraint dance is worth recording, because the obvious order fails: the
old `CHECK` only permits `('accent','paper','ink')`, so it has to come **off**
before the backfill writes `peach`, and the new one can only go **on** after
the backfill has cleared the last `ink` row. Drop, backfill, re-add.

---

## 2. The pen

A simple marker, explicitly not a drawing application: no pressure, no brushes,
no smoothing, no bezier fitting, no layers panel, no eraser. The scope test
used throughout was "could a whiteboard marker do this?"

### A stroke is data, not a picture

```
id, board_id, points, color, width, z_index, version, author_name, timestamps
```

`points` is a polyline — `[[x, y], ...]` — in **world coordinates**, stored as
`JSONB`. That single decision is what buys everything else:

- it stays put under pan and zoom, because it lives in the same space notes do
  (`canvas.md` §4), and the camera never touches it;
- it can be selected, restacked and deleted, which a flattened bitmap cannot;
- two clients render it identically from the same numbers, with no image to
  fetch or agree on.

A screenshot would have been less code and a dead end. Points-as-rows in a
second table would have been more faithful and much worse: a stroke is read and
written whole and never edited point-by-point, so a row per point buys join
cost and nothing else.

`version` is carried for symmetry with `Note` and for the day a stroke's colour
becomes editable. Nothing increments it today, and the docstring says so rather
than implying a guarantee the code does not make.

### Rendering

One `<svg>` inside the existing world-transform layer, one `<path>` per stroke,
`fill="none"` with round caps and joins. Explicitly **not** one DOM element per
point — a 400-point stroke is one path, not 400 divs. Because the svg sits
inside the transformed layer, pan and zoom move strokes and notes together by
the compositor, with no per-stroke recalculation and no re-render.

---

## 3. Drawing without writing to Postgres 300 times a second

Exactly the tiering a drag already uses (`canvas.md` §3), applied to the pen:

| tier | path | persisted | revision |
|---|---|---|---|
| the artist's own view | local state, every pointer event | no | no |
| everyone else's view | `draw` frame over WebSocket, ~30/s | **no** | **no** |
| the committed stroke | `POST /boards/{id}/strokes` on pointer-up | yes | yes |

While the pointer is down the client appends points locally on *every* event —
the person drawing must never wait for a network round trip — and streams the
polyline-so-far as a throttled ephemeral `draw` frame. The server validates it,
stamps the connection's actor, and rebroadcasts as `presence.draw`: no
revision, no write, dropped by observers the moment `settled` arrives.

One completed stroke is **one** REST call, **one** row and **one** board
revision. Nothing intermediate is ever persisted.

`stroke_id` on a `draw` frame is the drawing client's own id for that gesture,
not a database id. It exists so observers can tell one artist's in-progress
stroke from another's and know which rumour to drop; the real id is assigned by
the server at commit.

### Ephemeral is not the same as harmless

`draw` frames validate their coordinates against the world bounds exactly as
`pointer` frames do. A frame that is never persisted is still stamped with the
sender's identity and rebroadcast to everyone on the board, so a client that
can put anything in one can put anything on everybody else's canvas.

---

## 4. Z-order: the last thing you grab is on top

The old rule was "a new note lands on top, and z only changes again if you
explicitly ask". That is why stacking felt arbitrary: the object you were
touching was routinely not the object in front.

The new rule is the physical one. **Grabbing is what raises.** Every pointer-down
on a note — a click as much as a drag — and every selection of a stroke calls
the existing reorder endpoint with `to: "front"`. There is no new mechanism and
no new transport; the act that raises the object just changed.

### One cursor, not two maxima

Notes and strokes are different tables, so "the highest z" could have meant two
different numbers. Two per-table maxima are each locally correct and still
disagree about which object is actually on top where they overlap — which is
precisely the silent divergence this project exists to avoid.

So the board carries one counter:

```
boards.z_cursor   bigint, monotonic, never exposed to clients
```

Every raise — a new note, a new stroke, a grabbed note, a selected stroke —
increments that one counter under the board row lock and takes the result. Two
users grabbing different objects at the same instant are serialized by the same
lock that already serializes every other mutation, so the order they end up in
is the order the database committed them in, and every client converges on it.
The client never sends a z-index, for the same reason it never sent a rank.

"Send to back" stays local to each table's own minimum. It is a rare, explicit
command rather than a gesture, and nothing requires a note to be tucked behind
a drawing.

### What grabbing costs, and the one case that costs nothing

Grabbing writes — except when there is nothing to write. Raising an object that
is *already* the topmost one is answered as a no-op: no `z_cursor` movement, no
`z_index` change, no revision, no broadcast.

That case is not rare, which is the point. Grabbing is what raises, so the
endpoint is hit on every pointer-down, and the most common grab of all is
picking up the note you were already looking at. Without the check, each one
would tell every other client to restack something that never moved.

The check runs **on the server, under the board lock**, and asks the question
across both tables (`boards.topmost_object`): a note holding the highest
*note* z is not topmost if a stroke sits above it. It compares `(z_index, id)`,
not just the z, because paint order is that whole pair everywhere else in this
system — an object that merely ties the highest z need not be the one on top.

It deliberately does not live in the client. A browser's copy of the stack is
allowed to be stale, so a client that decided "I am already on top" could skip
a raise that was genuinely needed — and skipping a *needed* raise is a visible
bug, while performing an unnecessary one is only waste. The server has the only
copy that is never stale.

**The no-op returns no event.** `MutationResult.event` is null in this one
case. Returning a `note.reordered` event for a reorder that did not happen
would break the envelope's central promise — that an event never describes a
change that did not occur — and would consume a revision to say nothing.

### Overlap selection

Notes are absolutely positioned siblings carrying their own `z_index`, so the
browser's hit-testing already selects the visibly topmost one; the fix was to
stop the stroke layer from swallowing those clicks (`pointer-events: none` on
the svg, re-enabled on the paths themselves) and to paint strokes and notes in
one z-ordered pass, since they now share one sequence. Clicking the top object
selects the top object, and grabbing it brings it forward — which is what makes
an overlapping pile usable without a layers panel.

---

## 5. What did not change

Worth checking, because this milestone added a second object type and a fourth
colour and should not have cost anything else:

- **The event envelope.** `stroke.created` / `stroke.reordered` /
  `stroke.deleted` are the same shape as every note event, carry the same
  `revision`, `op_id` and `actor`, and travel the same Redis channel to the
  same `applyEvent`. No second realtime architecture.
- **The mutation path.** Socket frames still never mutate durable state. Every
  stroke write is a REST call into the same service layer, the same board-row
  lock and the same transaction.
- **Deployment, Redis, Postgres, auth, the coordinate system.** Untouched.
