/**
 * Where the API lives, and how to reach it over each protocol.
 *
 * ONE variable configures both: `NEXT_PUBLIC_API_URL` is the API's *origin*
 * (scheme + host + port, no path). The REST base and the WebSocket base are
 * derived from it, which is what stops the two drifting apart — a deployment
 * where HTTPS works and WSS points at localhost is the classic failure here.
 *
 * The scheme swap is the important line. `http` → `ws` and `https` → `wss`
 * both fall out of replacing the leading "http", so a TLS deployment gets a
 * TLS socket without anybody remembering to configure one. Browsers refuse a
 * plain `ws://` from an HTTPS page, so getting this wrong is not a subtle bug
 * — it is a socket that never connects in production and always connects
 * locally.
 *
 * Nothing secret belongs in a NEXT_PUBLIC_* variable: everything here ships in
 * the client bundle and is readable by anyone who opens devtools.
 */

function rawOrigin(): string {
  const configured = process.env.NEXT_PUBLIC_API_URL;
  if (configured && configured.trim().length > 0) return configured.trim();

  // A production build with no API URL is a misconfiguration, not something to
  // paper over: falling back to localhost would produce a deployed app that
  // silently talks to the visitor's own machine, fails, and gives no clue why.
  // Failing here names the exact variable to set.
  //
  // The guard also keeps the development default out of the production bundle
  // entirely - Next inlines NODE_ENV, so this branch is eliminated at build
  // time and no localhost literal ships.
  if (process.env.NODE_ENV === "production") {
    throw new Error(
      "NEXT_PUBLIC_API_URL is not set. Point it at the deployed API origin, " +
        "e.g. https://syncboard-api.onrender.com (no trailing /api).",
    );
  }
  return "http://localhost:8001";
}

/** The API origin, with any trailing slash and any accidental `/api` removed. */
export function apiOrigin(): string {
  return rawOrigin()
    .replace(/\/+$/, "")
    .replace(/\/api$/, "");
}

/** Base for REST calls, e.g. https://api.example.com/api */
export function apiBaseUrl(): string {
  return `${apiOrigin()}/api`;
}

/**
 * Base for WebSocket connections, e.g. wss://api.example.com
 *
 * An explicit `NEXT_PUBLIC_WS_URL` overrides the derivation, for the unusual
 * deployment that terminates sockets somewhere else. It should not normally be
 * set — one variable that cannot disagree with itself is the point.
 */
export function wsOrigin(): string {
  const override = process.env.NEXT_PUBLIC_WS_URL;
  if (override && override.trim().length > 0) return override.trim().replace(/\/+$/, "");
  return apiOrigin().replace(/^http/, "ws");
}

/** True when the API is on a different origin than the page. */
export function isCrossOrigin(): boolean {
  if (typeof window === "undefined") return false;
  try {
    return new URL(apiOrigin()).origin !== window.location.origin;
  } catch {
    return false;
  }
}
