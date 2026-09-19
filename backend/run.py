"""Development entrypoint.

Exists because of one real platform constraint, not for convenience.

psycopg's async driver cannot run on Windows' ProactorEventLoop - it raises
`InterfaceError` on the first connection and needs a selector-based loop. Since
uvicorn 0.36 the loop is chosen by a *factory* rather than by the global asyncio
policy, and `uvicorn/loops/asyncio.py` hardcodes `ProactorEventLoop` on win32
whenever the server is not running in a subprocess. So neither
`asyncio.set_event_loop_policy(...)` nor `--loop asyncio` has any effect here;
the factory itself has to be replaced.

Overriding `Server.run` is the narrow way to do that. It covers both paths,
because uvicorn's reloader also uses `server.run` as its subprocess target.

Trade-off worth knowing: a selector loop on Windows uses `select()`, which caps
out around 512 sockets. Irrelevant for local development, and irrelevant on
Linux where the default loop is already selector-based - but a reason not to
load-test the Milestone 2 WebSocket fan-out on Windows.

    python run.py             # reload enabled
    python run.py --no-reload
"""

from __future__ import annotations

import asyncio
import os
import sys
from collections.abc import Callable

import uvicorn
from uvicorn.supervisors import ChangeReload

# None means "let asyncio choose", which is correct everywhere except Windows.
LOOP_FACTORY: Callable[[], asyncio.AbstractEventLoop] | None = (
    asyncio.SelectorEventLoop if sys.platform == "win32" else None
)


class SelectorLoopServer(uvicorn.Server):
    """uvicorn's Server, but running on a loop psycopg can actually use."""

    def run(self, sockets: list | None = None) -> None:  # type: ignore[type-arg]
        asyncio.run(self.serve(sockets=sockets), loop_factory=LOOP_FACTORY)


def main() -> None:
    config = uvicorn.Config(
        "app.main:app",
        host=os.environ.get("API_HOST", "127.0.0.1"),
        # 8001 rather than 8000: another service already listens on 8000 on this
        # machine.
        port=int(os.environ.get("API_PORT", "8001")),
        reload="--no-reload" not in sys.argv,
        reload_dirs=["app"],
        log_level=os.environ.get("LOG_LEVEL", "info"),
    )
    server = SelectorLoopServer(config)

    if config.should_reload:
        # Bind in the parent so the watcher owns the socket across restarts.
        ChangeReload(config, target=server.run, sockets=[config.bind_socket()]).run()
    else:
        server.run()


if __name__ == "__main__":
    main()
