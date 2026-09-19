"use client";

/**
 * The lobby: every board that exists, and a way to start another.
 *
 * All data here is real — it comes from GET /boards. Nothing is seeded, and an
 * empty account shows an empty state rather than sample boards.
 */

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";
import { PresenceRail } from "@/components/presence/PresenceRail";
import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { EmptyState } from "@/components/ui/EmptyState";
import { Spinner } from "@/components/ui/Spinner";
import { Surface } from "@/components/ui/Surface";
import { TextField } from "@/components/ui/TextField";
import { ApiError, api } from "@/lib/api";
import { cn } from "@/lib/cn";
import { useSession } from "@/lib/session";
import type { BoardSummary } from "@/lib/types";

type LoadState = "loading" | "ready" | "error";

export default function LobbyPage() {
  const { actor, failed, rename } = useSession();
  const [boards, setBoards] = useState<BoardSummary[]>([]);
  const [status, setStatus] = useState<LoadState>("loading");
  const [error, setError] = useState<string | null>(null);
  const [title, setTitle] = useState("");
  const [creating, setCreating] = useState(false);

  const load = useCallback(async () => {
    // No synchronous setState before the await: `load` runs from an effect,
    // and `status` already starts as "loading".
    try {
      setBoards(await api.listBoards());
      setStatus("ready");
      setError(null);
    } catch (err) {
      setStatus("error");
      setError(
        err instanceof ApiError ? err.message : "Could not reach the API. Is the backend running?",
      );
    }
  }, []);

  useEffect(() => {
    // Fetching on mount is the documented escape hatch for an effect, and
    // every setState inside load() happens after the await.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    void load();
  }, [load]);

  async function createBoard(event: React.FormEvent) {
    event.preventDefault();
    const trimmed = title.trim();
    if (trimmed.length === 0 || creating) return;
    setCreating(true);
    try {
      await api.createBoard({ title: trimmed });
      setTitle("");
      await load();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not create that board.");
    } finally {
      setCreating(false);
    }
  }

  return (
    <main className="mx-auto max-w-[1000px] px-4 py-10 sm:px-8 sm:py-16">
      <div className="flex justify-end">
        {/* The lobby holds no socket, so there is no presence to show here
            and the indicator says so rather than inventing one. */}
        <PresenceRail actor={actor} presence={[]} connection="closed" onRename={rename} />
      </div>

      {/* Deliberately off-axis: the wordmark sits left, the description
          indented under it, rather than a centred hero. */}
      <header className="mt-6">
        <h1 className="font-display text-6xl leading-[0.95] tracking-tight text-[var(--color-ink)] sm:text-7xl">
          SyncBoard
        </h1>
        <p className="mt-3 max-w-md pl-1 text-[15px] leading-relaxed text-[var(--color-ink-soft)]">
          A shared canvas. Leave ideas anywhere on it; everyone watching sees them land.
        </p>
        {failed ? (
          <p className="mt-3 max-w-md rounded-[var(--radius-md)] border border-[var(--color-danger)]/30 bg-[var(--color-danger-soft)] px-3 py-2 text-[13px] text-[var(--color-danger)]">
            Could not reach the server to start your session. Rename and presence will not work
            until it is reachable.
          </p>
        ) : null}
      </header>

      <form onSubmit={createBoard} className="mt-10 flex flex-wrap items-end gap-2">
        <div className="min-w-0 flex-1">
          <TextField
            id="board-title"
            label="New board"
            value={title}
            maxLength={120}
            placeholder="Name it something you'll recognise"
            onChange={(event) => setTitle(event.target.value)}
          />
        </div>
        <Button type="submit" disabled={title.trim().length === 0 || creating}>
          {creating ? "Creating…" : "Create board"}
        </Button>
      </form>

      {error !== null ? (
        <p className="mt-4 rounded-[var(--radius-md)] border border-[var(--color-danger)]/30 bg-[var(--color-danger-soft)] px-3 py-2 text-[13px] text-[var(--color-danger)]">
          {error}
        </p>
      ) : null}

      <section className="mt-12">
        {status === "loading" ? (
          <div className="flex justify-center py-16">
            <Spinner />
          </div>
        ) : boards.length === 0 ? (
          <EmptyState
            title="No boards yet."
            description="Make the first one. It opens as an empty field — nothing is seeded, nothing is pretend."
          />
        ) : (
          <ul className="flex flex-col gap-2">
            {boards.map((board) => (
              <li key={board.id}>
                <Link href={`/boards/${board.id}`} className="block">
                  <Surface
                    elevation="flat"
                    padding="none"
                    className={cn(
                      "flex items-center gap-4 px-4 py-3",
                      "transition-[transform,border-color,box-shadow]",
                      "duration-[var(--duration-base)] ease-[var(--ease-physical)]",
                      "hover:-translate-y-px hover:border-[var(--color-border-strong)]",
                      "hover:shadow-[var(--shadow-raised)]",
                    )}
                  >
                    <div className="min-w-0 flex-1">
                      <h2 className="truncate font-display text-2xl leading-tight tracking-tight text-[var(--color-ink)]">
                        {board.title}
                      </h2>
                      <p className="mt-0.5 font-mono text-[11px] text-[var(--color-ink-faint)]">
                        created {new Date(board.created_at).toLocaleDateString()}
                      </p>
                    </div>
                    <Badge tone="neutral">
                      {board.note_count} {board.note_count === 1 ? "note" : "notes"}
                    </Badge>
                    <Badge tone="neutral" title="Mutations applied to this board">
                      rev {board.revision}
                    </Badge>
                  </Surface>
                </Link>
              </li>
            ))}
          </ul>
        )}
      </section>

      <footer className="mt-16 border-t border-[var(--color-border)] pt-6">
        <p className="max-w-2xl font-mono text-[11px] leading-relaxed text-[var(--color-ink-faint)]">
          Every board is a shared spatial canvas. Notes are written to Postgres, ordered by the
          server and broadcast over Redis, so two windows on the same board stay in step without
          either one reloading.
        </p>
      </footer>
    </main>
  );
}
