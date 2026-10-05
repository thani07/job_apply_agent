"""LinkedIn / Indeed credentials: store (encrypted), test with a real login, keep the session cookies."""

from __future__ import annotations
from typing import Any, Dict, List

from fastapi import APIRouter, Body, Depends, Path, status

from controllers.dependencies import current_user, get_repo, http_error
from services import credentials_service
from services.core_service import ConfigError, Repository
from models import CredentialStatus, CredentialsIn, Platform, TestLoginRequest, TestLoginResult

router = APIRouter(prefix="/api/credentials", tags=["Profile"])

PlatformPath = Path(..., description="linkedin or indeed")

# Resilient LinkedIn login helper
_test_linkedin_login = credentials_service._test_linkedin_login


@router.get("", response_model=List[CredentialStatus], summary="Status of the saved LinkedIn / Indeed logins")
def list_credentials(user: Dict[str, Any] = Depends(current_user), repo: Repository = Depends(get_repo)):
    """Never returns passwords or cookies - only whether they exist, when they were saved and the last test result."""
    return credentials_service.list_status(repo, user["id"])


@router.put("/{platform}", response_model=CredentialStatus, summary="Save the login for a platform")
def save_credentials(body: CredentialsIn, platform: Platform = PlatformPath,
                     user: Dict[str, Any] = Depends(current_user), repo: Repository = Depends(get_repo)):
    """The password is encrypted before it is stored. Use the /test endpoint afterwards to check it works."""
    try:
        return credentials_service.save_credentials(repo, user["id"], platform, body.username, body.password)
    except ConfigError as e:
        raise http_error(e)


@router.delete("/{platform}", status_code=status.HTTP_204_NO_CONTENT, summary="Delete the saved login and cookies")
def delete_credentials(platform: Platform = PlatformPath, user: Dict[str, Any] = Depends(current_user), repo: Repository = Depends(get_repo)):
    credentials_service.delete_credentials(repo, user["id"], platform)


@router.post("/{platform}/test", response_model=TestLoginResult, summary="Test the saved login in a real browser")
def test_credentials(body: TestLoginRequest = Body(default_factory=TestLoginRequest), platform: Platform = PlatformPath,
                     user: Dict[str, Any] = Depends(current_user), repo: Repository = Depends(get_repo)):
    """
    Opens a browser, signs in with the saved e-mail and password and reports `success`, `invalid_credentials`,
    `verification_required` (CAPTCHA / one-time code) or `error`. On success the session cookies are stored (encrypted)
    and used by the agent. If a verification step is needed, call again with `headless=false` and a
    `manual_wait_seconds` so you can complete it in the window, or import cookies with the /cookies endpoint.
    """
    try:
        return credentials_service.run_login_test(repo, user["id"], platform, body.headless, body.manual_wait_seconds)
    except Exception as e:
        raise http_error(e)


@router.post("/{platform}/cookies", response_model=CredentialStatus, summary="Import session cookies exported from your own browser")
def import_cookies(cookies: Any = Body(..., description="A cookie array (Cookie-Editor export) or a Playwright storage_state object"),
                   platform: Platform = PlatformPath, user: Dict[str, Any] = Depends(current_user), repo: Repository = Depends(get_repo)):
    """Alternative to the login test when the site insists on a CAPTCHA. Only cookies for the platform's own domain are kept."""
    try:
        return credentials_service.import_cookies(repo, user["id"], platform, cookies)
    except (ValueError, ConfigError) as e:
        raise http_error(e)
