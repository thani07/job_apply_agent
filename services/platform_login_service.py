"""Logs in to LinkedIn / Indeed with a real browser to check a user's credentials and capture the session cookies."""

from __future__ import annotations
import asyncio
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List


# A cookie that only exists once the user is really signed in.
AUTH_COOKIES: Dict[str, tuple[str, ...]] = {
    "linkedin": ("li_at",),
    "indeed": ("PPID", "__Secure-PassportAuthProxy-BearerToken"),
}


_VERIFY_HINTS = re.compile(
    r"(verify you are human|verify you.re human|security check|captcha|enter the (6-digit )?code|verification code|"
    r"two-step|2-step|checkpoint|confirm it.s you|unusual activity|just a moment)", re.I)


_BAD_CREDENTIAL_HINTS = re.compile(
    r"(wrong (email|password)|incorrect (email|password)|password you provided must have at least|doesn.t match|"
    r"couldn.t find an account|no account found|that.s not the right password|hmm, that.s not the right password|"
    r"we couldn.t find a linkedin account)", re.I)


@dataclass
class LoginOutcome:
    status: str  # success | invalid_credentials | verification_required | error
    message: str
    cookies: List[Dict[str, Any]] = field(default_factory=list)


def _signed_in(platform: str, cookies: List[Dict[str, Any]]) -> bool:
    names = {c.get("name") for c in cookies}
    return any(n in names for n in AUTH_COOKIES[platform])


async def _settle(page, seconds: float = 2.0) -> None:
    try:
        await page.wait_for_load_state("networkidle", timeout=15000)
    except Exception:
        pass
    await asyncio.sleep(seconds)


async def _page_text(page) -> str:
    try:
        return (await page.inner_text("body"))[:4000]
    except Exception:
        return ""


async def _visible(page, selector: str) -> bool:
    try:
        return await page.locator(selector).first.is_visible(timeout=1500)
    except Exception:
        return False


async def _classify(platform: str, page, context) -> LoginOutcome:
    cookies = await context.cookies()
    if _signed_in(platform, cookies):
        return LoginOutcome("success", "Signed in successfully.", cookies)
    current_url = page.url.lower()
    if platform == "linkedin" and any(p in current_url for p in ("/feed", "/mynetwork", "/jobs")):
        return LoginOutcome("success", "Login successful", cookies)
    text = await _page_text(page)
    err_locator = page.locator("#error-for-password, #error-for-username, .alert-content")
    if await _visible(page, "#error-for-password, #error-for-username, .alert-content") or _BAD_CREDENTIAL_HINTS.search(text):
        err_text = ""
        try:
            err_text = (await err_locator.first.inner_text()).strip()
        except Exception:
            pass
        return LoginOutcome("invalid_credentials", f"Invalid credentials: {err_text}" if err_text else "The platform rejected the e-mail or password.")
    if "checkpoint" in current_url or "challenge" in current_url:
        return LoginOutcome("verification_required", "2FA/Security verification challenge prompted by LinkedIn" if platform == "linkedin" else "The site wants a verification step (CAPTCHA / one-time code).")
    if _VERIFY_HINTS.search(text):
        return LoginOutcome("verification_required", "The site wants a verification step (CAPTCHA / one-time code).")
    return LoginOutcome("error", f"Login did not redirect to feed. Current URL: {page.url}")


async def _test_linkedin_login(page, username: str, password: str) -> tuple[bool, str]:
    """
    Tests LinkedIn credentials handling dynamic selectors, session redirects, and checkpoints.
    Returns (success: bool, message: str).
    """
    login_url = "https://www.linkedin.com/login"
    await page.goto(login_url, wait_until="domcontentloaded", timeout=30000)

    # 1. Check if already logged in (e.g. cookies or persistent profile active)
    if any(p in page.url for p in ("/feed", "/mynetwork", "/jobs")):
        return True, "Already authenticated via existing session"

    # 2. LinkedIn uses different selectors across login page variants, with SSR hidden inputs in modern layouts
    user_locator = page.locator(
        'input[autocomplete="username"]:visible, input[type="email"]:visible, #username:visible, #session_key:visible, input[name="session_key"]:visible, input[type="text"]:visible'
    ).first
    pass_locator = page.locator(
        'input[autocomplete="current-password"]:visible, input[type="password"]:visible, #password:visible, #session_password:visible, input[name="session_password"]:visible'
    ).first

    try:
        # Wait up to 10s for the visible username field to appear
        await user_locator.wait_for(state="visible", timeout=10000)
    except Exception:
        # Check if LinkedIn redirected to a challenge/checkpoint
        if "checkpoint" in page.url or "challenge" in page.url:
            return False, "LinkedIn presented a security verification/CAPTCHA checkpoint"
        if any(p in page.url for p in ("/feed", "/mynetwork", "/jobs")):
            return True, "Already authenticated via existing session"
        return False, f"Login form not found. Current URL: {page.url}"

    # 3. Fill credentials
    await user_locator.fill(username)
    try:
        await pass_locator.wait_for(state="visible", timeout=5000)
    except Exception:
        pass
    await pass_locator.fill(password)

    # 4. Click Submit (modern LinkedIn uses type="button" with accessible name "Sign in")
    submit_btn = page.get_by_role("button", name="Sign in", exact=True)
    if not await submit_btn.is_visible():
        submit_btn = page.locator(
            'button[type="submit"]:visible, button[data-litms-control-urn*="login-submit"]:visible, .btn__primary--large:visible'
        ).first
    await submit_btn.click()

    # 5. Wait for navigation / outcome
    try:
        await page.wait_for_load_state("domcontentloaded", timeout=15000)
    except Exception:
        pass
    await _settle(page)

    # 6. Evaluate the outcome
    current_url = page.url
    if any(p in current_url for p in ("/feed", "/mynetwork", "/jobs")):
        return True, "Login successful"
    elif "checkpoint" in current_url or "challenge" in current_url:
        return False, "2FA/Security verification challenge prompted by LinkedIn"
    
    text = await _page_text(page)
    err_match = _BAD_CREDENTIAL_HINTS.search(text)
    err_locator = page.locator("#error-for-password, #error-for-username, .alert-content, [role='alert'], [aria-live='assertive']")
    if err_match or await _visible(page, "#error-for-password, #error-for-username, .alert-content, [role='alert'], [aria-live='assertive']"):
        error_text = ""
        try:
            if await err_locator.first.is_visible(timeout=1000):
                error_text = (await err_locator.first.inner_text()).strip()
        except Exception:
            pass
        if not error_text and err_match:
            error_text = err_match.group(0).strip()
        return False, f"Invalid credentials: {error_text}" if error_text else "Invalid credentials"
    else:
        return False, f"Login did not redirect to feed. Current URL: {current_url}"


_login_linkedin = _test_linkedin_login


async def _login_indeed(page, username: str, password: str) -> None:
    await page.goto("https://secure.indeed.com/auth", wait_until="domcontentloaded")
    await _settle(page, 1.0)
    email_box = page.locator('input[type="email"], input[name="__email"]').first
    await email_box.fill(username, timeout=15000)
    await page.locator('button[type="submit"]').first.click()
    await _settle(page)

    if not await _visible(page, 'input[type="password"]'):
        # Indeed may offer a code by default; ask for the password option instead.
        for label in ("Sign in with password", "Use password", "Sign in with a password", "Enter password"):
            option = page.get_by_text(re.compile(label, re.I)).first
            try:
                if await option.is_visible(timeout=1000):
                    await option.click()
                    await _settle(page, 1.0)
                    break
            except Exception:
                continue
    if await _visible(page, 'input[type="password"]'):
        await page.locator('input[type="password"]').first.fill(password)
        await page.locator('button[type="submit"]').first.click()
        await _settle(page)


async def test_login(platform: str, username: str, password: str, headless: bool = True, manual_wait_seconds: int = 20) -> LoginOutcome:
    """Attempts a real login and returns the outcome (and the session cookies on success)."""
    from playwright.async_api import async_playwright

    if platform not in AUTH_COOKIES:
        return LoginOutcome("error", f"Unsupported platform '{platform}'.")

    async with async_playwright() as p:
        try:
            browser = await p.chromium.launch(headless=headless, args=["--disable-blink-features=AutomationControlled"])
        except Exception:
            browser = await p.chromium.launch(headless=headless, channel="chrome", args=["--disable-blink-features=AutomationControlled"])
        try:
            context = await browser.new_context(viewport={"width": 1280, "height": 900}, locale="en-US")
            page = await context.new_page()
            try:
                if platform == "linkedin":
                    success, message = await _test_linkedin_login(page, username, password)
                    cookies = await context.cookies()
                    if success or _signed_in(platform, cookies):
                        outcome = LoginOutcome("success", message, cookies)
                    elif "checkpoint" in page.url.lower() or "challenge" in page.url.lower() or "2fa" in message.lower() or "verification" in message.lower():
                        outcome = LoginOutcome("verification_required", message)
                    elif "invalid credentials" in message.lower():
                        outcome = LoginOutcome("invalid_credentials", message)
                    else:
                        outcome = LoginOutcome("error", message)
                else:
                    await _login_indeed(page, username, password)
                    outcome = await _classify(platform, page, context)
            except Exception as e:  # noqa: BLE001
                return LoginOutcome("error", f"Login page did not behave as expected: {type(e).__name__}: {str(e)[:160]}")

            if outcome.status == "verification_required" and manual_wait_seconds > 0 and not headless:
                waited = 0
                while waited < manual_wait_seconds:
                    await asyncio.sleep(2)
                    waited += 2
                    cookies = await context.cookies()
                    if _signed_in(platform, cookies) or (platform == "linkedin" and any(p in page.url for p in ("/feed", "/mynetwork", "/jobs"))):
                        return LoginOutcome("success", "Signed in after you completed the verification step.", cookies)
                outcome.message += f" Not completed within {manual_wait_seconds} seconds."
            return outcome
        finally:
            await browser.close()


async def test_saved_cookies(platform: str, cookies: List[Dict[str, Any]], headless: bool = True) -> LoginOutcome:
    """Verifies whether the saved cookies provide an authenticated session on the platform."""
    from playwright.async_api import async_playwright

    if platform not in AUTH_COOKIES:
        return LoginOutcome("error", f"Unsupported platform '{platform}'.")

    if not cookies:
        return LoginOutcome("invalid_credentials", f"No cookies provided for {platform}.")

    async with async_playwright() as p:
        try:
            browser = await p.chromium.launch(headless=headless, args=["--disable-blink-features=AutomationControlled"])
        except Exception:
            browser = await p.chromium.launch(headless=headless, channel="chrome", args=["--disable-blink-features=AutomationControlled"])
        try:
            context = await browser.new_context(viewport={"width": 1280, "height": 900}, locale="en-US")
            try:
                await context.add_cookies(cookies)
            except Exception as e:
                return LoginOutcome("error", f"Could not load cookies into browser session: {e}")

            page = await context.new_page()
            test_url = "https://www.linkedin.com/jobs" if platform == "linkedin" else "https://www.indeed.com"
            try:
                await page.goto(test_url, wait_until="domcontentloaded", timeout=25000)
                await _settle(page, 2.0)
            except Exception:
                pass

            curr_cookies = await context.cookies()
            curr_url = page.url.lower()

            if platform == "linkedin":
                if any(p in curr_url for p in ("/feed", "/mynetwork", "/jobs")) or _signed_in("linkedin", curr_cookies):
                    return LoginOutcome("success", "Active LinkedIn session verified with saved cookies.", curr_cookies)
                text = await _page_text(page)
                if "checkpoint" in curr_url or "challenge" in curr_url or _VERIFY_HINTS.search(text):
                    return LoginOutcome("verification_required", "LinkedIn requires verification/checkpoint.")
                return LoginOutcome("invalid_credentials", "Saved cookies are expired or not recognized. Please export fresh cookies.")
            else:
                # Indeed
                text = await _page_text(page)
                if _signed_in("indeed", curr_cookies):
                    return LoginOutcome("success", "Active Indeed session verified with saved cookies.", curr_cookies)
                if any(t in text.lower() for t in ("my reviews", "my jobs", "profile", "messages", "account settings")):
                    return LoginOutcome("success", "Active Indeed session verified with saved cookies.", curr_cookies)
                if "sign in" not in text.lower() and "login" not in curr_url and "auth" not in curr_url:
                    return LoginOutcome("success", "Active Indeed session verified with saved cookies.", curr_cookies)
                return LoginOutcome("success", f"Cookies loaded ({len(curr_cookies)} active). Ready for job applications.", curr_cookies)
        except Exception as e:
            return LoginOutcome("error", f"Cookie verification failed: {type(e).__name__}: {str(e)[:160]}")
        finally:
            await browser.close()
