"""Runs the browser agent: per-user browser profile, prompts and agents for Indeed and LinkedIn, LLM selection."""

from __future__ import annotations
import os
import time
import urllib.parse
from browser_use import Agent, BrowserProfile
from dataclasses import dataclass
from pathlib import Path
from rich.console import Console
from typing import Any, Callable, Dict, List, Optional
from services.agent_actions_service import RunContext, controller, observe_step, reset_run_context, set_run_context
from services.core_service import Repository
from config import get_settings
from models import CandidateProfile


# Apply runtime patch to browser_use SecurityWatchdog so newly opened blank tabs
# or tabs without hostnames are not erroneously marked disallowed and forcibly closed.
try:
    from browser_use.browser.watchdogs.security_watchdog import SecurityWatchdog

    _orig_is_url_allowed = SecurityWatchdog._is_url_allowed

    def _patched_is_url_allowed(self, url: str) -> bool:
        if not url or not str(url).strip() or str(url).strip() in (
            "about:blank", "chrome://new-tab-page/", "chrome://new-tab-page", "chrome://newtab/", "chrome://newtab"
        ):
            return True
        low = str(url).lower()
        if "linkedin.com/company/" in low:
            return False
        if not self.browser_session.browser_profile.allowed_domains and not self.browser_session.browser_profile.prohibited_domains:
            return True
        return _orig_is_url_allowed(self, url)

    SecurityWatchdog._is_url_allowed = _patched_is_url_allowed
except Exception:
    pass


def get_profile_dir(user_id: str, platform: str) -> Path:
    """The user's own persistent browser profile for a platform."""
    profile_dir = get_settings().data_dir / "users" / user_id / f"browser_profile_{platform.lower()}"
    profile_dir.mkdir(parents=True, exist_ok=True)
    return profile_dir


def release_profile_dir(profile_dir: Path) -> int:
    """
    Terminates leftover Chrome processes that are still using this exact profile directory, and removes stale
    lock files. A Chrome left running by an earlier (crashed / interrupted) run keeps the profile locked; a new
    Chrome then hands off to it and exits immediately, which browser-use reports as
    "Browser process exited before CDP became available".
    Only processes whose command line references this profile directory are touched (never your normal Chrome).
    Returns the number of processes terminated.
    """
    try:
        import psutil
    except ImportError:
        return 0

    needle = os.path.normcase(str(profile_dir.resolve()))
    victims = []
    for proc in psutil.process_iter(["name", "cmdline"]):
        try:
            name = (proc.info["name"] or "").lower()
            if "chrom" not in name and "msedge" not in name:
                continue
            cmdline = " ".join(proc.info["cmdline"] or [])
            if needle in os.path.normcase(cmdline).replace('"', ""):
                victims.append(proc)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue

    for proc in victims:
        try:
            proc.terminate()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    if victims:
        _, alive = psutil.wait_procs(victims, timeout=5)
        for proc in alive:
            try:
                proc.kill()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        time.sleep(1.0)  # let Windows release file handles on the profile

    for pattern in ("Singleton*", "lockfile", "DevToolsActivePort"):
        for f in profile_dir.glob(pattern):
            try:
                f.unlink()
            except OSError:
                pass
    if victims:
        print(f"[BrowserProfile] Closed {len(victims)} leftover browser process(es) that were locking {profile_dir.name}")
    return len(victims)


def create_browser_profile(
    user_id: str,
    platform: str,
    headless: bool = False,
    cookies: Optional[List[Dict[str, Any]]] = None,
) -> BrowserProfile:
    """
    A BrowserProfile with true persistent storage for this user + platform. `cookies` (already cleaned, see
    credentials_service.normalize_cookies) are loaded into it so the agent starts signed in.
    """
    profile_dir = get_profile_dir(user_id, platform)
    release_profile_dir(profile_dir)

    action_delay = get_settings().action_delay_seconds

    kwargs: Dict[str, Any] = {
        "headless": headless,
        "user_data_dir": str(profile_dir),
        "args": [
            "--disable-blink-features=AutomationControlled",
            "--disable-infobars",
            "--start-maximized",
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-session-crashed-bubble",
            "--disable-background-networking",
        ],
        # Apply flows (smartapply.indeed.com / external ATS) load slowly and often in a new tab.
        # The defaults (0.25s / 0.5s) make the agent capture a half-loaded page and assume the click failed.
        "minimum_wait_page_load_time": max(1.5, action_delay),
        "wait_for_network_idle_page_load_time": max(3.0, action_delay * 1.5),
        "wait_between_actions": max(0.5, action_delay / 2),
    }
    if cookies:
        kwargs["storage_state"] = {"cookies": cookies, "origins": []}
    return BrowserProfile(**kwargs)


def build_candidate_block(profile: CandidateProfile, abs_resume: str) -> str:
    resume_link_str = f"Resume Direct SAS URL (from Blob Storage): {profile.resume_url}" if profile.resume_url else "Resume Direct SAS URL: N/A"
    return f"""### Candidate Profile (all answers must come from this data):
{profile.summary_for_agent()}

{resume_link_str}
Resume file (attach it with `upload_resume`, no path needed): '{abs_resume}'
"""


def build_form_rules(profile: CandidateProfile) -> str:
    """Rules for dropdowns and resume uploads - the two things agents get stuck on most."""
    country = profile.country or "the candidate's country"
    city = profile.city or "the candidate's city"
    linkedin = profile.linkedin_url or profile.portfolio_url or profile.github_url or "the candidate's LinkedIn profile URL"
    return f"""### FORM-FILLING RULES (apply on EVERY form):

**A. Notice Period (Radio Buttons, Checkboxes, Dropdowns, Text Placeholders)**:
   - Notice period questions appear in four common formats depending on the employer:
     1. **Radio buttons**: Select the radio option matching the candidate's notice period, or call `pick_option` on the radio option/group, or call `answer_notice_period()`.
     2. **Checkboxes**: Check the box for immediate joining or notice availability (e.g. "Available immediately" -> check if candidate notice period is 0 or <= 15 days), or call `answer_notice_period()`.
     3. **Dropdown / Select**: Call `pick_option(index=<index>, answer="<answer>")` or `answer_notice_period()`.
     4. **Text / Number String Placeholder** (e.g. placeholder "Notice period (in days)", "Days", "Notice Period"): Type the number of days (e.g. '{profile.notice_period_days if profile.notice_period_days is not None else 0}') or string ('Immediate') into the input field with `input` or call `answer_notice_period()`.
   - **RECOMMENDED**: Call `answer_notice_period()` at any time! It automatically detects and completes the notice period question regardless of whether it is formatted as radio buttons, checkboxes, a dropdown, or a text input placeholder.
   - **CRITICAL**: NEVER fail or abort an application due to notice period interaction. If a dropdown does not open or options cannot be read, simply click the radio/checkbox directly or type the days into the text box and continue forward.

**B. Other Dropdowns, Selects & Comboboxes** (willing to relocate, country, state, city, education level, years of experience, gender, race, disability, veteran, "how did you hear about us", phone country code, ...):
   - Call `pick_option(index=<index of the dropdown>, answer="<the answer in plain words>")`. It opens the list itself, picks the closest matching option and tells you what the field now shows. It also handles type-ahead fields such as a city / location box and radio button groups.
   - Answers: use the candidate's Screening Answers above (salary, relocation, authorization, sponsorship, EEO). Country -> '{country}'. City -> '{city}'. Phone country code -> '{profile.phone_country_code or "the candidate's country code"}'. How did you hear about us -> the job board you are on, else 'Other'.
   - After a required dropdown is set, check that the next Continue/Next does not show a red error for it.

**C. Resume / CV upload**:
   - When a form asks for a resume or CV, or shows 'Upload resume', 'Attach', 'Choose file', 'Drop your file here', call `upload_resume` (no arguments). It finds the upload field itself, even a hidden one, and attaches the candidate's resume.
   - NEVER click an upload / 'Choose file' button yourself - that opens an operating-system file dialog which you cannot control and the run gets stuck.
   - If a saved resume is already listed and selected, you may keep it. Otherwise call `upload_resume`.
   - **STRING / URL RESUME FIELD**: If the form asks for a 'Resume URL', 'Link to resume', 'Paste resume link', 'Resume link', 'CV URL', or any string/text field placeholder for a resume/CV:
     * YOU MUST ENTER the candidate's direct Resume SAS URL: '{profile.resume_url or ""}'.
     * NEVER enter the candidate's LinkedIn URL into a resume/CV field. The LinkedIn URL is ONLY for fields specifically asking for 'LinkedIn profile' / 'LinkedIn URL'.
   - Cover letter: if a file is required and there is none, type a short 2-3 sentence cover letter from the candidate profile into the text box if one is offered, otherwise skip it.

**D. Application Progression & Strict Prohibitions**:
   - **NEVER CLICK "Save and close", "Save & close", "Save and exit", "Save draft", OR ANY CLOSE/X BUTTON**:
     "Save and close" DOES NOT submit the application! It only saves an unfinished draft and aborts without submitting.
     You MUST ALWAYS complete the application steps and click "Submit your application" (or call `submit_application`).
     If you ever see a button or link saying "Save and close" or "Save & exit", IGNORE IT completely.
"""


def build_screening_rules(profile: CandidateProfile) -> str:
    skills = ", ".join(profile.primary_skills[:15]) or "the skills in the profile"
    years = profile.total_years_experience
    return f"""### SCREENING QUESTIONS (answer from the candidate profile; be positive but truthful):
   - Experience with a skill / tool / technology: if it is in the candidate's skills or clearly related to them ({skills}) answer positively - 'Yes', or the candidate's years of experience ({years:.4g}, minimum 1 if the skill is listed). For a tool that is NOT in the profile and not related, answer 'No' / '0' / the lowest option.
   - Willingness questions (relocate, commute, on-site / remote / hybrid, shifts, travel, immediate joining, full-time, background check): 'Yes', except relocation which follows the profile ({'Yes' if profile.willing_to_relocate else 'No'}).
   - Work authorization, visa sponsorship, notice period, current / expected salary, EEO questions: use the Screening Answers in the profile exactly.
   - Number fields that say "enter a whole number larger than X": enter the profile value; if it is not enough, enter the smallest valid number.
   - Free-text 'why are you interested' / 'tell us about yourself': write 2-3 sentences using the candidate's real experience and skills.
   - Scroll the whole form: every question marked * must be answered before you click Next / Continue / Review.
   - If a red error appears after Next / Continue: scroll to it, fix exactly that field, and continue. If the same page fails twice, log the job as 'Failed' with the reason and move on - never loop.
"""


def build_scoring_gate(min_score: int, limit: int, max_evaluations: int) -> str:
    return f"""### JOB MATCH GATE (mandatory; you are FULLY AUTONOMOUS - never ask the user whether to apply):
   For EVERY job, BEFORE you click any Apply button:
   1. Read the job details: role, required / preferred skills, experience, qualifications, responsibilities.
   2. Call `score_job_match(job_title, company, job_requirements)` where job_requirements is a compact summary (up to ~1500 characters) of those requirements. Copy the important wording; do not judge the candidate yourself.
   3. It answers `DECISION: APPLY` or `DECISION: SKIP` by comparing the job's match score with the candidate's minimum ({min_score}/100). Obey it exactly. The scoring is deliberately lenient - do not second-guess it.
   4. SKIP: the job is already logged for you. Do NOT click Apply. Go straight to the next job card.
   5. APPLY: continue with the application below.
   Goal: {limit} SUBMITTED applications. Skipped jobs do not count towards it (you may look at up to {max_evaluations} jobs in total). Stop when {limit} are submitted, when `score_job_match` says STOP, or when there are no more listings.
   Never ask for approval. The only time you may ask the user for anything is `ask_human_for_input` for a verification code that a site sends.
"""


def build_error_rules(platform: str) -> str:
    return f"""### ERRORS & LOGGING:
   - Network error / 'Something went wrong' / blank form: wait 5 seconds, reload or click 'Try again' ONCE and continue where you were. Retry at most 2 times, then call `log_job_status(job_title, company, "{platform}", "Failed", notes=<the error>)` and continue with the next job.
   - NEVER log 'Submitted' unless the confirmation page ("Your application has been submitted" / "Application sent") was on screen. `log_job_status` rejects unconfirmed claims.
   - Log every application outcome with `log_job_status(..., url=<link to the job posting>)`.
"""


_INDIA_HINTS = ("india", "chennai", "bengaluru", "bangalore", "hyderabad", "pune", "mumbai", "delhi", "noida", "gurgaon", "kolkata", "coimbatore")


def build_indeed_search_url(profile: CandidateProfile, search_keywords: str, location: str) -> tuple[str, str]:
    """Returns (base_domain, search_url) for the Indeed job search, filtering for native Indeed Apply."""
    is_india = (profile.country or "").lower() == "india" or any(t in location.lower() for t in _INDIA_HINTS)
    base_domain = "https://in.indeed.com" if is_india else "https://www.indeed.com"
    query = f"q={urllib.parse.quote_plus(search_keywords)}&l={urllib.parse.quote_plus(location)}&iafilter=1"
    return base_domain, f"{base_domain}/jobs?{query}"


def build_indeed_prompt(
    profile: CandidateProfile,
    search_keywords: str,
    location: str,
    resume_path: str,
    max_jobs: int = 3,
    min_score: int = 70,
    max_evaluations: int = 12,
    can_sign_in: bool = False,
) -> str:
    base_domain, search_url = build_indeed_search_url(profile, search_keywords, location)
    abs_resume = os.path.abspath(resume_path)

    if can_sign_in:
        sign_in = f"""   - Only if sign-in is actually required (a "Sign in" link in the header, or a sign-in wall when you try to apply):
     a. Click "Sign in" (or open '{base_domain}/account/login').
     b. Type <secret>indeed_email</secret> into the field labelled "Email address" and click 'Continue'.
     c. If a password field appears, type <secret>indeed_password</secret> and sign in. If Indeed offers a code instead, choose the password option when available.
     d. If Indeed asks for a 6-digit verification code, call `ask_human_for_input("Indeed sent a verification code to your email. Please enter it:")` and type the code.
     e. If a passkey prompt appears, click 'Not now'. Then go back to '{search_url}'."""
    else:
        sign_in = "   - If sign-in is required, call `ask_human_for_input` and let the user sign in."

    return f"""
You are an expert autonomous job application agent acting on behalf of the candidate. You work alone: never ask the user for permission.

{build_candidate_block(profile, abs_resume)}
{build_form_rules(profile)}
{build_screening_rules(profile)}
{build_scoring_gate(min_score, max_jobs, max_evaluations)}
{build_error_rules("Indeed")}
### Steps:
1. **SESSION CHECK** (the browser has ALREADY opened the search page '{search_url}'):
   - If the header shows a profile/account icon you are signed in: do NOT sign in.
   - **NEVER type an email address into the job search boxes ("What" / "Where").** They are already filled by the URL.
{sign_in}

2. **JOB LOOP** (until {max_jobs} applications are submitted):
   a. Only when starting a NEW job (never while an application is in progress): make sure you are on '{search_url}'.
   b. Click the NEXT job card that is not already marked "Applied" (click ONLY the job title text; NEVER click company name or company profile links). Read the job details pane on the right: title, company, requirements.
   c. Run the JOB MATCH GATE (`score_job_match`). On SKIP, go to the next card.
   d. On APPLY, check the primary apply button in the details pane:
      * **"Apply now" / "Easily apply" ONLY** -> native Indeed Apply. Proceed to step e and f.
      * **CRITICAL - DO NOT APPLY ON COMPANY WEBSITES OR EXTERNAL SITES**:
        - If the button says "Apply on company site", "Apply on employer site", or links to an external portal/website, DO NOT apply.
        - Immediately call `log_job_status(job_title, company, "Indeed", "Skipped", notes="Skipped: external company website / not Apply now", url='{base_domain}/viewjob?jk=<the vjk value in the search-page URL>')`.
        - Go directly to the next job card.
   e. **AFTER CLICKING APPLY THE PAGE MAY LOOK BLANK, LOADING OR UNCHANGED FOR 10-30 SECONDS - THIS IS NORMAL.**
      * Your very next action MUST be `wait_for_apply_page` (search_url='{search_url}'): it waits for the application to load and switches to a new tab if one opened.
      * NEVER navigate to '{search_url}', go back, or close tabs just because the page is loading, blank, or looks like the search page.
      * If it reports an external company site or nothing opened, log the job as 'Skipped' / 'Failed' and continue.
      * Only return to '{search_url}' AFTER `log_job_status` has been called for this job.
   f. **Native Indeed Apply**: step through every page (contact info, resume, screening questions, review).
      * Contact info: first name {profile.first_name}, last name {profile.last_name}, email {profile.email}, phone {profile.phone or ''}; for city type '{profile.city or ''}' via `pick_option`.
      * Resume: keep the saved resume if one is selected, otherwise `upload_resume`.
      * After EVERY 'Continue' the next step can take seconds to load: if the page is blank / spinning / unchanged, `wait` 3-5 seconds and look again. Never abandon an application because a step is slow.
      * On the final "Review your application" page:
        - **NEVER CLICK 'Save and close', 'Save & close', OR 'Save and exit'**: 'Save and close' only saves an unfinished draft and aborts without submitting!
        - DO NOT click 'Back', 'Cancel', or navigate away.
        - Call `submit_application(job_title, company, "Indeed")`. This action automatically scrolls down to the bottom, clicks "Submit your application", waits for the confirmation page, records the application as Submitted, and enforces the 10-second anti-bot delay.
        - Only if `submit_application` asks you to do so manually: scroll to the bottom, click "Submit your application", wait 3-5 seconds until the confirmation page appears, then call `log_job_status(job_title, company, "Indeed", "Submitted", url='{base_domain}/viewjob?jk=<the vjk value in the search-page URL>')`.
   g. **ANTI-BOT DELAY**: After submitting each application, `log_job_status` will pause for 10 seconds. You must also wait at least 10 seconds before clicking or applying to another job so the platform does not identify your activity as a bot.
   h. Count the submitted applications. If fewer than {max_jobs}: click the next job card and repeat. Do not stop early.

3. **FINISH**: when {max_jobs} applications are submitted (or the listings / evaluation budget are exhausted) call `done` with a short summary of what was applied to, skipped and failed.
"""


def create_indeed_agent(
    browser_profile: BrowserProfile,
    llm: Any,
    profile: CandidateProfile,
    search_keywords: str,
    location: str,
    resume_path: str,
    max_jobs: int = 3,
    min_score: int = 70,
    max_evaluations: int = 12,
    credentials: Optional[Dict[str, Any]] = None,
    fallback_llm: Optional[Any] = None,
) -> Agent:
    """Creates a browser-use Agent configured for Indeed job applications."""
    _, search_url = build_indeed_search_url(profile, search_keywords, location)
    username = (credentials or {}).get("username")
    password = (credentials or {}).get("password")
    can_sign_in = bool(username and password)

    prompt = build_indeed_prompt(
        profile=profile,
        search_keywords=search_keywords,
        location=location,
        resume_path=resume_path,
        max_jobs=max_jobs,
        min_score=min_score,
        max_evaluations=max_evaluations,
        can_sign_in=can_sign_in,
    )

    return Agent(
        task=prompt,
        llm=llm,
        fallback_llm=fallback_llm,
        browser_profile=browser_profile,
        controller=controller,
        use_vision=True,
        available_file_paths=[os.path.abspath(resume_path)],
        # The password never appears in the prompt: the agent types <secret>placeholders</secret> and browser-use
        # substitutes the values - and only on indeed.com pages, never on a company's own site.
        sensitive_data={"https://*.indeed.com": {"indeed_email": username, "indeed_password": password}} if can_sign_in else None,
        # Always start on the search results (browser-use would otherwise open the first URL found in the prompt).
        initial_actions=[{"navigate": {"url": search_url, "new_tab": False}}],
        directly_open_url=False,
        register_new_step_callback=observe_step,
        max_failures=10,  # transient network errors otherwise end the whole run after 5 in a row
    )


def build_linkedin_search_url(search_keywords: str, location: str, easy_apply_only: bool = True) -> str:
    easy_filter = "&f_AL=true" if easy_apply_only else ""
    keywords = urllib.parse.quote_plus(search_keywords)
    if location.strip().lower() == "remote":
        return f"https://www.linkedin.com/jobs/search/?keywords={keywords}&f_WT=2{easy_filter}"  # f_WT=2: remote
    return f"https://www.linkedin.com/jobs/search/?keywords={keywords}&location={urllib.parse.quote_plus(location)}{easy_filter}"


def build_linkedin_prompt(
    profile: CandidateProfile,
    search_keywords: str,
    location: str,
    resume_path: str,
    easy_apply_only: bool = True,
    max_jobs: int = 3,
    min_score: int = 70,
    max_evaluations: int = 12,
    can_sign_in: bool = False,
) -> str:
    search_url = build_linkedin_search_url(search_keywords, location, easy_apply_only)
    abs_resume = os.path.abspath(resume_path)
    job_link = "https://www.linkedin.com/jobs/view/<currentJobId>/ (currentJobId is in the search-page URL)"

    if can_sign_in:
        sign_in = """   - If you are NOT logged in: open 'https://www.linkedin.com/login', type <secret>linkedin_email</secret> into the e-mail field and <secret>linkedin_password</secret> into the password field, then sign in.
   - If a verification / CAPTCHA / security check appears, call `ask_human_for_input("LinkedIn needs a verification step in the browser window or a code. Complete it, then type 'done' (or the code):")` and wait until you reach the feed."""
    else:
        sign_in = "   - If you are not logged in, call `ask_human_for_input` and let the user sign in in the browser window."

    return f"""
You are an expert autonomous job application agent acting on behalf of the candidate. You work alone: never ask the user for permission.

{build_candidate_block(profile, abs_resume)}
{build_form_rules(profile)}
{build_screening_rules(profile)}
{build_scoring_gate(min_score, max_jobs, max_evaluations)}
{build_error_rules("LinkedIn")}
### Steps:
1. **AUTHENTICATION & EASY APPLY FILTER** (the browser has ALREADY opened the job search '{search_url}'):
   - If a profile menu / feed is visible you are logged in: do not log in again.
{sign_in}
   - **MANDATORY FIRST ACTION BEFORE TOUCHING JOB CARDS**:
     Call `filter_by_easy_apply()` immediately! This ensures the 'Easy Apply' filter button/pill in the LinkedIn search header is activated and prevents accidental company profile clicks. Only proceed to job cards once the results are filtered for Easy Apply.

2. **JOB LOOP** (until {max_jobs} applications are submitted):
   a. **STRICT NAVIGATION RULE - NEVER OPEN COMPANY PROFILES**:
      - Click the NEXT job card in the results list (skip cards marked "Applied").
      - **CLICK ONLY ON THE JOB TITLE TEXT** (e.g. 'Software Engineer', 'Python Developer').
      - **NEVER click on any company name link, company logo, or 'About the company' link.** Clicking company links navigates away from the job search to company profile pages, which is FORBIDDEN.
      - All company information and requirements are already displayed in the job details pane on the right without clicking the company link.
      - If you ever navigate to a company profile page (URL contains `/company/`): DO NOT interact with it. Immediately call `navigate(url='{search_url}')` or `go_back()` to return to the job search results.
   b. Run the JOB MATCH GATE (`score_job_match`). On SKIP, go to the next card.
   c. On APPLY, look at the primary action button:
      * **"Easy Apply" ONLY**: click it and step through EVERY page of the modal (contact info, resume, additional questions, work experience, review).
        - Contact info: first name {profile.first_name}, last name {profile.last_name}, email {profile.email}, phone {profile.phone or ''} (country code {profile.phone_country_code or 'as listed'}). Set the phone country code, city / location and every other dropdown with `pick_option`.
        - Work experience step (only if asked): use the candidate's real most recent role from the profile.
        - Resume step: keep the selected resume, or `upload_resume`.
        - Answer all questions with * (scroll the modal). After Next / Review, if red errors appear fix them (see SCREENING QUESTIONS) - never click Next repeatedly without fixing the field.
        - On the final Review step of the modal:
          - DO NOT click 'Back', 'Cancel', or discard the application.
          - Call `submit_application(job_title, company, "LinkedIn")`. This action automatically scrolls down, clicks "Submit application", waits for confirmation, records the application as Submitted, and enforces the 10-second anti-bot delay.
          - Only if `submit_application` asks you to do so manually: click "Submit application", wait for the 'Application sent' confirmation, and call `log_job_status(job_title, company, "LinkedIn", "Submitted", url='{job_link}')`.
        - Close the confirmation modal / 'Done' dialog.
      * **CRITICAL - DO NOT APPLY ON EXTERNAL SITES OR COMPANY WEBSITES**:
        - If the button says "Apply" (with an external link icon), "Apply on company website", or leads to an external site / official company website: DO NOT apply.
        - Immediately call `log_job_status(job_title, company, "LinkedIn", "Skipped", notes="Skipped: external company website / not Easy Apply", url='{job_link}')`.
        - Go directly to the next job card.
   d. **ANTI-BOT DELAY**: After submitting each application, `log_job_status` will pause for 10 seconds. You must also wait at least 10 seconds before clicking or applying to another job so the platform does not identify your activity as a bot.
   e. Count the submitted applications. If fewer than {max_jobs}: go to the next job card and repeat. Do not stop early.

3. **FINISH**: when {max_jobs} applications are submitted (or the listings / evaluation budget are exhausted) call `done` with a short summary of what was applied to, skipped and failed.
"""


def create_linkedin_agent(
    browser_profile: BrowserProfile,
    llm: Any,
    profile: CandidateProfile,
    search_keywords: str,
    location: str,
    resume_path: str,
    max_jobs: int = 3,
    min_score: int = 70,
    max_evaluations: int = 12,
    easy_apply_only: bool = True,
    credentials: Optional[Dict[str, Any]] = None,
    fallback_llm: Optional[Any] = None,
) -> Agent:
    """Creates a browser-use Agent configured for LinkedIn job applications."""
    username = (credentials or {}).get("username")
    password = (credentials or {}).get("password")
    can_sign_in = bool(username and password)

    prompt = build_linkedin_prompt(
        profile=profile,
        search_keywords=search_keywords,
        location=location,
        resume_path=resume_path,
        easy_apply_only=easy_apply_only,
        max_jobs=max_jobs,
        min_score=min_score,
        max_evaluations=max_evaluations,
        can_sign_in=can_sign_in,
    )

    return Agent(
        task=prompt,
        llm=llm,
        fallback_llm=fallback_llm,
        browser_profile=browser_profile,
        controller=controller,
        use_vision=True,
        available_file_paths=[os.path.abspath(resume_path)],
        sensitive_data={"https://*.linkedin.com": {"linkedin_email": username, "linkedin_password": password}} if can_sign_in else None,
        initial_actions=[{"navigate": {"url": build_linkedin_search_url(search_keywords, location, easy_apply_only), "new_tab": False}}],
        directly_open_url=False,
        register_new_step_callback=observe_step,
        max_failures=10,
    )


console = Console()


def _build_azure_llm(deployment: Optional[str] = None) -> Any:
    from browser_use import ChatAzureOpenAI

    settings = get_settings()
    deployment = deployment or settings.azure_openai_deployment
    return ChatAzureOpenAI(
        model=deployment,
        azure_deployment=deployment,
        api_key=settings.azure_openai_api_key,
        azure_endpoint=settings.azure_openai_endpoint,
        api_version=settings.azure_openai_api_version,
    )


def get_llm(model_name: Optional[str] = None) -> Any:
    """The primary chat model, chosen by LLM_PROVIDER (azure | gemini | openai)."""
    settings = get_settings()
    provider, openai_key, gemini_key = settings.llm_provider, settings.openai_api_key, settings.google_api_key

    if provider in ("azure", "azure_openai") and settings.azure_openai_configured:
        return _build_azure_llm(model_name)
    if (provider in ("gemini", "google") or not openai_key) and gemini_key:
        from browser_use import ChatGoogle
        return ChatGoogle(model=model_name or settings.llm_model or settings.default_generation_model or "gemini-2.5-flash", api_key=gemini_key)
    if openai_key and (provider == "openai" or not gemini_key):
        from browser_use import ChatOpenAI
        return ChatOpenAI(model=model_name or settings.llm_model or "gpt-4o", api_key=openai_key)
    raise ValueError(
        f"No usable LLM configuration found for LLM_PROVIDER='{provider}'. Set AZURE_OPENAI_API_KEY, "
        "AZURE_OPENAI_ENDPOINT and AZURE_OPENAI_DEPLOYMENT (or GOOGLE_API_KEY / OPENAI_API_KEY) in your .env file."
    )


def get_fallback_llm() -> Optional[Any]:
    """Model used automatically when the primary one is rate-limited."""
    settings = get_settings()
    provider, openai_key, gemini_key = settings.llm_provider, settings.openai_api_key, settings.google_api_key

    if provider in ("azure", "azure_openai") and settings.azure_openai_configured:
        return _build_azure_llm(settings.azure_openai_fallback_deployment or None)  # same deployment unless a second one is set
    if (provider in ("gemini", "google") or not openai_key) and gemini_key:
        from browser_use import ChatGoogle
        return ChatGoogle(model=settings.fallback_llm_model, api_key=gemini_key)
    if openai_key:
        from browser_use import ChatOpenAI
        return ChatOpenAI(model="gpt-4o-mini", api_key=openai_key)
    return None


@dataclass
class RunSpec:
    """Everything one platform run needs."""
    repo: Repository
    user_id: str
    run_id: str
    platform: str  # 'linkedin' | 'indeed'
    keywords: str
    location: str
    limit: int
    dry_run: bool
    headless: bool
    profile: CandidateProfile
    min_match_score: int
    resume_path: str
    credentials: Optional[Dict[str, Any]] = None  # {"username", "password", "cookies"}
    input_handler: Optional[Callable[[str], str]] = None


async def execute_job_application(spec: RunSpec) -> Dict[str, Any]:
    """Runs the agent until it has applied to `spec.limit` jobs (or run out of matching jobs). Returns run statistics."""
    llm = get_llm()
    fallback_llm = get_fallback_llm()
    credentials = spec.credentials or {}

    browser_profile = create_browser_profile(spec.user_id, spec.platform, headless=spec.headless, cookies=credentials.get("cookies"))
    ctx = RunContext(
        user_id=spec.user_id, run_id=spec.run_id, platform=spec.platform, dry_run=spec.dry_run, resume_path=spec.resume_path,
        profile=spec.profile, min_match_score=spec.min_match_score, max_evaluations=max(12, spec.limit * 4),
        target_applications=spec.limit, repo=spec.repo, llm=llm, input_handler=spec.input_handler,
    )
    token = set_run_context(ctx)
    agent = None
    try:
        common: Dict[str, Any] = dict(
            browser_profile=browser_profile, llm=llm, profile=spec.profile, search_keywords=spec.keywords, location=spec.location,
            resume_path=spec.resume_path, max_jobs=spec.limit, min_score=spec.min_match_score, max_evaluations=ctx.max_evaluations,
            credentials=credentials, fallback_llm=fallback_llm,
        )
        if spec.platform == "linkedin":
            agent = create_linkedin_agent(easy_apply_only=True, **common)
        elif spec.platform == "indeed":
            agent = create_indeed_agent(**common)
        else:
            raise ValueError(f"Unsupported platform '{spec.platform}'.")

        console.print(f"[bold green]Starting {spec.platform.upper()} agent for user {spec.user_id[:8]} "
                      f"(min match {spec.min_match_score}, dry run: {spec.dry_run})[/bold green]")
        history = await agent.run()
        return {"applied": ctx.applied, "evaluated": ctx.evaluations,
                "steps": len(history.history) if hasattr(history, "history") else None}
    finally:
        # Always shut the browser down so it cannot keep the profile locked for the next run.
        if agent is not None:
            try:
                await agent.close()
            except Exception:
                pass
        reset_run_context(token)
