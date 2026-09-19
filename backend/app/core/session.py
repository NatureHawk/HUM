"""Guest sessions.

Milestones 1-3 let the client declare who it was on every request: an `actor`
in each mutation body and an `actor_id` in the WebSocket query string. That is
fine on a laptop and indefensible on the public internet - anyone could attribute
notes to anyone, and drive somebody else's cursor.

This replaces it with the smallest thing that is actually sound: a signed,
HTTP-only cookie holding a server-issued guest id. The browser cannot read it,
cannot forge it without the secret, and never sends an identity any other way.
The display name lives inside the same signed payload, so renaming yourself is a
server round trip rather than a claim attached to each message.

Deliberately NOT built: accounts, passwords, OAuth, or a sessions table. A guest
id is disposable and worth exactly one signed cookie.
"""

from __future__ import annotations

import secrets
import uuid

from itsdangerous import BadSignature, URLSafeTimedSerializer

from app.core.config import get_settings
from app.schemas.note import Actor

COOKIE_NAME = "sb_session"
_SALT = "syncboard.guest.v1"

# Fun, neutral, and short enough to sit on a note's byline.
_ADJECTIVES = (
    "Amber",
    "Quiet",
    "Brisk",
    "Cedar",
    "Copper",
    "Dusk",
    "Ember",
    "Fern",
    "Golden",
    "Hazel",
    "Indigo",
    "Juniper",
    "Lark",
    "Marble",
    "Nimbus",
    "Ochre",
    "Pewter",
    "Russet",
    "Slate",
    "Umber",
    "Velvet",
    "Willow",
)
_NOUNS = (
    "Fox",
    "Heron",
    "Otter",
    "Falcon",
    "Marten",
    "Crane",
    "Bison",
    "Wren",
    "Ibex",
    "Lynx",
    "Shrike",
    "Tapir",
    "Vireo",
    "Badger",
    "Kestrel",
    "Osprey",
)


def _serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(get_settings().session_secret, salt=_SALT)


def generate_name(seed: str) -> str:
    """A friendly two-word name, derived from the id so it is stable.

    uuid5 rather than hash(): Python's hash is salted per process, so the same
    guest would be renamed on every restart.
    """
    digest = uuid.uuid5(uuid.NAMESPACE_OID, seed).int
    return f"{_ADJECTIVES[digest % len(_ADJECTIVES)]} {_NOUNS[(digest // 97) % len(_NOUNS)]}"


def new_actor() -> Actor:
    """Mint a fresh guest identity. The id is random and never reused."""
    actor_id = f"guest-{secrets.token_urlsafe(12)}"
    return Actor(id=actor_id, name=generate_name(actor_id))


def sign(actor: Actor) -> str:
    # dumps() is typed as returning Any because the serializer is generic.
    return str(_serializer().dumps({"id": actor.id, "name": actor.name}))


def verify(token: str) -> Actor | None:
    """Decode a cookie, or return None if it is missing, stale or forged.

    Every failure mode collapses to None on purpose: the caller's response is
    the same in each case (issue a new guest), and distinguishing "expired" from
    "tampered with" in an error message only helps an attacker.
    """
    try:
        payload = _serializer().loads(token, max_age=get_settings().session_max_age_seconds)
    except (BadSignature, ValueError):
        return None

    if not isinstance(payload, dict):
        return None
    try:
        return Actor(id=str(payload["id"]), name=str(payload["name"]))
    except (KeyError, ValueError):
        return None


def cookie_params() -> dict[str, object]:
    """Cookie flags, which differ between local development and deployment.

    In production the frontend and the API are on different origins (Vercel and
    Render, say), so the cookie has to be `SameSite=None`, which browsers only
    accept together with `Secure`. Locally there is no HTTPS, so `Secure` would
    mean the cookie is silently never stored - hence `Lax` over plain HTTP.

    `httponly` in both: no part of this app reads the session from JavaScript.
    """
    settings = get_settings()
    if settings.is_production:
        return {
            "httponly": True,
            "secure": True,
            "samesite": "none",
            "max_age": settings.session_max_age_seconds,
            "path": "/",
        }
    return {
        "httponly": True,
        "secure": False,
        "samesite": "lax",
        "max_age": settings.session_max_age_seconds,
        "path": "/",
    }
