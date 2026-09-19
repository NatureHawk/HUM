"""Board table."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import BigInteger, DateTime, String, func
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base

if TYPE_CHECKING:
    # Import-time cycle: Note imports Board for its relationship. Only the
    # type checker needs this name; SQLAlchemy resolves the string at mapper
    # configuration time.
    from app.models.note import Note


class Board(Base):
    __tablename__ = "boards"

    # See the note in models/card.py - required for async serialisation safety.
    # RUF012 would ask for a ClassVar annotation here, but SQLAlchemy
    # declares __mapper_args__ as an instance attribute and mypy rejects
    # overriding it with a class variable. SQLAlchemy's convention wins.
    __mapper_args__ = {"eager_defaults": True}  # noqa: RUF012

    id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    title: Mapped[str] = mapped_column(String(120), nullable=False)

    # Monotonic, gapless mutation counter for this board.
    #
    # Every mutation increments it inside the same transaction as the data
    # change, so it is simultaneously: the sequence number stamped on each
    # broadcast event, the client's dedupe key, its gap detector, and its resync
    # cursor. A Postgres SEQUENCE would be the obvious alternative but sequences
    # are deliberately non-transactional - they leave gaps on rollback, and a
    # gap is exactly the signal we use to mean "you missed an event".
    revision: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, server_default="0")

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    notes: Mapped[list[Note]] = relationship(
        back_populates="board",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
