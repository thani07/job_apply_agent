"""Per-user LinkedIn / Indeed logins. Passwords and session cookies are encrypted before they are stored; API responses never contain them."""

from __future__ import annotations
from typing import Any, Dict, List, Optional
from services.core_service import Repository, decrypt_json, decrypt_text, encrypt_json, encrypt_text, run_in_worker_thread, utc_now
from services.platform_login_service import test_login, test_saved_cookies, _test_linkedin_login
from models import CredentialStatus, TestLoginResult


PLATFORM_DOMAINS = {"linkedin": "linkedin.com", "indeed": "indeed.com"}


_COOKIE_KEYS = {"name", "value", "domain", "path", "expires", "httpOnly", "secure", "sameSite"}


def normalize_cookies(raw: Any, platform: str) -> List[Dict[str, Any]]:
    """
    Accepts a cookie array (Cookie-Editor / EditThisCookie export) or a Playwright storage_state dict and returns
    clean cookies for Chromium. Cookies that do not belong to the platform's domain are dropped.
    """
    if isinstance(raw, dict):
        raw = raw.get("cookies", [])
    if not isinstance(raw, list):
        raise ValueError("Cookies must be a JSON array of cookies, or an object with a 'cookies' array.")

    domain = PLATFORM_DOMAINS[platform]
    cleaned: List[Dict[str, Any]] = []
    for cookie in raw:
        if not isinstance(cookie, dict) or not cookie.get("name") or "value" not in cookie:
            continue
        item = {k: v for k, v in cookie.items() if k in _COOKIE_KEYS}
        if domain not in str(item.get("domain", "")).lower() and "url" not in cookie:
            continue
        if item.get("expires") in (0, 0.0, -1, -1.0, None):
            item.pop("expires", None)
        same_site = item.get("sameSite")
        if isinstance(same_site, str):
            normalised = {"no_restriction": "None", "unspecified": "Lax"}.get(same_site.lower(), same_site.capitalize())
            if normalised in ("None", "Lax", "Strict"):
                item["sameSite"] = normalised
            else:
                item.pop("sameSite", None)
        item.setdefault("path", "/")
        cleaned.append(item)
    if not cleaned:
        raise ValueError(f"No {domain} cookies found in the data you sent.")
    return cleaned


def status_of(platform: str, doc: Optional[Dict[str, Any]]) -> CredentialStatus:
    if not doc:
        return CredentialStatus(platform=platform, configured=False)
    has_creds = bool(doc.get("password_enc"))
    has_cookies = bool(doc.get("cookies_enc"))
    return CredentialStatus(
        platform=platform,
        configured=has_creds or has_cookies,
        username=doc.get("username"),
        cookies_saved=has_cookies,
        cookie_count=doc.get("cookie_count", 0),
        cookies_saved_at=doc.get("cookies_saved_at"),
        last_tested_at=doc.get("last_tested_at"),
        last_test_status=doc.get("last_test_status"),
        last_test_message=doc.get("last_test_message"),
    )


def list_status(repo: Repository, user_id: str) -> List[CredentialStatus]:
    docs = {d["platform"]: d for d in repo.list_credentials(user_id)}
    return [status_of(p, docs.get(p)) for p in PLATFORM_DOMAINS]


def save_credentials(repo: Repository, user_id: str, platform: str, username: str, password: str) -> CredentialStatus:
    existing = repo.get_credentials(user_id, platform) or {}
    doc = {**existing, "id": platform, "user_id": user_id, "platform": platform, "username": username.strip(),
           "password_enc": encrypt_text(password), "updated_at": utc_now()}
    if existing.get("username") != doc["username"]:
        # different account: the old session and test result no longer apply
        for key in ("cookies_enc", "cookie_count", "cookies_saved_at", "last_tested_at", "last_test_status", "last_test_message"):
            doc.pop(key, None)
    return status_of(platform, repo.save_credentials(doc))


def delete_credentials(repo: Repository, user_id: str, platform: str) -> None:
    repo.delete_credentials(user_id, platform)


def import_cookies(repo: Repository, user_id: str, platform: str, raw: Any) -> CredentialStatus:
    cookies = normalize_cookies(raw, platform)
    doc = repo.get_credentials(user_id, platform) or {"id": platform, "user_id": user_id, "platform": platform}
    doc.update(
        cookies_enc=encrypt_json(cookies),
        cookie_count=len(cookies),
        cookies_saved_at=utc_now(),
        last_tested_at=utc_now(),
        last_test_status="success",
        last_test_message=f"Imported {len(cookies)} session cookies successfully.",
        updated_at=utc_now(),
    )
    return status_of(platform, repo.save_credentials(doc))


def get_login_material(repo: Repository, user_id: str, platform: str) -> Optional[Dict[str, Any]]:
    """Decrypted username / password / cookies for the agent, or None if the user has nothing stored for the platform."""
    doc = repo.get_credentials(user_id, platform)
    if not doc:
        return None
    return {
        "username": doc.get("username"),
        "password": decrypt_text(doc["password_enc"]) if doc.get("password_enc") else None,
        "cookies": decrypt_json(doc["cookies_enc"]) if doc.get("cookies_enc") else None,
    }


def run_login_test(repo: Repository, user_id: str, platform: str, headless: bool, manual_wait_seconds: int) -> TestLoginResult:
    """Logs in for real with stored credentials, or tests saved cookies. On success the session is confirmed."""
    doc = repo.get_credentials(user_id, platform)
    has_creds = bool(doc and doc.get("password_enc") and doc.get("username"))
    has_cookies = bool(doc and doc.get("cookies_enc"))

    if not has_creds and not has_cookies:
        raise LookupError(f"No {platform} credentials or cookies saved yet. Save credentials or import cookies first.")

    try:
        if has_creds:
            password = decrypt_text(doc["password_enc"])
            outcome = run_in_worker_thread(test_login, platform, doc["username"], password, headless, manual_wait_seconds,
                                           timeout=manual_wait_seconds + 150)
        else:
            cookies = decrypt_json(doc["cookies_enc"])
            outcome = run_in_worker_thread(test_saved_cookies, platform, cookies, headless,
                                           timeout=manual_wait_seconds + 150)
        status, message, cookies = outcome.status, outcome.message, outcome.cookies
    except Exception as e:  # noqa: BLE001 - browser missing, timeout, network ...
        status, message, cookies = "error", f"Could not run the browser test: {type(e).__name__}: {str(e)[:200]}", []

    doc.update(last_tested_at=utc_now(), last_test_status=status, last_test_message=message, updated_at=utc_now())
    saved = False
    if status == "success" and cookies:
        try:
            cleaned = normalize_cookies(cookies, platform)
            doc.update(cookies_enc=encrypt_json(cleaned), cookie_count=len(cleaned), cookies_saved_at=utc_now())
            saved = True
        except ValueError:
            pass
    repo.save_credentials(doc)
    return TestLoginResult(platform=platform, status=status, message=message, cookies_saved=saved,
                           cookie_count=doc.get("cookie_count", 0) if saved else 0)
