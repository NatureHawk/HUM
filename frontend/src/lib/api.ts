/**
 * Typed fetch client for the SyncBoard REST API.
 *
 * Every failure the caller is expected to handle arrives as an `ApiError`
 * with a stable `code` — either the domain error the backend sends
 * (`{code, message, details}`, see backend/app/core/errors.py) or a 422
 * FastAPI validation response (`{detail: [...]}`), normalised into the same
 * shape so callers never branch on transport format.
 */

import { apiBaseUrl } from "@/lib/config";
import type {
  Board,
  BoardCreate,
  BoardSnapshot,
  BoardSummary,
  MutationResult,
  NoteContentUpdate,
  NoteCreate,
  NoteDelete,
  NoteMove,
  NoteReorder,
  NoteResize,
  StrokeCreate,
  StrokeDelete,
  StrokeReorder,
} from "@/lib/types";
import type { ApiErrorBody, ApiErrorCode } from "@/lib/types";

export class ApiError extends Error {
  readonly code: ApiErrorCode;
  readonly details: unknown;
  readonly status: number;

  constructor(params: { code: ApiErrorCode; message: string; details: unknown; status: number }) {
    super(params.message);
    this.name = "ApiError";
    this.code = params.code;
    this.details = params.details;
    this.status = params.status;
  }
}

interface FastApiValidationError {
  detail: Array<{ loc: (string | number)[]; msg: string; type: string }>;
}

function isDomainErrorBody(value: unknown): value is ApiErrorBody {
  return (
    typeof value === "object" &&
    value !== null &&
    "code" in value &&
    "message" in value &&
    typeof (value as Record<string, unknown>).code === "string" &&
    typeof (value as Record<string, unknown>).message === "string"
  );
}

function isValidationErrorBody(value: unknown): value is FastApiValidationError {
  return (
    typeof value === "object" &&
    value !== null &&
    "detail" in value &&
    Array.isArray((value as Record<string, unknown>).detail)
  );
}

async function toApiError(response: Response): Promise<ApiError> {
  let body: unknown = null;
  try {
    body = await response.json();
  } catch {
    // No JSON body at all — fall through to a generic error below.
  }

  if (isDomainErrorBody(body)) {
    return new ApiError({
      code: body.code,
      message: body.message,
      details: body.details,
      status: response.status,
    });
  }

  if (isValidationErrorBody(body)) {
    const first = body.detail[0];
    const field = first?.loc?.slice(1).join(".") || "request";
    return new ApiError({
      code: "validation_error",
      message: first ? `${field}: ${first.msg}` : "The request was invalid.",
      details: body.detail,
      status: response.status,
    });
  }

  return new ApiError({
    code: "unknown_error",
    message: `Request failed with status ${response.status}.`,
    details: body,
    status: response.status,
  });
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let response: Response;
  try {
    response = await fetch(`${apiBaseUrl()}${path}`, {
      ...init,
      // The session lives in an HTTP-only cookie. Without this, a
      // cross-origin deployment drops it and every call becomes anonymous.
      credentials: "include",
      headers: {
        "Content-Type": "application/json",
        ...init?.headers,
      },
    });
  } catch (cause) {
    throw new ApiError({
      code: "unknown_error",
      message: "Could not reach the server. Check your connection and try again.",
      details: cause,
      status: 0,
    });
  }

  if (!response.ok) {
    throw await toApiError(response);
  }

  if (response.status === 204) {
    return undefined as T;
  }

  return (await response.json()) as T;
}

export const api = {
  listBoards(): Promise<BoardSummary[]> {
    return request<BoardSummary[]>("/boards");
  },

  createBoard(payload: BoardCreate): Promise<Board> {
    return request<Board>("/boards", {
      method: "POST",
      body: JSON.stringify(payload),
    });
  },

  getBoard(boardId: string): Promise<BoardSnapshot> {
    return request<BoardSnapshot>(`/boards/${boardId}`);
  },

  deleteBoard(boardId: string): Promise<void> {
    return request<void>(`/boards/${boardId}`, { method: "DELETE" });
  },

  createNote(boardId: string, payload: NoteCreate): Promise<MutationResult> {
    return request<MutationResult>(`/boards/${boardId}/notes`, {
      method: "POST",
      body: JSON.stringify(payload),
    });
  },

  /** Text or tone. The only note call that carries `expected_version`. */
  updateNote(noteId: string, payload: NoteContentUpdate): Promise<MutationResult> {
    return request<MutationResult>(`/notes/${noteId}`, {
      method: "PATCH",
      body: JSON.stringify(payload),
    });
  },

  /**
   * Commit a position. Called once, on pointer release — the intermediate
   * positions travel over the socket as ephemeral drag frames and are never
   * written down. See docs/canvas.md section 3.
   */
  moveNote(noteId: string, payload: NoteMove): Promise<MutationResult> {
    return request<MutationResult>(`/notes/${noteId}/position`, {
      method: "POST",
      body: JSON.stringify(payload),
    });
  },

  resizeNote(noteId: string, payload: NoteResize): Promise<MutationResult> {
    return request<MutationResult>(`/notes/${noteId}/size`, {
      method: "POST",
      body: JSON.stringify(payload),
    });
  },

  reorderNote(noteId: string, payload: NoteReorder): Promise<MutationResult> {
    return request<MutationResult>(`/notes/${noteId}/order`, {
      method: "POST",
      body: JSON.stringify(payload),
    });
  },

  deleteNote(noteId: string, payload: NoteDelete): Promise<MutationResult> {
    return request<MutationResult>(`/notes/${noteId}`, {
      method: "DELETE",
      body: JSON.stringify(payload),
    });
  },

  /**
   * Commit a finished stroke. Called once, on pointer release — the polyline
   * travels over the socket as ephemeral `draw` frames while it is being
   * drawn, and is never written down until the gesture ends. Mirrors why
   * `moveNote` only fires on release rather than every frame.
   */
  createStroke(boardId: string, payload: StrokeCreate): Promise<MutationResult> {
    return request<MutationResult>(`/boards/${boardId}/strokes`, {
      method: "POST",
      body: JSON.stringify(payload),
    });
  },

  reorderStroke(strokeId: string, payload: StrokeReorder): Promise<MutationResult> {
    return request<MutationResult>(`/strokes/${strokeId}/order`, {
      method: "POST",
      body: JSON.stringify(payload),
    });
  },

  deleteStroke(strokeId: string, payload: StrokeDelete): Promise<MutationResult> {
    return request<MutationResult>(`/strokes/${strokeId}`, {
      method: "DELETE",
      body: JSON.stringify(payload),
    });
  },
};
