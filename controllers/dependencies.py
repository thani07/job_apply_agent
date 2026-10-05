"""FastAPI dependencies shared by the controllers: the database, the blob store and the signed-in user."""

from __future__ import annotations
import threading
from typing import Any, Dict, Optional

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from services import auth_service
from services.apply_service import RunError
from services.core_service import AuthError, BlobStore, ConfigError, CosmosRepository, Repository, decode_access_token
from config import get_settings

bearer_scheme = HTTPBearer(auto_error=False, description="Enter the JWT access token")
oauth2_scheme = bearer_scheme
_lock = threading.Lock()
_repo: Optional[Repository] = None
_blob: Optional[BlobStore] = None


def get_repo() -> Repository:
    """The Cosmos DB repository (created on first use; the database and containers are created if missing)."""
    global _repo
    with _lock:
        if _repo is None:
            settings = get_settings()
            try:
                repo = CosmosRepository(settings.cosmos_endpoint, settings.cosmos_key, settings.cosmos_database)
                repo.ensure_containers()
            except Exception as e:  # noqa: BLE001
                raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, f"Cosmos DB is not available: {e}") from e
            _repo = repo
    return _repo


def get_blob() -> BlobStore:
    """The Blob Storage client (created on first use)."""
    global _blob
    with _lock:
        if _blob is None:
            try:
                _blob = BlobStore(get_settings().storage_connection_string)
            except Exception as e:  # noqa: BLE001
                raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, f"Blob storage is not available: {e}") from e
    return _blob


def unauthorized(detail: str = "Not authenticated") -> HTTPException:
    return HTTPException(status.HTTP_401_UNAUTHORIZED, detail, headers={"WWW-Authenticate": "Bearer"})


def verified_claims(credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme)) -> Dict[str, Any]:
    """The claims of a valid Bearer JWT. Checked BEFORE the database is touched, so a missing / bad token is always a 401."""
    if not credentials or not credentials.credentials:
        raise unauthorized()
    token = credentials.credentials
    try:
        return decode_access_token(token)
    except AuthError as e:
        raise unauthorized(str(e)) from e
    except ConfigError as e:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(e)) from e


def current_user(claims: Dict[str, Any] = Depends(verified_claims), repo: Repository = Depends(get_repo)) -> Dict[str, Any]:
    """The signed-in user. 401 if the account no longer exists."""
    try:
        user = auth_service.user_from_claims(repo, claims)
    except Exception as e:
        if "ServiceRequestError" in type(e).__name__ or "getaddrinfo" in str(e) or "Cosmos" in type(e).__name__:
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "Database connection is temporarily unavailable. Please check your network connection and retry.",
            ) from e
        raise
    if not user:
        raise unauthorized()
    return user


def http_error(e: Exception) -> HTTPException:
    """Maps a service-layer error to an HTTP error."""
    if isinstance(e, LookupError):
        return HTTPException(status.HTTP_404_NOT_FOUND, str(e))
    if isinstance(e, ConfigError):
        return HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(e))
    if isinstance(e, RunError):
        return HTTPException(e.status_code, str(e))
    if "ServiceRequestError" in type(e).__name__ or "getaddrinfo" in str(e) or "Cosmos" in type(e).__name__:
        return HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Database connection is temporarily unavailable. Please check your network connection and retry.",
        )
    return HTTPException(status.HTTP_400_BAD_REQUEST, str(e))
