"""Database package — owns the Declarative Base for the entire app.

Importing ``app.db`` gives you :class:`Base`. ORM models in
:mod:`app.db.models` inherit from it. Alembic's ``env.py`` imports
``Base`` from here so migrations see the same metadata the app uses.

Defining ``Base`` here (rather than in ``models.py``) avoids the
circular-import trap: ``models.py`` does ``from app.db import Base``,
and Python only ever resolves the base class through this single,
package-level definition.
"""
from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """Project-wide declarative base. All ORM models inherit from this."""


__all__ = ["Base"]