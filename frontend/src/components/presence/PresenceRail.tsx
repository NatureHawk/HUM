"use client";

/**
 * Who is actually here.
 *
 * Presence is tracked per *connection* on the server, because a connection is
 * the thing that can be open or closed. But a person with three tabs open is
 * still one person, so the rail groups connections by `actor.id` and shows a
 * small count when someone has more than one. That keeps the model honest and
 * the UI sane at the same time.
 *
 * Everything rendered here comes from live socket frames. Nothing is invented:
 * with no collaborators connected, the rail shows you alone.
 */

import { useEffect, useRef, useState } from "react";
import { Button } from "@/components/ui/Button";
import { cn } from "@/lib/cn";
import { hueForActor } from "@/lib/session";
import type { Actor, ConnectionStatus, PresenceMember } from "@/lib/types";

export function Avatar({
  name,
  hue,
  title,
  dimmed,
  className,
}: {
  name: string;
  hue: number;
  title?: string;
  dimmed?: boolean;
  className?: string;
}) {
  const initials = name
    .split(/\s+/)
    .slice(0, 2)
    .map((part) => part.charAt(0).toUpperCase())
    .join("");

  return (
    <span
      title={title ?? name}
      className={cn(
        "inline-flex h-7 w-7 shrink-0 items-center justify-center rounded-[var(--radius-sm)]",
        "font-mono text-[11px] leading-none font-medium tracking-tight",
        "border border-[var(--color-border-strong)]",
        "transition-opacity duration-[var(--duration-base)] ease-[var(--ease-physical)]",
        dimmed && "opacity-40",
        className,
      )}
      style={{
        // The one legitimately computed colour: each person's accent derives
        // from their own id, so it cannot come from a fixed token.
        backgroundColor: `hsl(${hue} 34% 88%)`,
        color: `hsl(${hue} 42% 24%)`,
      }}
    >
      {initials}
    </span>
  );
}

interface Collaborator {
  actorId: string;
  name: string;
  connections: number;
  isSelf: boolean;
  editingNoteId: string | null;
}

export function groupByActor(
  members: PresenceMember[],
  selfActorId: string | null,
): Collaborator[] {
  const byActor = new Map<string, Collaborator>();

  for (const member of members) {
    const existing = byActor.get(member.actor.id);
    if (existing === undefined) {
      byActor.set(member.actor.id, {
        actorId: member.actor.id,
        name: member.actor.name,
        connections: 1,
        isSelf: member.actor.id === selfActorId,
        editingNoteId: member.editing_note_id,
      });
    } else {
      existing.connections += 1;
      // Any tab of theirs editing is enough to show the hint.
      existing.editingNoteId ??= member.editing_note_id;
    }
  }

  // You first, then everyone else in a stable order.
  return [...byActor.values()].sort((a, b) => {
    if (a.isSelf !== b.isSelf) return a.isSelf ? -1 : 1;
    return a.actorId < b.actorId ? -1 : 1;
  });
}

export function PresenceRail({
  actor,
  presence,
  connection,
  onRename,
}: {
  actor: Actor | null;
  presence: PresenceMember[];
  connection: ConnectionStatus;
  onRename: (name: string) => void;
}) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState("");
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (editing) inputRef.current?.select();
  }, [editing]);

  if (actor === null) {
    return <div className="h-7" aria-hidden="true" />;
  }

  function commit() {
    const trimmed = draft.trim();
    if (trimmed.length > 0) onRename(trimmed);
    setEditing(false);
  }

  const collaborators = groupByActor(presence, actor.id);
  const others = collaborators.filter((c) => !c.isSelf);
  const myTabs = collaborators.find((c) => c.isSelf)?.connections ?? 1;
  // While disconnected the list is last-known, not current, so dim it rather
  // than claiming people are still here.
  const stale = connection !== "open";

  return (
    <div className="flex items-center gap-2">
      <div className="flex items-center gap-2">
        <span className="relative inline-flex">
          <Avatar
            name={actor.name}
            hue={hueForActor(actor.id)}
            title={
              myTabs > 1 ? `${actor.name} (you) — ${myTabs} tabs` : `${actor.name} (you)`
            }
            dimmed={stale}
          />
          {myTabs > 1 ? <TabCount count={myTabs} /> : null}
        </span>

        {editing ? (
          <input
            ref={inputRef}
            value={draft}
            maxLength={40}
            aria-label="Your display name"
            onChange={(event) => setDraft(event.target.value)}
            onBlur={commit}
            onKeyDown={(event) => {
              if (event.key === "Enter") commit();
              if (event.key === "Escape") setEditing(false);
            }}
            className={cn(
              "w-32 rounded-[var(--radius-sm)] border border-[var(--color-border-strong)]",
              "bg-[var(--color-surface-raised)] px-1.5 py-0.5 text-sm text-[var(--color-ink)]",
            )}
          />
        ) : (
          <button
            type="button"
            onClick={() => {
              setDraft(actor.name);
              setEditing(true);
            }}
            title="Rename yourself"
            className={cn(
              "rounded-[var(--radius-sm)] px-1 py-0.5 text-sm text-[var(--color-ink-soft)]",
              "transition-colors duration-[var(--duration-fast)] ease-[var(--ease-physical)]",
              "hover:bg-[var(--color-canvas-raised)] hover:text-[var(--color-ink)]",
            )}
          >
            {actor.name} <span className="text-[var(--color-ink-faint)]">(you)</span>
          </button>
        )}
      </div>

      {others.length > 0 ? (
        <>
          <span className="h-4 w-px bg-[var(--color-border)]" aria-hidden="true" />
          <ul className="flex items-center gap-1" aria-label="Collaborators on this board">
            {others.map((person) => (
              <li key={person.actorId} className="relative">
                <Avatar
                  name={person.name}
                  hue={hueForActor(person.actorId)}
                  dimmed={stale}
                  title={
                    person.connections > 1
                      ? `${person.name} — ${person.connections} tabs`
                      : person.name
                  }
                />
                {person.connections > 1 ? <TabCount count={person.connections} /> : null}
              </li>
            ))}
          </ul>
        </>
      ) : null}

      <span className="h-4 w-px bg-[var(--color-border)]" aria-hidden="true" />
      <ConnectionDot connection={connection} others={others.length} />

      {editing ? null : (
        <Button
          variant="ghost"
          size="sm"
          className="sr-only focus-visible:not-sr-only"
          onClick={() => {
            setDraft(actor.name);
            setEditing(true);
          }}
        >
          Rename yourself
        </Button>
      )}
    </div>
  );
}

/** How many tabs one person has open. Presence is per connection; this is
 *  where that detail surfaces without splitting them into separate people. */
function TabCount({ count }: { count: number }) {
  return (
    <span
      aria-hidden="true"
      className={cn(
        "absolute -right-1 -bottom-1 flex h-3.5 min-w-3.5 items-center justify-center",
        "rounded-[var(--radius-xs)] border border-[var(--color-border-strong)]",
        "bg-[var(--color-surface-raised)] px-[3px] font-mono text-[9px]",
        "leading-none text-[var(--color-ink-soft)]",
      )}
    >
      {count}
    </span>
  );
}

/** Reports the real socket state. It never claims "live" when it is not. */
function ConnectionDot({
  connection,
  others,
}: {
  connection: ConnectionStatus;
  others: number;
}) {
  const label: Record<ConnectionStatus, string> = {
    connecting: "connecting",
    open: others === 0 ? "live · just you" : `live · ${others + 1} here`,
    reconnecting: "reconnecting",
    closed: "offline",
  };

  const dot: Record<ConnectionStatus, string> = {
    connecting: "bg-[var(--color-ink-faint)]",
    open: "bg-[var(--color-accent-teal)]",
    reconnecting: "bg-[var(--color-accent-amber)]",
    closed: "bg-[var(--color-danger)]",
  };

  return (
    <span
      className="flex items-center gap-1.5 font-mono text-[11px] tracking-tight text-[var(--color-ink-faint)]"
      role="status"
      aria-live="polite"
    >
      <span
        aria-hidden="true"
        className={cn("h-1.5 w-1.5 rounded-full", dot[connection])}
      />
      {label[connection]}
    </span>
  );
}
