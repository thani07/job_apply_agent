"""Accounts: sign up, log in and access tokens."""

from __future__ import annotations
from typing import Any, Dict, Optional
from services.core_service import (
    AuthError, DUMMY_HASH, Repository, create_access_token, create_refresh_token,
    decode_refresh_token, hash_password, new_id, utc_now, verify_password,
)
from models import TokenResponse, UserOut


def user_out(user: Dict[str, Any]) -> UserOut:
    return UserOut(id=user["id"], email=user["email"], full_name=user["full_name"], created_at=user["created_at"])


def issue_token(user: Dict[str, Any], refresh_token: Optional[str] = None) -> TokenResponse:
    """A signed JWT access token and refresh token for the user. Raises ConfigError when JWT_SECRET is not configured."""
    access_data = create_access_token(user["id"], user["email"])
    r_token = refresh_token or create_refresh_token(user["id"], user["email"])
    return TokenResponse(**access_data, refresh_token=r_token, user=user_out(user))


def refresh_access_token(repo: Repository, refresh_token: str) -> TokenResponse:
    """Validates the refresh token and issues a new access token (and rotates the refresh token)."""
    claims = decode_refresh_token(refresh_token)
    user = repo.get_user(claims["sub"], claims["email"])
    if not user:
        raise AuthError("User no longer exists")
    new_refresh_token = create_refresh_token(user["id"], user["email"])
    return issue_token(user, refresh_token=new_refresh_token)


def register_user(repo: Repository, email: str, full_name: str, password: str) -> Dict[str, Any]:
    """Creates the user with a new uuid. Raises EmailAlreadyExists if the address is taken."""
    user = {"id": new_id(), "email": email.strip().lower(), "full_name": full_name.strip(),
            "password_hash": hash_password(password), "created_at": utc_now()}
    repo.create_user(user)
    return user


def authenticate(repo: Repository, email: str, password: str) -> Dict[str, Any]:
    """The user for a correct e-mail + password. Raises AuthError otherwise (with the same message for both failures)."""
    user = repo.get_user_by_email(email.strip().lower())
    # Compare against a dummy hash for unknown e-mails so the response time does not reveal which accounts exist.
    if not verify_password(password, user["password_hash"] if user else DUMMY_HASH) or not user:
        raise AuthError("Invalid email or password")
    return user


def user_from_claims(repo: Repository, claims: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """The stored user a verified token belongs to, or None if the account no longer exists."""
    return repo.get_user(claims["sub"], claims["email"])
