"""
Pydantic models: every request and response payload of the API, plus the data shapes shared by the services
(resume data, job-match result).
"""

from __future__ import annotations
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, EmailStr, Field


# ====================================================================================================
# Shared
# ====================================================================================================


Platform = Literal["linkedin", "indeed"]


PLATFORMS: tuple[str, ...] = ("linkedin", "indeed")


# ====================================================================================================
# Authentication
# ====================================================================================================


class SignupRequest(BaseModel):
    email: EmailStr
    full_name: str = Field(..., min_length=1, max_length=100)
    password: str = Field(..., min_length=8, max_length=128, description="At least 8 characters")


class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(..., min_length=1, max_length=128)


class UserOut(BaseModel):
    id: str = Field(..., description="The user's uuid; everything the user owns is stored against it")
    email: str
    full_name: str
    created_at: str


class RefreshTokenRequest(BaseModel):
    refresh_token: str = Field(..., min_length=1, description="JWT refresh token to exchange for new access token")


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: Optional[str] = None
    token_type: str = "bearer"
    expires_at: str
    user: UserOut


# ====================================================================================================
# Preferences
# ====================================================================================================


class DefaultSearch(BaseModel):
    keywords: str = Field("", max_length=200, description="Role / keywords to search for, e.g. 'Python Developer'")
    location: str = Field("", max_length=200, description="e.g. 'Chennai' or 'Remote'")
    limit_per_platform: int = Field(3, ge=1, le=25, description="How many applications to submit per platform")
    platforms: List[Platform] = Field(default_factory=lambda: ["indeed"])


class Preferences(BaseModel):
    notice_period_days: Optional[int] = Field(None, ge=0, le=365, description="Notice period in days (0 = immediate). Required to apply.")
    expected_salary: Optional[float] = Field(None, gt=0, description="Expected annual salary. Required to apply.")
    current_salary: Optional[float] = Field(None, ge=0, description="Current annual salary (optional)")
    salary_currency: str = Field("INR", min_length=3, max_length=3)
    min_match_score: int = Field(70, ge=0, le=100, description="Only apply to jobs whose match score (0-100) is at least this")
    sas_expiry_days: int = Field(365, ge=1, le=365, description="How long resume view/download links stay valid (default 1 year)")
    willing_to_relocate: bool = True
    requires_sponsorship: bool = False
    work_authorization: str = Field("Authorized to work", max_length=100)
    gender: str = Field("Decline to self-identify", max_length=60)
    disability_status: str = Field("Decline to self-identify", max_length=100)
    veteran_status: str = Field("Decline to self-identify", max_length=100)
    race_ethnicity: str = Field("Decline to self-identify", max_length=100)
    default_search: DefaultSearch = Field(default_factory=DefaultSearch)


class PreferencesUpdate(BaseModel):
    """Every field optional: only the fields you send are changed."""
    notice_period_days: Optional[int] = Field(None, ge=0, le=365)
    expected_salary: Optional[float] = Field(None, gt=0)
    current_salary: Optional[float] = Field(None, ge=0)
    salary_currency: Optional[str] = Field(None, min_length=3, max_length=3)
    min_match_score: Optional[int] = Field(None, ge=0, le=100)
    sas_expiry_days: Optional[int] = Field(None, ge=1, le=365)
    willing_to_relocate: Optional[bool] = None
    requires_sponsorship: Optional[bool] = None
    work_authorization: Optional[str] = Field(None, max_length=100)
    gender: Optional[str] = Field(None, max_length=60)
    disability_status: Optional[str] = Field(None, max_length=100)
    veteran_status: Optional[str] = Field(None, max_length=100)
    race_ethnicity: Optional[str] = Field(None, max_length=100)
    default_search: Optional[DefaultSearch] = None


# ====================================================================================================
# Platform credentials
# ====================================================================================================


class CredentialsIn(BaseModel):
    username: str = Field(..., min_length=1, max_length=200, description="Login e-mail / username for the platform")
    password: str = Field(..., min_length=1, max_length=200)


class TestLoginRequest(BaseModel):
    headless: bool = Field(True, description="Set false to watch the browser (needed if you must solve a CAPTCHA / enter a code)")
    manual_wait_seconds: int = Field(20, ge=0, le=300, description="With headless=false: how long to wait for you to finish a verification step")


class CredentialStatus(BaseModel):
    platform: Platform
    configured: bool
    username: Optional[str] = None
    cookies_saved: bool = False
    cookie_count: int = 0
    cookies_saved_at: Optional[str] = None
    last_tested_at: Optional[str] = None
    last_test_status: Optional[str] = None
    last_test_message: Optional[str] = None


class TestLoginResult(BaseModel):
    platform: Platform
    status: Literal["success", "invalid_credentials", "verification_required", "error"]
    message: str
    cookies_saved: bool = False
    cookie_count: int = 0


# ====================================================================================================
# Resumes and profile
# ====================================================================================================


class ResumeOut(BaseModel):
    id: str
    filename: str
    content_type: str
    size_bytes: int
    uploaded_at: str
    active: bool
    view_url: str = Field(..., description="Blob URL + SAS token that opens the file inline (preview)")
    download_url: str = Field(..., description="Blob URL + SAS token that downloads the file")
    sas_expires_at: str
    parsed: Optional[Dict[str, Any]] = Field(None, description="Data extracted from the resume (skills, experience, education ...)")


class Readiness(BaseModel):
    ready: bool
    missing: List[str] = Field(default_factory=list, description="What still has to be filled in before applying")


class ProfileOut(BaseModel):
    user: UserOut
    preferences: Preferences
    credentials: List[CredentialStatus]
    resumes: List[ResumeOut]
    active_resume: Optional[ResumeOut] = None
    readiness: Readiness


# ====================================================================================================
# History
# ====================================================================================================


Outcome = Literal["applied", "skipped", "failed", "previewed", "not_confirmed", "other"]


OUTCOMES: tuple[str, ...] = ("applied", "skipped", "failed", "previewed", "not_confirmed", "other")


class HistoryItem(BaseModel):
    """One job the agent looked at."""
    id: str
    run_id: Optional[str] = None
    platform: str = Field(..., description="indeed | linkedin")
    platform_detail: str = Field("", description="e.g. 'Indeed - Company Site'")
    company: str
    job_title: str
    url: Optional[str] = Field(None, description="Link to view the job posting / application")
    status: str = Field(..., description="Submitted | Skipped | Failed | Previewed | Not confirmed")
    outcome: Outcome = Field(..., description="applied | skipped | failed | previewed | not_confirmed | other")
    match_score: Optional[int] = Field(None, description="0-100 match between your resume and the job's requirements")
    match_reason: Optional[str] = None
    notes: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: str


class HistorySummary(BaseModel):
    """Counts over everything that matches the filters (not just the current page)."""
    total: int = 0
    applied: int = 0
    skipped: int = 0
    failed: int = 0
    previewed: int = 0
    not_confirmed: int = 0
    other: int = 0
    avg_score: Optional[float] = None
    by_platform: Dict[str, Dict[str, int]] = Field(default_factory=dict, description="platform -> outcome -> count")


class HistoryResponse(BaseModel):
    total: int = Field(..., description="How many items match the filters")
    offset: int
    limit: int
    view: str
    summary: HistorySummary
    items: List[HistoryItem] = Field(default_factory=list, description="view=list: the current page")
    groups: Optional[Dict[str, List[HistoryItem]]] = Field(None, description="view=grouped: the current page grouped by platform")


# ====================================================================================================
# Search and apply
# ====================================================================================================


class InputRequest(BaseModel):
    answer: str = Field(..., min_length=1, max_length=200)


class ApplyRequest(BaseModel):
    """Every field is optional - anything left out comes from the user's default search / preferences."""
    platforms: Optional[List[Platform]] = None
    keywords: Optional[str] = Field(None, max_length=200)
    location: Optional[str] = Field(None, max_length=200)
    limit_per_platform: Optional[int] = Field(None, ge=1, le=25)
    min_match_score: Optional[int] = Field(None, ge=0, le=100, description="Override the profile's minimum match score for this run")
    headless: bool = Field(False, description="Keep false so you can enter a verification code if a site asks")
    dry_run: bool = Field(False, description="true = fill forms but never press the final submit button")


class TaskOut(BaseModel):
    platform: str
    status: str = Field(..., description="pending | running | completed | failed")
    error: Optional[str] = None
    started_at: Optional[str] = None
    finished_at: Optional[str] = None


class RunOut(BaseModel):
    id: str
    status: str = Field(..., description="pending | running | completed | failed")
    platforms: List[str]
    params: Dict[str, Any]
    created_at: str
    finished_at: Optional[str] = None
    tasks: List[TaskOut]
    pending_input: Optional[str] = Field(None, description="If set, the agent is waiting for you (e.g. an emailed verification code) - answer via POST /input")


class RunDetail(BaseModel):
    run: RunOut
    applications: List[HistoryItem]


# ====================================================================================================
# Resume data (what the agent fills into application forms)
# ====================================================================================================


class WorkExperience(BaseModel):
    title: str = Field(..., description="Job title")
    company: str = Field(..., description="Company name")
    start_date: Optional[str] = Field(None, description="Start date (e.g. Month Year)")
    end_date: Optional[str] = Field(None, description="End date or 'Present'")
    skills: List[str] = Field(default_factory=list, description="Technologies/skills used in this role")
    summary: Optional[str] = Field(None, description="Key accomplishments or description")


class Education(BaseModel):
    degree: str = Field(..., description="Degree obtained or pursued (e.g., B.S. Computer Science)")
    institution: str = Field(..., description="University or college name")
    graduation_year: Optional[str] = Field(None, description="Year of graduation")


def _num(value: float) -> str:
    """1200000.0 -> '1200000', 2.5 -> '2.5' (never scientific notation, which would confuse the agent)."""
    return f"{value:.2f}".rstrip("0").rstrip(".")


class CandidateProfile(BaseModel):
    # Identity & contact (from the resume)
    full_name: str = Field("", description="Candidate's full name")
    first_name: str = ""
    last_name: str = ""
    email: str = ""
    phone: Optional[str] = Field(None, description="Phone number, digits only")
    phone_country_code: str = Field("", description="International dialling code, e.g. +91")
    city: Optional[str] = None
    country: Optional[str] = None
    linkedin_url: Optional[str] = None
    github_url: Optional[str] = None
    portfolio_url: Optional[str] = None
    resume_url: Optional[str] = Field(None, description="Direct Blob Storage SAS URL for viewing/downloading the candidate's resume")

    # Skills & career (from the resume)
    primary_skills: List[str] = Field(default_factory=list)
    all_keywords: List[str] = Field(default_factory=list)
    total_years_experience: float = 0.0
    experiences: List[WorkExperience] = Field(default_factory=list)
    education: List[Education] = Field(default_factory=list)

    # Preferences (from the user's profile - never guessed from the resume)
    notice_period_days: Optional[int] = None
    expected_salary: Optional[float] = None
    current_salary: Optional[float] = None
    salary_currency: str = "INR"
    work_authorization: str = "Authorized to work"
    requires_sponsorship: bool = False
    willing_to_relocate: bool = True
    gender: str = "Decline to self-identify"
    disability_status: str = "Decline to self-identify"
    veteran_status: str = "Decline to self-identify"
    race_ethnicity: str = "Decline to self-identify"

    def summary_for_agent(self) -> str:
        """Plain-text candidate summary injected into the browser agent's prompt. Built only from real data."""
        lines = [
            f"Candidate Full Name: {self.full_name or 'N/A'}",
            f"First Name: {self.first_name or 'N/A'} | Last Name: {self.last_name or 'N/A'}",
            f"Email: {self.email or 'N/A'} | Phone: {self.phone or 'N/A'}",
            f"Phone Country Code: {self.phone_country_code or 'N/A'} (Country: {self.country or 'N/A'})",
            f"Location: {', '.join(x for x in (self.city, self.country) if x) or 'N/A'}",
            f"Resume SAS URL (Direct Resume / CV Link): {self.resume_url or 'N/A'}",
            f"LinkedIn: {self.linkedin_url or 'N/A'} | GitHub: {self.github_url or 'N/A'} | Portfolio: {self.portfolio_url or 'N/A'}",
            f"Total Years of Professional Experience: {_num(self.total_years_experience)}",
        ]
        if self.education:
            lines.append("Education:")
            for e in self.education:
                lines.append(f" - {e.degree}, {e.institution}" + (f" ({e.graduation_year})" if e.graduation_year else ""))
        if self.experiences:
            lines.append("Work Experience:")
            for x in self.experiences:
                span = " - ".join(p for p in (x.start_date, x.end_date) if p)
                lines.append(f" - {x.title} at {x.company}" + (f" ({span})" if span else ""))
                if x.summary:
                    lines.append(f"   {x.summary}")
        lines.append(f"Primary Skills: {', '.join(self.primary_skills) or 'N/A'}")
        others = [k for k in self.all_keywords if k not in self.primary_skills]
        if others:
            lines.append(f"Other Skills / Tools: {', '.join(others)}")

        lines.append("Screening Answers (use these; they come from the candidate's own profile):")
        lines.append(f" - Work authorization: {self.work_authorization}; requires visa sponsorship: {'Yes' if self.requires_sponsorship else 'No'}")
        lines.append(f" - Willing to relocate: {'Yes' if self.willing_to_relocate else 'No'}")
        if self.notice_period_days is not None:
            months = _num(self.notice_period_days / 30)
            lines.append(f" - Notice period: {self.notice_period_days} days (about {months} month(s)). "
                         f"For choice lists pick the option closest to that ('Immediate' if 0); for a number field enter {self.notice_period_days} days "
                         f"(or {months} if it asks for months).")
        else:
            lines.append(" - Notice period: not provided - choose the shortest reasonable option")
        cur = self.salary_currency
        if self.expected_salary is not None:
            lpa = f" (= {_num(self.expected_salary / 100000)} LPA)" if cur == "INR" else ""
            lines.append(f" - Expected salary / CTC: {_num(self.expected_salary)} {cur} per year{lpa}. For a numeric field enter {_num(self.expected_salary)}.")
        if self.current_salary is not None:
            lpa = f" (= {_num(self.current_salary / 100000)} LPA)" if cur == "INR" else ""
            lines.append(f" - Current salary / CTC: {_num(self.current_salary)} {cur} per year{lpa}.")
        else:
            lines.append(" - Current salary / CTC: not provided - if a number is required enter 0, otherwise leave it blank or 'Not applicable'")
        lines.append("Voluntary Disclosure / EEO answers (choose the matching option; 'Decline to self-identify' = 'I don't wish to answer'):")
        lines.append(f" - Gender: {self.gender} | Disability: {self.disability_status} | Veteran: {self.veteran_status} | Race/Ethnicity: {self.race_ethnicity}")
        return "\n".join(lines)


# ====================================================================================================
# Job match scoring
# ====================================================================================================


class MatchResult(BaseModel):
    score: int = Field(..., ge=0, le=100)
    matched: List[str] = Field(default_factory=list)
    missing: List[str] = Field(default_factory=list)
    reason: str = ""
