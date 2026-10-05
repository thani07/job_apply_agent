"""Sign up, log in and identify the current user (JWT)."""

from __future__ import annotations
from typing import Any, Dict

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordRequestForm

from controllers.dependencies import current_user, get_repo, unauthorized
from services import auth_service
from models import LoginRequest, RefreshTokenRequest, SignupRequest, TokenResponse, UserOut
from services.core_service import AuthError, ConfigError, EmailAlreadyExists, Repository

router = APIRouter(prefix="/api/auth", tags=["Authentication"])


def _token_for(user: Dict[str, Any]) -> TokenResponse:
    try:
        return auth_service.issue_token(user)
    except ConfigError as e:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(e)) from e


def _authenticate(repo: Repository, email: str, password: str) -> Dict[str, Any]:
    try:
        return auth_service.authenticate(repo, email, password)
    except AuthError as e:
        raise unauthorized(str(e)) from e


@router.post("/signup", response_model=TokenResponse, status_code=status.HTTP_201_CREATED, summary="Create an account")
def signup(body: SignupRequest, repo: Repository = Depends(get_repo)):
    """Creates the user (with a new uuid) and returns a JWT so they are signed in straight away."""
    try:
        user = auth_service.register_user(repo, body.email, body.full_name, body.password)
    except EmailAlreadyExists:
        raise HTTPException(status.HTTP_409_CONFLICT, "An account with this email already exists")
    return _token_for(user)


@router.post("/login", response_model=TokenResponse, summary="Log in with email and password")
def login(body: LoginRequest, repo: Repository = Depends(get_repo)):
    return _token_for(_authenticate(repo, body.email, body.password))


@router.post("/token", response_model=TokenResponse, include_in_schema=False)
def login_form(form: OAuth2PasswordRequestForm = Depends(), repo: Repository = Depends(get_repo)):
    """Form-encoded login used by the Swagger 'Authorize' button (username = your email)."""
    return _token_for(_authenticate(repo, form.username, form.password))


@router.post("/refresh", response_model=TokenResponse, summary="Refresh access token")
def refresh_token(body: RefreshTokenRequest, repo: Repository = Depends(get_repo)):
    """Exchange a valid refresh token for a new access token and rotated refresh token."""
    try:
        return auth_service.refresh_access_token(repo, body.refresh_token)
    except AuthError as e:
        raise unauthorized(str(e)) from e


@router.get("/me", response_model=UserOut, summary="The signed-in user")
def me(user: Dict[str, Any] = Depends(current_user)):
    return auth_service.user_out(user)
