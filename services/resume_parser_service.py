"""Resume reading: text extraction and LLM / heuristic parsing into a CandidateProfile."""

from __future__ import annotations
import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional
from models import CandidateProfile, Education, WorkExperience
from config import get_settings


def extract_text_from_file(file_path: str | Path) -> str:
    """Extract raw text from a PDF, DOCX or plain-text file."""
    path = Path(file_path)
    if not path.exists():
        raise FileNotFoundError(f"Resume file not found at: {file_path}")

    ext = path.suffix.lower()
    if ext == ".pdf":
        import pypdf
        reader = pypdf.PdfReader(str(path))
        return "\n".join(t for t in (page.extract_text() for page in reader.pages) if t)
    if ext in (".docx", ".doc"):
        import docx
        doc = docx.Document(str(path))
        return "\n".join(p.text for p in doc.paragraphs if p.text)
    if ext in (".txt", ".md"):
        return path.read_text(encoding="utf-8", errors="ignore")
    raise ValueError(f"Unsupported resume format '{ext}'. Supported formats: .pdf, .docx, .txt")


_COMMON_TECH = [
    "python", "javascript", "typescript", "react", "node.js", "next.js", "vue", "angular", "java", "c++", "c#", ".net",
    "go", "golang", "rust", "sql", "postgresql", "mongodb", "mysql", "redis", "docker", "kubernetes", "aws", "gcp",
    "azure", "ci/cd", "git", "rest api", "graphql", "machine learning", "pytorch", "tensorflow", "agile", "scrum",
    "microservices", "html", "css", "uipath", "rpa", "tkinter", "kivy", "django", "flask", "fastapi", "spring",
]


def extract_keywords_heuristically(raw_text: str) -> List[str]:
    """Fallback keyword extraction when the LLM is unavailable."""
    text_lower = raw_text.lower()
    return [
        tech.title() if len(tech) > 3 else tech.upper()
        for tech in _COMMON_TECH
        if re.search(r"(?<![\w.])" + re.escape(tech) + r"(?![\w])", text_lower)
    ]


_COUNTRY_CODES = {
    "india": "+91", "united states": "+1", "usa": "+1", "us": "+1", "canada": "+1", "united kingdom": "+44", "uk": "+44",
    "australia": "+61", "germany": "+49", "singapore": "+65", "uae": "+971", "united arab emirates": "+971",
}


def _safe_list(model, items) -> list:
    """Builds model instances from LLM output, skipping entries that are missing required fields."""
    out = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        try:
            out.append(model(**{k: v for k, v in item.items() if v is not None}))
        except Exception:
            continue
    return out


def _split_name(full_name: str) -> tuple[str, str]:
    parts = full_name.split()
    if not parts:
        return "", ""
    return parts[0], (parts[-1] if len(parts) > 1 else "")


def _llm_extract(raw_text: str) -> Optional[Dict[str, Any]]:
    """Asks the configured LLM for the profile as JSON. Returns None when no LLM is configured or it fails."""
    settings = get_settings()
    google_key = settings.google_api_key
    use_azure = settings.llm_provider in ("azure", "azure_openai") and settings.azure_openai_configured
    if not (use_azure or google_key):
        return None

    prompt = f"""Extract the candidate profile from this resume:
{raw_text[:12000]}

Return JSON strictly with the following fields. Use null (or [] for lists) for anything that is NOT in the resume - never invent data.
{{
  "full_name": "Full name of the candidate",
  "first_name": "First name",
  "last_name": "Last name",
  "email": "Email address",
  "phone": "Phone digits only, without country code",
  "phone_country_code": "e.g. +91, or null",
  "city": "City name",
  "country": "Country name (e.g. India)",
  "linkedin_url": "LinkedIn profile URL or null",
  "github_url": "GitHub profile URL or null",
  "portfolio_url": "Portfolio / website URL or null",
  "total_years_experience": 0.0,
  "primary_skills": ["Top 10 technical skills"],
  "all_keywords": ["Every technology, tool, framework, language and certification mentioned"],
  "experiences": [{{"title": "Job title", "company": "Company", "start_date": "Mon YYYY", "end_date": "Mon YYYY or Present", "skills": ["tech used"], "summary": "1-2 sentence description"}}],
  "education": [{{"degree": "Degree", "institution": "College / university", "graduation_year": "YYYY"}}]
}}
"""
    try:
        if use_azure:
            from openai import AzureOpenAI
            client = AzureOpenAI(
                api_key=settings.azure_openai_api_key,
                azure_endpoint=settings.azure_openai_endpoint,
                api_version=settings.azure_openai_api_version,
            )
            resp = client.chat.completions.create(
                model=settings.azure_openai_deployment,
                messages=[{"role": "user", "content": prompt}],
                response_format={"type": "json_object"},
            )
            return json.loads(resp.choices[0].message.content)
        from google import genai
        client = genai.Client(api_key=google_key)
        model_name = settings.llm_model or settings.default_generation_model or "gemini-2.5-flash"
        resp = client.models.generate_content(model=model_name, contents=prompt, config={"response_mime_type": "application/json"})
        return json.loads(resp.text)
    except Exception as e:  # noqa: BLE001 - fall back to heuristics
        print(f"Resume LLM extraction failed, using heuristics: {e}")
        return None


def _profile_from_llm(data: Dict[str, Any]) -> CandidateProfile:
    full_name = (data.get("full_name") or "").strip()
    first, last = data.get("first_name") or "", data.get("last_name") or ""
    if not first:
        first, last = _split_name(full_name)
    country = data.get("country") or None
    code = data.get("phone_country_code") or _COUNTRY_CODES.get((country or "").lower(), "")
    return CandidateProfile(
        full_name=full_name,
        first_name=first,
        last_name=last,
        email=(data.get("email") or "").strip(),
        phone=re.sub(r"\D", "", str(data.get("phone") or "")) or None,
        phone_country_code=code,
        city=data.get("city") or None,
        country=country,
        linkedin_url=data.get("linkedin_url") or None,
        github_url=data.get("github_url") or None,
        portfolio_url=data.get("portfolio_url") or None,
        primary_skills=[str(s) for s in (data.get("primary_skills") or [])],
        all_keywords=[str(k) for k in (data.get("all_keywords") or [])],
        total_years_experience=float(data.get("total_years_experience") or 0.0),
        experiences=_safe_list(WorkExperience, data.get("experiences")),
        education=_safe_list(Education, data.get("education")),
    )


def _profile_from_heuristics(raw_text: str) -> CandidateProfile:
    email = (re.search(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+", raw_text) or [""])[0]
    phone = None
    for candidate_number in re.findall(r"\+?\d[\d\s().-]{8,16}\d", raw_text):
        digits = re.sub(r"\D", "", candidate_number)
        if 10 <= len(digits) <= 13:  # 98765 43210, +91 98765 43210, (044) 2345 6789 ...
            phone = digits[-10:]
            break

    skip = {"skills", "contact", "education", "languages", "objective", "resume", "curriculum vitae", "summary", "profile"}
    full_name = ""
    for line in raw_text.splitlines():
        candidate = line.strip()
        if 3 <= len(candidate) <= 60 and "@" not in candidate and candidate.lower() not in skip and not any(c.isdigit() for c in candidate):
            full_name = candidate.title() if candidate.isupper() else candidate
            break
    first, last = _split_name(full_name)
    keywords = extract_keywords_heuristically(raw_text)
    return CandidateProfile(
        full_name=full_name,
        first_name=first,
        last_name=last,
        email=email,
        phone=phone,
        linkedin_url=(re.search(r"(?:https?://)?(?:www\.)?linkedin\.com/in/[\w\-%]+", raw_text, re.I) or [None])[0],
        github_url=(re.search(r"(?:https?://)?(?:www\.)?github\.com/[\w\-]+", raw_text, re.I) or [None])[0],
        primary_skills=keywords[:10],
        all_keywords=keywords,
    )


def parse_resume(file_path: str | Path, llm: Optional[Any] = None) -> CandidateProfile:
    """Parses a resume file into a CandidateProfile (LLM first, heuristics as a fallback). `llm` is unused, kept for callers."""
    raw_text = extract_text_from_file(file_path)
    data = _llm_extract(raw_text)
    if data:
        try:
            return _profile_from_llm(data)
        except Exception as e:  # noqa: BLE001
            print(f"Resume LLM result could not be used, using heuristics: {e}")
    return _profile_from_heuristics(raw_text)


def apply_preferences(profile: CandidateProfile, prefs: Dict[str, Any]) -> CandidateProfile:
    """Returns a copy of `profile` with the user's own preferences (notice period, salary, EEO answers ...) applied."""
    update = {
        k: prefs[k]
        for k in (
            "notice_period_days", "expected_salary", "current_salary", "salary_currency", "work_authorization",
            "requires_sponsorship", "willing_to_relocate", "gender", "disability_status", "veteran_status", "race_ethnicity",
        )
        if prefs.get(k) is not None
    }
    return profile.model_copy(update=update)
