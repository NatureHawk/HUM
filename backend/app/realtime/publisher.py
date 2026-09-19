"""The one seam between the mutation path and the realtime layer.

The service layer calls `publish_event` immediately after its transaction
commits, and never before. Publishing inside the transaction would broadcast
changes that could still roll back; publishing from the route layer would mean
a future second caller of the service silently fans out nothing.

This indirection exists so services depend on a function, not on the hub: with
no hub configured - unit tests, or a process that never serves sockets - the
call is a no-op and the mutation is unaffected.
"""

from __future__ import annotations

from app.realtime.hub import get_hub
from app.schemas.events import BoardEvent


async def publish_event(event: BoardEvent) -> None:
    """Fan a committed event out to every connected client, best-effort.

    Deliberately cannot fail a mutation: by the time this runs the write is
    already durable. If the broadcast is lost, clients simply do not hear about
    it until their next mutation exposes a revision gap and they resync.
    """
    hub = get_hub()
    if hub is None:
        return
    await hub.publish_event(event)
