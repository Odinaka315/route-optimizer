"""Refresh-token lifecycle: issue, rotate, revoke.

Tokens are opaque random strings; only their SHA-256 hash is stored. Each token is
single-use: refreshing revokes it and issues a replacement in the same *family* (one
login = one family). Presenting a token that was already rotated means two parties hold
it, so the whole family is revoked and both are signed out.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import Settings
from ..models import RefreshToken, utcnow
from ..security import hash_refresh_token, new_refresh_token


class RefreshError(Exception):
    """The refresh token is unknown, expired, revoked, or was already used."""


def _aware(value: datetime) -> datetime:
    # SQLite hands back naive datetimes; everything we store is UTC.
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _new_row(user_id: uuid.UUID, family_id: uuid.UUID, settings: Settings) -> tuple[str, RefreshToken]:
    raw = new_refresh_token()
    row = RefreshToken(
        id=uuid.uuid4(),
        user_id=user_id,
        family_id=family_id,
        token_hash=hash_refresh_token(raw),
        expires_at=utcnow() + timedelta(days=settings.refresh_token_expire_days),
    )
    return raw, row


async def issue_refresh_token(
    session: AsyncSession, user_id: uuid.UUID, settings: Settings, *, family_id: uuid.UUID | None = None
) -> str:
    """Create a token (a new family unless one is given) and return the raw value.
    Does not commit."""
    raw, row = _new_row(user_id, family_id or uuid.uuid4(), settings)
    session.add(row)
    return raw


async def purge_expired(session: AsyncSession, *, user_id: uuid.UUID | None = None) -> None:
    """Delete tokens that expired more than a week ago (kept briefly so a replayed old
    token is still recognised). Does not commit."""
    stmt = delete(RefreshToken).where(RefreshToken.expires_at < utcnow() - timedelta(days=7))
    if user_id is not None:
        stmt = stmt.where(RefreshToken.user_id == user_id)
    await session.execute(stmt)


async def _revoke_family(session: AsyncSession, family_id: uuid.UUID) -> None:
    await session.execute(
        update(RefreshToken)
        .where(RefreshToken.family_id == family_id, RefreshToken.revoked_at.is_(None))
        .values(revoked_at=utcnow())
    )


async def rotate_refresh_token(
    session: AsyncSession, raw: str, settings: Settings
) -> tuple[uuid.UUID, str]:
    """Exchange a valid token for a new one. Returns (user_id, new_raw_token) and commits.

    Raises RefreshError otherwise (after committing the family revocation if the token
    was a replay)."""
    row = await session.scalar(
        select(RefreshToken).where(RefreshToken.token_hash == hash_refresh_token(raw))
    )
    if row is None:
        raise RefreshError
    if row.revoked_at is not None:
        if row.replaced_by_id is not None:  # already rotated, so someone is replaying it
            await _revoke_family(session, row.family_id)
            await session.commit()
        raise RefreshError
    if _aware(row.expires_at) <= utcnow():
        raise RefreshError

    new_raw, child = _new_row(row.user_id, row.family_id, settings)
    session.add(child)
    await session.flush()
    # Claim the old token atomically: of two simultaneous refreshes, only one wins.
    claimed = await session.execute(
        update(RefreshToken)
        .where(RefreshToken.id == row.id, RefreshToken.revoked_at.is_(None))
        .values(revoked_at=utcnow(), replaced_by_id=child.id)
    )
    if claimed.rowcount != 1:
        await session.rollback()
        raise RefreshError
    user_id = row.user_id
    await session.commit()
    return user_id, new_raw


async def revoke_session(session: AsyncSession, raw: str) -> None:
    """Log out: revoke the token's whole family. Unknown tokens are ignored. Commits."""
    row = await session.scalar(
        select(RefreshToken).where(RefreshToken.token_hash == hash_refresh_token(raw))
    )
    if row is not None:
        await _revoke_family(session, row.family_id)
        await session.commit()


async def revoke_all_for_user(session: AsyncSession, user_id: uuid.UUID) -> None:
    """Sign the user out of every device. Does not commit (callers batch it)."""
    await session.execute(
        update(RefreshToken)
        .where(RefreshToken.user_id == user_id, RefreshToken.revoked_at.is_(None))
        .values(revoked_at=utcnow())
    )
