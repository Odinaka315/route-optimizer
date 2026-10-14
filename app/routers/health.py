from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from .. import __version__
from ..deps import get_session

router = APIRouter(tags=["health"])


@router.get("/health")
async def health() -> dict:
    """Liveness: the process is up. Does not touch the database."""
    return {"status": "ok", "version": __version__}


@router.get("/health/ready")
async def ready(session: AsyncSession = Depends(get_session)) -> dict:
    """Readiness: the database answers. With Neon, the first call after an idle
    period can be slow while the compute wakes up."""
    try:
        await session.execute(text("SELECT 1"))
    except Exception:  # noqa: BLE001
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Database unavailable") from None
    return {"status": "ready"}
