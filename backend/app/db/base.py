"""Declarative base. Importing this module must also import every model, or
Alembic's autogenerate will not see them."""

from __future__ import annotations

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass
