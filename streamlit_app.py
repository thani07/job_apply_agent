"""
JobApply - Streamlit UI (a client of the JobApply API).

    python main.py                    # start the API first
    streamlit run streamlit_app.py    # then the UI

Screens: sign in / sign up, then Search & Apply, Profile (Details, Resumes, Credentials) and History.
"""

from __future__ import annotations
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))

from dotenv import load_dotenv

load_dotenv()

import pandas as pd
import requests
import streamlit as st
import streamlit.components.v1 as components


# ------------------------------------------------------------------------------------------------ API client

class ApiError(Exception):
    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


def _detail(response: requests.Response) -> str:
    try:
        detail = response.json().get("detail", response.text)
    except Exception:
        return response.text[:300] or response.reason
    if isinstance(detail, list):  # FastAPI validation errors
        return "; ".join(f"{'.'.join(str(p) for p in e.get('loc', [])[1:])}: {e.get('msg', '')}" for e in detail)
    return str(detail)


class ApiClient:
    """Thin HTTP client for the JobApply API."""

    def __init__(self, base_url: str, token: Optional[str] = None, refresh_token: Optional[str] = None):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.refresh_token = refresh_token

    def _request(self, method: str, path: str, timeout: float = 30, **kwargs: Any) -> Any:
        headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}
        try:
            response = requests.request(method, f"{self.base_url}{path}", headers=headers, timeout=timeout, **kwargs)
        except requests.ConnectionError as e:
            raise ApiError(0, f"Cannot reach the API at {self.base_url}. Is it running? (python main.py)") from e
        except requests.Timeout as e:
            raise ApiError(0, "The API took too long to answer.") from e

        # Automatic token refresh on 401 Unauthorized if refresh_token is available
        if response.status_code == 401 and self.refresh_token and path != "/api/auth/refresh":
            try:
                ref_resp = requests.post(f"{self.base_url}/api/auth/refresh",
                                         json={"refresh_token": self.refresh_token}, timeout=15)
                if ref_resp.status_code == 200:
                    ref_data = ref_resp.json()
                    self.token = ref_data["access_token"]
                    if ref_data.get("refresh_token"):
                        self.refresh_token = ref_data["refresh_token"]
                    st.session_state["token"] = self.token
                    st.session_state["refresh_token"] = self.refresh_token
                    headers["Authorization"] = f"Bearer {self.token}"
                    response = requests.request(method, f"{self.base_url}{path}", headers=headers, timeout=timeout, **kwargs)
            except Exception:
                pass

        if response.status_code >= 400:
            raise ApiError(response.status_code, _detail(response))
        return response.json() if response.content else None

    def refresh(self, refresh_token: str) -> Dict[str, Any]:
        return self._request("POST", "/api/auth/refresh", json={"refresh_token": refresh_token})

    def signup(self, email: str, full_name: str, password: str) -> Dict[str, Any]:
        return self._request("POST", "/api/auth/signup", json={"email": email, "full_name": full_name, "password": password})

    def login(self, email: str, password: str) -> Dict[str, Any]:
        return self._request("POST", "/api/auth/login", json={"email": email, "password": password})

    def profile(self) -> Dict[str, Any]:
        return self._request("GET", "/api/profile")

    def update_preferences(self, **fields: Any) -> Dict[str, Any]:
        return self._request("PUT", "/api/profile", json=fields)

    def upload_resume(self, filename: str, data: bytes, make_active: bool = True) -> Dict[str, Any]:
        return self._request("POST", "/api/profile/resumes", timeout=180, files={"file": (filename, data)}, data={"make_active": str(make_active).lower()})

    def resume(self, resume_id: str) -> Dict[str, Any]:
        return self._request("GET", f"/api/profile/resumes/{resume_id}")

    def activate_resume(self, resume_id: str) -> None:
        self._request("PUT", f"/api/profile/resumes/{resume_id}/active")

    def reparse_resume(self, resume_id: str) -> Dict[str, Any]:
        return self._request("POST", f"/api/profile/resumes/{resume_id}/reparse", timeout=180)

    def delete_resume(self, resume_id: str) -> None:
        self._request("DELETE", f"/api/profile/resumes/{resume_id}")

    def save_credentials(self, platform: str, username: str, password: str) -> Dict[str, Any]:
        return self._request("PUT", f"/api/credentials/{platform}", json={"username": username, "password": password})

    def delete_credentials(self, platform: str) -> None:
        self._request("DELETE", f"/api/credentials/{platform}")

    def test_credentials(self, platform: str, headless: bool = True, manual_wait_seconds: int = 20) -> Dict[str, Any]:
        return self._request("POST", f"/api/credentials/{platform}/test", timeout=manual_wait_seconds + 200,
                             json={"headless": headless, "manual_wait_seconds": manual_wait_seconds})

    def import_cookies(self, platform: str, cookies: Any) -> Dict[str, Any]:
        return self._request("POST", f"/api/credentials/{platform}/cookies", json=cookies)

    def start_apply(self, **body: Any) -> Dict[str, Any]:
        return self._request("POST", "/api/apply", json=body)

    def runs(self) -> List[Dict[str, Any]]:
        return self._request("GET", "/api/apply/runs")

    def run(self, run_id: str) -> Dict[str, Any]:
        return self._request("GET", f"/api/apply/runs/{run_id}")

    def answer_prompt(self, run_id: str, answer: str) -> None:
        self._request("POST", f"/api/apply/runs/{run_id}/input", json={"answer": answer})

    def history(self, **params: Any) -> Dict[str, Any]:
        """GET /api/history - filters, sorting, paging and view modes are all query parameters."""
        return self._request("GET", "/api/history", params={k: v for k, v in params.items() if v not in (None, "", [])})


API_BASE_URL = os.getenv("API_BASE_URL", "http://127.0.0.1:8000")
PLATFORMS = {"Indeed": "indeed", "LinkedIn": "linkedin"}
PLATFORM_LABEL = {v: k for k, v in PLATFORMS.items()}

st.set_page_config(page_title="JobApply", page_icon="💼", layout="wide")


# ----------------------------------------------------------------------------------------------- helpers

def api() -> ApiClient:
    return ApiClient(API_BASE_URL, st.session_state.get("token"), st.session_state.get("refresh_token"))


def logout() -> None:
    for key in list(st.session_state.keys()):
        del st.session_state[key]


def show_error(e: ApiError) -> None:
    """A 401 means the token expired: sign out. Anything else is shown to the user."""
    if e.status_code == 401:
        logout()
        st.rerun()
    st.error(e.detail)


def fmt_when(value: Optional[str]) -> str:
    if not value:
        return "—"
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone().strftime("%d %b %Y %H:%M")
    except ValueError:
        return value[:16]


def human_size(n: int) -> str:
    return f"{n / 1024:.0f} KB" if n < 1024 * 1024 else f"{n / (1024 * 1024):.1f} MB"


def status_label(status: str) -> str:
    s = (status or "").lower()
    if s == "submitted":
        return "✅ Applied"
    if "preview" in s or "dry_run" in s:
        return "👁 Previewed (not submitted)"
    if "not confirmed" in s:
        return "❓ Not confirmed (no submission page seen)"
    if "skip" in s:
        return "⏭ Skipped"
    if "fail" in s or "error" in s:
        return "❌ Failed"
    return status or "—"


def platform_root(platform: str) -> str:
    return (platform or "").split(" - ")[0].strip().lower()


def render_applications(rows: List[Dict[str, Any]], empty_message: str) -> None:
    """One table per platform: company, job, match score, status and a link to the job."""
    if not rows:
        st.info(empty_message)
        return
    by_platform: Dict[str, List[Dict[str, Any]]] = {}
    for r in sorted(rows, key=lambda r: r.get("updated_at", ""), reverse=True):
        by_platform.setdefault(platform_root(r.get("platform", "")) or "other", []).append(r)

    for key, items in by_platform.items():
        applied = sum(1 for r in items if str(r.get("status", "")).lower() == "submitted")
        st.markdown(f"#### {PLATFORM_LABEL.get(key, key.title())} — {applied} applied, {len(items)} looked at")
        frame = pd.DataFrame([
            {"Platform": r.get("platform", ""), "Company": r.get("company", ""), "Job title": r.get("job_title", ""),
             "Match": r.get("match_score"), "Status": status_label(r.get("status", "")), "When": fmt_when(r.get("updated_at")),
             "Application": r.get("url") or None, "Note": r.get("notes") or ""}
            for r in items
        ])
        st.dataframe(
            frame, hide_index=True, width="stretch",
            column_config={
                "Match": st.column_config.NumberColumn("Match /100", format="%d"),
                "Application": st.column_config.LinkColumn("View application", display_text="Open ↗"),
            },
        )


def _dash(value: Any) -> str:
    return str(value) if value not in (None, "", []) else "—"


def _chips(items: list) -> str:
    return "  ".join(f"`{i}`" for i in items) if items else "—"


def render_parsed(parsed: Dict[str, Any]) -> None:
    """Every field extracted from the resume. '—' means it was not found."""
    st.caption("Extracted from the resume by the LLM. '—' means it was not found in the resume.")
    st.markdown("##### Contact")
    a, b, c = st.columns(3)
    a.markdown(f"**Name:** {_dash(parsed.get('full_name'))}")
    a.markdown(f"**Email:** {_dash(parsed.get('email'))}")
    b.markdown(f"**Phone:** {_dash(parsed.get('phone_country_code'))} {_dash(parsed.get('phone'))}")
    b.markdown(f"**Location:** {_dash(parsed.get('city'))}, {_dash(parsed.get('country'))}")
    c.markdown(f"**LinkedIn:** {_dash(parsed.get('linkedin_url'))}")
    c.markdown(f"**GitHub:** {_dash(parsed.get('github_url'))}")
    c.markdown(f"**Portfolio:** {_dash(parsed.get('portfolio_url'))}")

    st.markdown("##### Skills")
    st.markdown(f"**Primary:** {_chips(parsed.get('primary_skills') or [])}")
    st.markdown(f"**All keywords ({len(parsed.get('all_keywords') or [])}):** {_chips(parsed.get('all_keywords') or [])}")

    st.markdown(f"##### Experience — {_dash(parsed.get('total_years_experience'))} years")
    for exp in parsed.get("experiences") or []:
        st.markdown(f"**{exp.get('title')}** — {exp.get('company')}  \n*{_dash(exp.get('start_date'))} → {_dash(exp.get('end_date'))}*")
        if exp.get("summary"):
            st.write(exp["summary"])
        if exp.get("skills"):
            st.markdown(_chips(exp["skills"]))
    if not parsed.get("experiences"):
        st.write("No work history found.")

    st.markdown("##### Education")
    if parsed.get("education"):
        st.dataframe(pd.DataFrame([{"Degree": e.get("degree"), "Institution": e.get("institution"), "Year": e.get("graduation_year")}
                                   for e in parsed["education"]]), hide_index=True, width="stretch")
    else:
        st.write("No education found.")


# --------------------------------------------------------------------------------------------- sign in

def auth_screen() -> None:
    st.title("💼 JobApply")
    st.caption("Sign in to search and apply for jobs automatically.")
    tab_login, tab_signup = st.tabs(["Log in", "Sign up"])

    with tab_login:
        with st.form("login"):
            email = st.text_input("Email", key="login_email")
            password = st.text_input("Password", type="password", key="login_password")
            if st.form_submit_button("Log in", type="primary"):
                try:
                    data = api().login(email, password)
                except ApiError as e:
                    st.error(e.detail)
                else:
                    st.session_state.update(token=data["access_token"], refresh_token=data.get("refresh_token"), user=data["user"])
                    st.rerun()

    with tab_signup:
        with st.form("signup"):
            name = st.text_input("Full name", key="signup_name")
            email = st.text_input("Email", key="signup_email")
            password = st.text_input("Password (at least 8 characters)", type="password", key="signup_password")
            again = st.text_input("Repeat password", type="password", key="signup_again")
            if st.form_submit_button("Create account", type="primary"):
                if password != again:
                    st.error("The passwords do not match.")
                else:
                    try:
                        data = api().signup(email, name, password)
                    except ApiError as e:
                        st.error(e.detail)
                    else:
                        st.session_state.update(token=data["access_token"], refresh_token=data.get("refresh_token"), user=data["user"])
                        st.rerun()


if not st.session_state.get("token"):
    auth_screen()
    st.stop()


# ---------------------------------------------------------------------------------------------- load data

try:
    profile = api().profile()
    active_run = next((r for r in api().runs() if r["status"] in ("pending", "running")), None)
except ApiError as e:
    show_error(e)
    st.stop()

prefs: Dict[str, Any] = profile["preferences"]
readiness = profile["readiness"]
MISSING_LABEL = {
    "notice_period_days": "Notice period (Profile → Preferences)",
    "expected_salary": "Expected salary (Profile → Preferences)",
    "resume": "A resume (Profile → Resumes)",
    "linkedin_or_indeed_credentials": "A LinkedIn or Indeed login (Profile → Credentials)",
}

with st.sidebar:
    st.header("💼 JobApply")
    st.write(f"**{profile['user']['full_name']}**")
    st.caption(profile["user"]["email"])
    if st.button("Log out"):
        logout()
        st.rerun()
    st.divider()
    if readiness["ready"]:
        st.success("Ready to apply")
    else:
        st.warning("Still needed before you can apply:")
        for item in readiness["missing"]:
            st.markdown(f"- {MISSING_LABEL.get(item, item)}")
    active_resume = profile.get("active_resume")
    st.caption("Active resume")
    st.write(f"📄 {active_resume['filename']}" if active_resume else "None yet")

tab_apply, tab_profile, tab_history = st.tabs(
    ["🔎 Search & Apply", "🧑 Profile", "📋 History"])


# ------------------------------------------------------------------------------------------ search & apply

def run_panel(run_id: str) -> None:
    """Live status + results of one run. Polls every few seconds while the run is active."""
    try:
        detail = api().run(run_id)
    except ApiError as e:
        show_error(e)
        return
    run, apps = detail["run"], detail["applications"]

    if run.get("pending_input"):
        st.warning(f"🔔 **Action required:** {run['pending_input']}")
        answer = st.text_input("Your answer / code", key=f"input_{run_id}")
        if st.button("Send", key=f"send_{run_id}") and answer.strip():
            try:
                api().answer_prompt(run_id, answer)
            except ApiError as e:
                st.error(e.detail)
            st.rerun(scope="fragment")

    p = run["params"]
    mode = "Preview only (nothing is submitted)" if p["dry_run"] else "Live — applications are submitted"
    st.markdown(f"**{p['keywords']}** in **{p['location']}** · up to {p['limit_per_platform']} per platform · "
                f"applies only at match score ≥ **{p['min_match_score']}** · {mode}")

    icons = {"pending": "⏳ Waiting", "running": "🔄 Running", "completed": "✅ Finished", "failed": "❌ Failed"}
    cols = st.columns(max(len(run["tasks"]), 1))
    for col, task in zip(cols, run["tasks"]):
        with col:
            st.metric(PLATFORM_LABEL.get(task["platform"], task["platform"]), icons.get(task["status"], task["status"]))
            if task.get("error"):
                st.caption(f"Error: {task['error'][:300]}")

    count = lambda pred: sum(1 for a in apps if pred(str(a.get("status", "")).lower()))  # noqa: E731
    applied, skipped = count(lambda s: s == "submitted"), count(lambda s: "skip" in s)
    previewed = count(lambda s: "preview" in s)
    not_done = len(apps) - applied - skipped - previewed
    summary = f"{applied} applied" + (f", {previewed} previewed" if previewed else "") + f", {skipped} skipped (low match)" + (f", {not_done} not completed" if not_done else "")

    if run["status"] in ("pending", "running"):
        st.info(f"The browser is working through the jobs. Leave this page open — this list updates by itself. So far: {summary}.")
    elif run["status"] == "completed":
        st.success(f"Run finished — {summary}.")
    else:
        st.error(f"Run finished with errors — {summary}.")

    render_applications(apps, "No jobs have been handled yet.")

    if run["status"] not in ("pending", "running") and st.session_state.get("was_running"):
        st.session_state["was_running"] = False
        st.rerun()


with tab_apply:
    running = active_run is not None
    default = prefs["default_search"]
    if not readiness["ready"]:
        st.warning("Before the first run, complete: " + ", ".join(MISSING_LABEL.get(m, m) for m in readiness["missing"]) + ".")

    with st.form("search_form"):
        c1, c2, c3 = st.columns([3, 3, 1.5])
        role = c1.text_input("Role", value=default["keywords"], placeholder="e.g. Python Developer")
        location = c2.text_input("Location", value=default["location"], placeholder="e.g. Chennai or Remote")
        limit = c3.number_input("Jobs to apply (per platform)", min_value=1, max_value=25, value=default["limit_per_platform"], step=1)

        c4, c5 = st.columns([3, 3])
        chosen = c4.multiselect("Platforms", list(PLATFORMS), default=[PLATFORM_LABEL[p] for p in default["platforms"]])
        min_score = c5.slider("Minimum match score", 0, 100, prefs["min_match_score"],
                              help="The agent scores every job 0-100 against your resume (leniently) and applies only when the score is at least this.")
        with st.expander("Options"):
            remember = st.checkbox("Remember role, location, number and platforms as my default search", value=False)
            preview_only = st.checkbox("Preview only — fill the forms but do NOT submit", value=False,
                                       help="Leave unchecked to complete and submit each application.")
            headless = st.checkbox("Hide the browser window (headless)", value=False,
                                   help="Keep this off so you can enter a verification code or solve a CAPTCHA if a site asks.")
        if not preview_only:
            st.caption("⚠️ Live mode: applications are **submitted for real**, without asking you. Jobs below your minimum score are skipped.")
        st.caption("ℹ️ Applies **only** to native Easy Apply (LinkedIn) & Apply now (Indeed). External site links and company websites are skipped automatically, with a 10s delay between submissions to avoid bot detection.")
        submitted = st.form_submit_button("Search and Apply", type="primary", disabled=running or not readiness["ready"])

    if running:
        st.caption("A run is in progress — the button is disabled until it finishes.")

    if submitted:
        if not role.strip() or not location.strip() or not chosen:
            st.error("Enter a role and a location, and pick at least one platform.")
        else:
            try:
                if remember:
                    api().update_preferences(default_search={"keywords": role.strip(), "location": location.strip(),
                                                             "limit_per_platform": int(limit), "platforms": [PLATFORMS[p] for p in chosen]})
                run = api().start_apply(keywords=role.strip(), location=location.strip(), limit_per_platform=int(limit),
                                        platforms=[PLATFORMS[p] for p in chosen], min_match_score=min_score,
                                        dry_run=preview_only, headless=headless)
                st.session_state.update(run_id=run["id"], was_running=True)
                st.rerun()
            except ApiError as e:
                show_error(e)

    run_id = active_run["id"] if active_run else st.session_state.get("run_id")
    if not run_id:
        try:
            recent = api().runs()
            run_id = recent[0]["id"] if recent else None
        except ApiError:
            run_id = None
    if run_id:
        if active_run:
            st.session_state["was_running"] = True
        st.divider()
        st.subheader("Progress & results")
        st.fragment(run_panel, run_every=3 if active_run else None)(run_id)


# ------------------------------------------------------------------------------------------------ profile

with tab_profile:
    tab_prefs, tab_resumes, tab_accounts = st.tabs(
        ["📝 Preferences & Details", "📄 Resumes", "🔐 Credentials"]
    )

    with tab_prefs:
        st.subheader("Your details")
        st.caption("The agent fills application forms from your resume plus the answers below. Notice period and expected salary are required.")
        with st.form("prefs_form"):
            c1, c2, c3, c4 = st.columns([2, 2.5, 1.2, 2.5])
            notice = c1.number_input("Notice period (days) *", min_value=0, max_value=365, value=prefs["notice_period_days"], step=5,
                                     placeholder="e.g. 30", help="0 = can join immediately")
            expected = c2.number_input("Expected salary (per year) *", min_value=0.0, value=prefs["expected_salary"], step=50000.0,
                                       format="%.0f", placeholder="e.g. 1200000")
            currency = c3.text_input("Currency", value=prefs["salary_currency"], max_chars=3)
            current = c4.number_input("Current salary (optional)", min_value=0.0, value=prefs["current_salary"], step=50000.0,
                                      format="%.0f", placeholder="leave empty if none")

            st.markdown("**Match score**")
            min_score_pref = st.slider("Only apply to jobs that match at least (out of 100)", 0, 100, prefs["min_match_score"],
                                       help="Scoring is lenient: related skills count, missing years matter little, 'nice to have' items never count against you.")
            sas_days = st.number_input("Resume link validity (days)", min_value=1, max_value=365, value=prefs["sas_expiry_days"],
                                       help="How long the view / download links of your resumes stay valid. 365 = 1 year.")

            st.markdown("**Default search**")
            d1, d2, d3 = st.columns([3, 3, 1.5])
            keywords = d1.text_input("Role", value=default["keywords"], key="pref_keywords", placeholder="e.g. Python Developer")
            loc = d2.text_input("Location", value=default["location"], key="pref_location", placeholder="e.g. Chennai")
            per_platform = d3.number_input("Jobs per platform", min_value=1, max_value=25, value=default["limit_per_platform"], key="pref_limit")
            plats = st.multiselect("Platforms", list(PLATFORMS), default=[PLATFORM_LABEL[p] for p in default["platforms"]], key="pref_platforms")

            st.markdown("**Screening answers**")
            s1, s2, s3 = st.columns(3)
            relocate = s1.checkbox("Willing to relocate", value=prefs["willing_to_relocate"])
            sponsorship = s2.checkbox("Need visa sponsorship", value=prefs["requires_sponsorship"])
            authorization = s3.text_input("Work authorization", value=prefs["work_authorization"])
            with st.expander("Equal-opportunity questions (optional)"):
                e1, e2, e3, e4 = st.columns(4)
                gender = e1.text_input("Gender", value=prefs["gender"])
                disability = e2.text_input("Disability", value=prefs["disability_status"])
                veteran = e3.text_input("Veteran status", value=prefs["veteran_status"])
                race = e4.text_input("Race / ethnicity", value=prefs["race_ethnicity"])
            save_prefs = st.form_submit_button("Save", type="primary")

        if save_prefs:
            if notice is None or not expected:
                st.error("Notice period and expected salary are required.")
            else:
                fields: Dict[str, Any] = dict(
                    notice_period_days=int(notice), expected_salary=float(expected), salary_currency=(currency or "INR").upper(),
                    min_match_score=int(min_score_pref), sas_expiry_days=int(sas_days), willing_to_relocate=relocate,
                    requires_sponsorship=sponsorship, work_authorization=authorization, gender=gender, disability_status=disability,
                    veteran_status=veteran, race_ethnicity=race,
                    default_search={"keywords": keywords.strip(), "location": loc.strip(), "limit_per_platform": int(per_platform),
                                    "platforms": [PLATFORMS[p] for p in plats] or ["indeed"]},
                )
                if current:
                    fields["current_salary"] = float(current)
                try:
                    api().update_preferences(**fields)
                except ApiError as e:
                    show_error(e)
                else:
                    st.success("Saved.")
                    st.rerun()

    with tab_resumes:
        st.subheader("Upload a resume")
        u1, u2 = st.columns([4, 2])
        uploaded = u1.file_uploader("PDF or DOCX (up to 5 MB)", type=["pdf", "docx"], key="resume_uploader")
        make_active = u2.checkbox("Use it for applications", value=True)
        if st.button("Save resume", disabled=uploaded is None):
            with st.spinner("Uploading and reading the resume…"):
                try:
                    api().upload_resume(uploaded.name, uploaded.getvalue(), make_active)
                except ApiError as e:
                    show_error(e)
                else:
                    st.rerun()

        st.divider()
        st.subheader("Your resumes")
        resumes = profile["resumes"]
        if not resumes:
            st.info("No resumes uploaded yet.")
        for r in resumes:
            rid = r["id"]
            with st.container(border=True):
                c_name, c_use, c_view, c_del = st.columns([5, 1.4, 1.4, 1.4])
                c_name.markdown(f"**{r['filename']}**" + ("  🟢 *active*" if r["active"] else ""))
                c_name.caption(f"{human_size(r['size_bytes'])} · uploaded {fmt_when(r['uploaded_at'])} · links valid until {fmt_when(r['sas_expires_at'])}")

                if c_use.button("Use", key=f"use_{rid}", disabled=r["active"]):
                    try:
                        api().activate_resume(rid)
                    except ApiError as e:
                        show_error(e)
                    st.rerun()

                is_open = st.session_state.get(f"open_{rid}", False)
                if c_view.button("Close" if is_open else "View", key=f"view_{rid}"):
                    st.session_state[f"open_{rid}"] = not is_open
                    st.rerun()

                if st.session_state.get(f"confirm_del_{rid}"):
                    if c_del.button("Confirm", key=f"yes_{rid}", type="primary"):
                        try:
                            api().delete_resume(rid)
                        except ApiError as e:
                            show_error(e)
                        for k in (f"confirm_del_{rid}", f"open_{rid}"):
                            st.session_state.pop(k, None)
                        st.rerun()
                elif c_del.button("Delete", key=f"del_{rid}"):
                    st.session_state[f"confirm_del_{rid}"] = True
                    st.rerun()

                if is_open:
                    try:
                        full = api().resume(rid)
                    except ApiError as e:
                        show_error(e)
                        continue
                    l1, l2, _ = st.columns([1.4, 1.4, 6])
                    l1.link_button("Open in new tab", full["view_url"])
                    l2.link_button("Download", full["download_url"])
                    tab_doc, tab_data = st.tabs(["📄 Resume", "🧾 Extracted data"])
                    with tab_doc:
                        if full["content_type"] == "application/pdf":
                            components.iframe(full["view_url"], height=780, scrolling=True)
                        else:
                            st.info("Word documents cannot be previewed here — use Download. The extracted data is in the next tab.")
                    with tab_data:
                        if st.button("Read again with the LLM", key=f"reparse_{rid}"):
                            with st.spinner("Reading the resume…"):
                                try:
                                    full = api().reparse_resume(rid)
                                except ApiError as e:
                                    show_error(e)
                        render_parsed(full.get("parsed") or {})

    with tab_accounts:
        st.subheader("LinkedIn & Indeed logins")
        st.caption("Passwords are encrypted before they are stored and are never shown again. Use “Test login” to check them — on success the "
                   "browser session (cookies) is saved and used for your runs.")
        by_platform = {c["platform"]: c for c in profile["credentials"]}

        for label, key in (("LinkedIn", "linkedin"), ("Indeed", "indeed")):
            cred = by_platform.get(key, {})
            with st.container(border=True):
                st.markdown(f"### {label}")
                lines = []
                if cred.get("username"):
                    lines.append(f"✅ Login credentials saved for **{cred['username']}**")
                elif cred.get("cookies_saved"):
                    lines.append("✅ Authenticated via imported session cookies")
                else:
                    lines.append("⚪ No login or cookies saved yet")

                if cred.get("cookies_saved"):
                    lines.append(f"🍪 Session saved ({cred.get('cookie_count', 0)} cookies, {fmt_when(cred.get('cookies_saved_at'))})")
                if cred.get("last_test_status"):
                    icon = {"success": "✅", "invalid_credentials": "❌", "verification_required": "⚠️"}.get(cred["last_test_status"], "❌")
                    lines.append(f"{icon} Last test {fmt_when(cred.get('last_tested_at'))}: {cred.get('last_test_message') or cred['last_test_status']}")
                st.markdown("  \n".join(lines))

                with st.form(f"cred_{key}"):
                    f1, f2 = st.columns(2)
                    username = f1.text_input("Email / username", value=cred.get("username") or "", key=f"user_{key}")
                    password = f2.text_input("Password", type="password", key=f"pass_{key}", placeholder="enter to save or change")
                    if st.form_submit_button(f"Save {label} login"):
                        if not username.strip() or not password:
                            st.error("Enter both the email and the password.")
                        else:
                            try:
                                api().save_credentials(key, username.strip(), password)
                            except ApiError as e:
                                show_error(e)
                            else:
                                st.rerun()

                t1, t2, t3 = st.columns([2, 2.5, 1.5])
                show_window = t1.checkbox("Show the browser window", value=False, key=f"show_{key}",
                                          help="Needed if the site asks for a CAPTCHA or a code — you can finish it in the window.")
                wait_s = t2.number_input("Wait for me to finish verification (s)", min_value=0, max_value=300, value=20,
                                         step=5, key=f"wait_{key}", disabled=not show_window)
                can_test = bool(cred.get("configured") or cred.get("cookies_saved"))
                if t3.button(f"Test {label} session", key=f"test_{key}", disabled=not can_test):
                    with st.spinner(f"Testing {label} session… this can take up to a minute"):
                        try:
                            st.session_state[f"result_{key}"] = api().test_credentials(key, headless=not show_window, manual_wait_seconds=int(wait_s))
                        except ApiError as e:
                            show_error(e)
                    st.rerun()

                result = st.session_state.pop(f"result_{key}", None)
                if result:
                    {"success": st.success, "invalid_credentials": st.error, "verification_required": st.warning}.get(result["status"], st.error)(result["message"])

                with st.expander("Or import cookies exported from your own browser"):
                    st.caption("Paste the JSON exported by a cookie extension while you are signed in to " + label + ".")
                    raw = st.text_area("Cookies (JSON)", key=f"cookies_{key}", height=120, label_visibility="collapsed")
                    if st.button("Import cookies", key=f"import_{key}", disabled=not raw.strip()):
                        try:
                            api().import_cookies(key, json.loads(raw))
                        except json.JSONDecodeError:
                            st.error("That is not valid JSON.")
                        except ApiError as e:
                            show_error(e)
                        else:
                            st.rerun()

                if cred.get("configured") or cred.get("cookies_saved"):
                    if st.button(f"Remove {label} login and session", key=f"remove_{key}"):
                        try:
                            api().delete_credentials(key)
                        except ApiError as e:
                            show_error(e)
                        else:
                            st.rerun()


# ------------------------------------------------------------------------------------------------ history

OUTCOME_CHOICES = {"Applied": "applied", "Skipped": "skipped", "Failed": "failed", "Previewed": "previewed", "Not confirmed": "not_confirmed"}
SORT_CHOICES = {"Date": "updated", "Match score": "score", "Company": "company", "Platform": "platform", "Outcome": "outcome"}


def history_frame(items: List[Dict[str, Any]]) -> pd.DataFrame:
    return pd.DataFrame([
        {"Company": i["company"], "Job title": i["job_title"], "Platform": PLATFORM_LABEL.get(i["platform"], i["platform"]),
         "Outcome": status_label(i["status"]), "Match": i.get("match_score"), "When": fmt_when(i.get("updated_at")), "View": i.get("url") or None}
        for i in items
    ])


HISTORY_COLUMNS = {
    "Match": st.column_config.ProgressColumn("Match /100", min_value=0, max_value=100, format="%d"),
    "View": st.column_config.LinkColumn("View", display_text="Open ↗"),
}


def history_detail(item: Dict[str, Any]) -> None:
    """Everything about one job: outcome, match score with the reason, notes and a link to open it."""
    with st.container(border=True):
        st.markdown(f"### {item['job_title']} — {item['company']}")
        d1, d2, d3 = st.columns(3)
        d1.markdown(f"**Platform:** {PLATFORM_LABEL.get(item['platform'], item['platform'])}" +
                    (f" ({item['platform_detail']})" if item.get("platform_detail") and item["platform_detail"].lower() != item["platform"] else ""))
        d2.markdown(f"**Outcome:** {status_label(item['status'])}")
        d3.markdown(f"**When:** {fmt_when(item.get('updated_at'))}")
        if item.get("match_score") is not None:
            st.progress(item["match_score"] / 100, text=f"Match score {item['match_score']}/100")
        if item.get("match_reason"):
            st.markdown(f"**Why:** {item['match_reason']}")
        if item.get("notes"):
            st.markdown(f"**Notes:** {item['notes']}")
        if item.get("url"):
            st.link_button("Open the job ↗", item["url"])


with tab_history:
    top_l, top_r = st.columns([6, 1])
    top_l.subheader("History — jobs applied to, skipped and more")
    if top_r.button("Refresh", key="refresh_history"):
        st.rerun()

    with st.container(border=True):
        st.markdown("**Filters**")
        f1, f2, f3 = st.columns([2, 3, 3])
        plat_sel = f1.multiselect("Platform", list(PLATFORMS), default=list(PLATFORMS), key="h_platform")
        out_sel = f2.multiselect("Outcome", list(OUTCOME_CHOICES), default=list(OUTCOME_CHOICES), key="h_outcome")
        score_range = f3.slider("Match score", 0, 100, (0, 100), key="h_score",
                                help="Jobs without a score are hidden while a range is set (for example ones that failed before being scored).")
        g1, g2 = st.columns([3, 2])
        text = g1.text_input("Search company or job title", key="h_query", placeholder="e.g. Zoho or Python")
        dates = g2.date_input("Date range", value=(), key="h_dates")

    with st.container(border=True):
        st.markdown("**View options**")
        v1, v2, v3, v4 = st.columns([2.6, 2, 2, 1.4])
        view_mode = v1.radio("View as", ["Table", "Cards", "By platform"], horizontal=True, key="h_view")
        sort_label = v2.selectbox("Sort by", list(SORT_CHOICES), key="h_sort")
        order = v3.radio("Order", ["Newest / highest first", "Oldest / lowest first"], key="h_order", horizontal=False)
        page_size = v4.selectbox("Per page", [10, 25, 50, 100], index=1, key="h_size")

    # any change of filter / view goes back to page 1
    signature = (tuple(plat_sel), tuple(out_sel), tuple(score_range), text, str(dates), view_mode, sort_label, order, page_size)
    if st.session_state.get("h_sig") != signature:
        st.session_state["h_sig"] = signature
        st.session_state["h_page"] = 1

    def fetch(page: int) -> Dict[str, Any]:
        chosen_dates = list(dates) if isinstance(dates, (list, tuple)) else ([dates] if dates else [])
        return api().history(
            platform=[PLATFORMS[p] for p in plat_sel] if 0 < len(plat_sel) < len(PLATFORMS) else None,
            outcome=[OUTCOME_CHOICES[o] for o in out_sel] if 0 < len(out_sel) < len(OUTCOME_CHOICES) else None,
            min_score=score_range[0] if score_range[0] > 0 else None,
            max_score=score_range[1] if score_range[1] < 100 else None,
            q=text.strip() or None,
            date_from=chosen_dates[0].isoformat() if chosen_dates else None,
            date_to=chosen_dates[1].isoformat() if len(chosen_dates) > 1 else None,
            sort=SORT_CHOICES[sort_label], order="desc" if order.startswith("Newest") else "asc",
            limit=page_size, offset=(page - 1) * page_size, view="grouped" if view_mode == "By platform" else "list",
        )

    try:
        page = st.session_state.get("h_page", 1)
        data = fetch(page)
        pages = max(1, -(-data["total"] // page_size))
        if page > pages:  # e.g. the page size was raised
            page = st.session_state["h_page"] = pages
            data = fetch(page)
    except ApiError as e:
        show_error(e)
        st.stop()

    summary = data["summary"]
    m1, m2, m3, m4, m5 = st.columns(5)
    m1.metric("Matching", summary["total"])
    m2.metric("✅ Applied", summary["applied"])
    m3.metric("⏭ Skipped", summary["skipped"])
    m4.metric("❌ Failed / unconfirmed", summary["failed"] + summary["not_confirmed"])
    m5.metric("Average match", f"{summary['avg_score']:.0f}" if summary["avg_score"] is not None else "—")

    if data["total"] == 0:
        st.info("Nothing matches these filters yet." if any([text, score_range != (0, 100), len(out_sel) < len(OUTCOME_CHOICES), len(plat_sel) < len(PLATFORMS)])
                else "Nothing yet — run Search & Apply first.")
    else:
        first, last = data["offset"] + 1, data["offset"] + (sum(len(g) for g in data["groups"].values()) if data.get("groups") else len(data["items"]))
        p1, p2, p3 = st.columns([1, 4, 1])
        if p1.button("◀ Previous", disabled=page <= 1, key="h_prev"):
            st.session_state["h_page"] = page - 1
            st.rerun()
        p2.markdown(f"<div style='text-align:center'>Showing {first}–{last} of {data['total']} · page {page} of {pages}</div>", unsafe_allow_html=True)
        if p3.button("Next ▶", disabled=page >= pages, key="h_next"):
            st.session_state["h_page"] = page + 1
            st.rerun()

        if view_mode == "Table":
            items = data["items"]
            event = st.dataframe(history_frame(items), hide_index=True, width="stretch", column_config=HISTORY_COLUMNS,
                                 on_select="rerun", selection_mode="single-row", key="h_table")
            selected = event.selection.rows if event and event.selection else []
            if selected and selected[0] < len(items):
                history_detail(items[selected[0]])
            else:
                st.caption("Select a row to see the details and open the job.")

        elif view_mode == "Cards":
            for item in data["items"]:
                with st.container(border=True):
                    c1, c2, c3 = st.columns([5, 2.5, 1.5])
                    c1.markdown(f"**{item['company']}**  \n{item['job_title']}  \n*{PLATFORM_LABEL.get(item['platform'], item['platform'])} · {fmt_when(item.get('updated_at'))}*")
                    c2.markdown(status_label(item["status"]))
                    if item.get("match_score") is not None:
                        c2.progress(item["match_score"] / 100, text=f"Match {item['match_score']}/100")
                    if item.get("url"):
                        c3.link_button("View ↗", item["url"])
                    if item.get("match_reason") or item.get("notes"):
                        with st.expander("Details"):
                            history_detail(item)

        else:  # By platform
            for key, group in (data.get("groups") or {}).items():
                st.markdown(f"#### {PLATFORM_LABEL.get(key, key.title())} — {len(group)} on this page")
                st.dataframe(history_frame(group), hide_index=True, width="stretch", column_config=HISTORY_COLUMNS)
