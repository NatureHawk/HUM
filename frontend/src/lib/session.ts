"use client";

/**
 * The guest session.
 *
 * Identity used to be generated in the browser and stored in localStorage,
 * which meant the client decided who it was. It now comes from the server: one
 * call to `POST /api/session` establishes a signed, HTTP-only cookie, and every
 * request and WebSocket handshake after that carries it automatically.
 *
 * Consequences worth knowing:
 *   - this code cannot read the cookie, and does not need to
 *   - every fetch must use `credentials: "include"`, or the cookie is dropped
 *     on a cross-origin deployment and the server sees an anonymous caller
 *   - renaming yourself is a server round trip, because the name is inside the
 *     signed payload rather than asserted per-message
 */

import { useCallback, useEffect, useState } from "react";
import { apiBaseUrl } from "@/lib/config";
import type { Actor } from "@/lib/types";

interface SessionResponse {
  actor: Actor;
}

export async function establishSession(): Promise<Actor> {
  const response = await fetch(`${apiBaseUrl()}/session`, {
    method: "POST",
    credentials: "include",
  });
  if (!response.ok) throw new Error(`session failed: ${response.status}`);
  return ((await response.json()) as SessionResponse).actor;
}

export async function renameSession(name: string): Promise<Actor> {
  const response = await fetch(`${apiBaseUrl()}/session`, {
    method: "PATCH",
    credentials: "include",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name }),
  });
  if (!response.ok) throw new Error(`rename failed: ${response.status}`);
  return ((await response.json()) as SessionResponse).actor;
}

/** Deterministic hue from an actor id, so a person looks the same to everyone. */
export function hueForActor(actorId: string): number {
  let hash = 0;
  for (let i = 0; i < actorId.length; i += 1) {
    hash = (hash * 31 + actorId.charCodeAt(i)) % 360;
  }
  return hash;
}

export interface UseSessionResult {
  actor: Actor | null;
  /** True once the handshake has failed - the API is unreachable or refusing. */
  failed: boolean;
  rename: (name: string) => void;
}

export function useSession(): UseSessionResult {
  const [actor, setActor] = useState<Actor | null>(null);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    let cancelled = false;
    // Every setState below runs in a promise callback, not in the effect
    // body, so there is no synchronous cascade to guard against.
    void establishSession()
      .then((established) => {
        if (!cancelled) setActor(established);
      })
      .catch(() => {
        if (!cancelled) setFailed(true);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const rename = useCallback((name: string) => {
    const trimmed = name.trim();
    if (trimmed.length === 0) return;
    // Optimistic, then corrected by the server's answer. A rename that fails
    // silently reverts rather than leaving a name the server disagrees with.
    setActor((current) => (current ? { ...current, name: trimmed } : current));
    void renameSession(trimmed)
      .then(setActor)
      .catch(() => void establishSession().then(setActor).catch(() => setFailed(true)));
  }, []);

  return { actor, failed, rename };
}
