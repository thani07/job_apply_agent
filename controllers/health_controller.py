"""Root redirect and the configuration health check."""

from __future__ import annotations
from fastapi import APIRouter
from fastapi.responses import RedirectResponse

from config import get_settings

router = APIRouter()


@router.get("/", include_in_schema=False)
def root() -> RedirectResponse:
    return RedirectResponse(url="/docs")


@router.get("/api/health", tags=["Health"], summary="Which required settings are configured (no secrets shown)")
def health():
    s = get_settings()
    return {
        "status": "ok",
        "jwt_secret": len(s.jwt_secret) >= 32,
        "credential_encryption_key": bool(s.credential_key),
        "cosmos": bool(s.cosmos_endpoint and s.cosmos_key),
        "blob_storage": bool(s.storage_connection_string),
    }
