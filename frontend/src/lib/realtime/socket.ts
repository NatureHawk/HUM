/**
 * The board WebSocket client.
 *
 * A plain class rather than a hook, for one reason: React in development
 * mounts effects twice, and a socket opened directly in an effect body tends
 * to become two sockets, two sets of listeners, and every event applied twice.
 * Keeping the connection in an object with explicit `connect()` / `close()`
 * means there is exactly one owner and one place that can open a socket.
 *
 * Responsibilities, deliberately narrow:
 *   - hold one connection, and only one
 *   - reconnect with bounded backoff
 *   - heartbeat, which doubles as the presence keepalive
 *   - parse frames and hand them to callbacks
 *
 * It does NOT own board state. Frames go to the caller, which feeds board
 * events into the existing reducer. There is no second state system here.
 */

import { wsOrigin } from "@/lib/config";
import type { ClientMessage, ConnectionStatus, ServerFrame } from "@/lib/types";

/** Matches the server's presence TTL of 75s with room for a dropped ping. */
const HEARTBEAT_MS = 20_000;

const BASE_BACKOFF_MS = 500;
const MAX_BACKOFF_MS = 15_000;

export interface BoardSocketHandlers {
  onFrame: (frame: ServerFrame) => void;
  onStatusChange: (status: ConnectionStatus) => void;
}

function socketUrl(boardId: string): string {
  // Identity travels in the session cookie now, not query params — the
  // handshake authenticates the same way every other request does.
  return `${wsOrigin()}/ws/boards/${boardId}`;
}

export class BoardSocket {
  private socket: WebSocket | null = null;
  private heartbeat: ReturnType<typeof setInterval> | null = null;
  private retryTimer: ReturnType<typeof setTimeout> | null = null;
  private attempt = 0;
  private closedByUs = false;

  constructor(
    private readonly boardId: string,
    private readonly handlers: BoardSocketHandlers,
  ) {}

  connect(): void {
    // Guard against a second connect while one is already live or in flight.
    // This is what keeps React's double-invoked effects from doubling sockets.
    if (this.socket !== null) return;
    this.closedByUs = false;

    this.handlers.onStatusChange(this.attempt === 0 ? "connecting" : "reconnecting");

    let socket: WebSocket;
    try {
      socket = new WebSocket(socketUrl(this.boardId));
    } catch {
      this.scheduleReconnect();
      return;
    }
    this.socket = socket;

    socket.onopen = () => {
      this.attempt = 0;
      this.handlers.onStatusChange("open");
      this.startHeartbeat();
    };

    socket.onmessage = (event) => {
      if (typeof event.data !== "string") return;
      let frame: ServerFrame;
      try {
        frame = JSON.parse(event.data) as ServerFrame;
      } catch {
        // A frame we cannot parse is the server's problem, not a reason to
        // tear down a working connection.
        return;
      }
      this.handlers.onFrame(frame);
    };

    socket.onerror = () => {
      // `onclose` always follows, so recovery is handled in one place.
    };

    socket.onclose = () => {
      this.stopHeartbeat();
      this.socket = null;
      if (this.closedByUs) {
        this.handlers.onStatusChange("closed");
        return;
      }
      this.scheduleReconnect();
    };
  }

  send(message: ClientMessage): void {
    if (this.socket?.readyState !== WebSocket.OPEN) return;
    this.socket.send(JSON.stringify(message));
  }

  close(): void {
    this.closedByUs = true;
    this.stopHeartbeat();
    if (this.retryTimer !== null) {
      clearTimeout(this.retryTimer);
      this.retryTimer = null;
    }
    const socket = this.socket;
    this.socket = null;
    socket?.close();
    this.handlers.onStatusChange("closed");
  }

  private scheduleReconnect(): void {
    if (this.closedByUs || this.retryTimer !== null) return;
    this.handlers.onStatusChange("reconnecting");

    // Exponential backoff, capped, with jitter. The cap stops a dead server
    // from being hammered forever; the jitter stops every client that dropped
    // at the same moment from reconnecting in lockstep and doing it again.
    const backoff = Math.min(BASE_BACKOFF_MS * 2 ** this.attempt, MAX_BACKOFF_MS);
    const jittered = backoff * (0.5 + Math.random() * 0.5);
    this.attempt += 1;

    this.retryTimer = setTimeout(() => {
      this.retryTimer = null;
      this.connect();
    }, jittered);
  }

  private startHeartbeat(): void {
    this.stopHeartbeat();
    this.heartbeat = setInterval(() => this.send({ type: "ping" }), HEARTBEAT_MS);
  }

  private stopHeartbeat(): void {
    if (this.heartbeat !== null) {
      clearInterval(this.heartbeat);
      this.heartbeat = null;
    }
  }
}
