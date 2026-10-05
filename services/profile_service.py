"""The user's profile: preferences, resumes (file in Blob Storage + extracted data in Cosmos DB), readiness to apply."""

from __future__ import annotations
import os
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional
from models import CandidateProfile, Preferences, PreferencesUpdate, ProfileOut, Readiness, ResumeOut, UserOut
from services.core_service import BlobStore, Repository, new_id, utc_now
from config import get_settings
from services.credentials_service import list_status
from services.resume_parser_service import apply_preferences, parse_resume


_CONTENT_TYPES = {".pdf": "application/pdf",
                  ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document"}


_MAGIC = {".pdf": b"%PDF", ".docx": b"PK\x03\x04"}


class ProfileError(ValueError):
    """User-correctable problem (bad file, missing data ...)."""


def _profile_doc(repo: Repository, user_id: str) -> Dict[str, Any]:
    return repo.get_profile(user_id) or {"id": user_id, "user_id": user_id, "preferences": {}, "active_resume_id": None}


def get_preferences(repo: Repository, user_id: str) -> Preferences:
    return Preferences(**_profile_doc(repo, user_id).get("preferences", {}))


def update_preferences(repo: Repository, user_id: str, update: PreferencesUpdate) -> Preferences:
    doc = _profile_doc(repo, user_id)
    merged = {**Preferences(**doc.get("preferences", {})).model_dump(), **update.model_dump(exclude_unset=True, exclude_none=True)}
    prefs = Preferences(**merged)  # re-validates the merged result
    doc.update(preferences=prefs.model_dump(), updated_at=utc_now())
    repo.save_profile(user_id, doc)
    return prefs


def _resume_out(blob: BlobStore, doc: Dict[str, Any], active_id: Optional[str], sas_days: int, include_parsed: bool) -> ResumeOut:
    links = blob.signed_links(doc["user_id"], doc["blob_name"], doc["filename"], doc["content_type"], sas_days)
    return ResumeOut(
        id=doc["id"], filename=doc["filename"], content_type=doc["content_type"], size_bytes=doc["size_bytes"],
        uploaded_at=doc["uploaded_at"], active=doc["id"] == active_id, view_url=links.view_url,
        download_url=links.download_url, sas_expires_at=links.expires_at,
        parsed=doc.get("parsed") if include_parsed else None,
    )


def _parse_bytes(data: bytes, suffix: str) -> Dict[str, Any]:
    fd, tmp = tempfile.mkstemp(suffix=suffix)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        return parse_resume(tmp).model_dump()
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass


def add_resume(repo: Repository, blob: BlobStore, user_id: str, filename: str, data: bytes, make_active: bool = True) -> ResumeOut:
    suffix = Path(filename or "").suffix.lower()
    if suffix not in _CONTENT_TYPES:
        raise ProfileError("Only .pdf and .docx resumes are supported.")
    if not data:
        raise ProfileError("The file is empty.")
    if len(data) > get_settings().max_resume_bytes:
        raise ProfileError(f"The resume is larger than {get_settings().max_resume_bytes // (1024 * 1024)} MB.")
    if not data.startswith(_MAGIC[suffix]):
        raise ProfileError(f"The file does not look like a real {suffix} document.")

    resume_id = new_id()
    blob_name = f"{resume_id}{suffix}"
    content_type = _CONTENT_TYPES[suffix]
    parsed = _parse_bytes(data, suffix)  # parse first: a corrupt file is rejected before anything is stored
    blob.upload(user_id, blob_name, data, content_type, metadata={"resume_id": resume_id})

    doc = {"id": resume_id, "user_id": user_id, "filename": Path(filename).name, "blob_name": blob_name, "content_type": content_type,
           "size_bytes": len(data), "uploaded_at": utc_now(), "parsed": parsed}
    repo.save_resume(doc)

    profile = _profile_doc(repo, user_id)
    if make_active or not profile.get("active_resume_id"):
        profile["active_resume_id"] = resume_id
        profile["updated_at"] = utc_now()
        repo.save_profile(user_id, profile)
    prefs = get_preferences(repo, user_id)
    return _resume_out(blob, doc, profile["active_resume_id"], prefs.sas_expiry_days, include_parsed=True)


def list_resumes(repo: Repository, blob: BlobStore, user_id: str, include_parsed: bool = False) -> List[ResumeOut]:
    active_id = _profile_doc(repo, user_id).get("active_resume_id")
    days = get_preferences(repo, user_id).sas_expiry_days
    return [_resume_out(blob, d, active_id, days, include_parsed) for d in repo.list_resumes(user_id)]


def get_resume(repo: Repository, blob: BlobStore, user_id: str, resume_id: str) -> ResumeOut:
    doc = repo.get_resume(user_id, resume_id)
    if not doc:
        raise LookupError("Resume not found.")
    active_id = _profile_doc(repo, user_id).get("active_resume_id")
    return _resume_out(blob, doc, active_id, get_preferences(repo, user_id).sas_expiry_days, include_parsed=True)


def set_active_resume(repo: Repository, user_id: str, resume_id: str) -> None:
    if not repo.get_resume(user_id, resume_id):
        raise LookupError("Resume not found.")
    profile = _profile_doc(repo, user_id)
    profile.update(active_resume_id=resume_id, updated_at=utc_now())
    repo.save_profile(user_id, profile)


def delete_resume(repo: Repository, blob: BlobStore, user_id: str, resume_id: str) -> None:
    doc = repo.get_resume(user_id, resume_id)
    if not doc:
        raise LookupError("Resume not found.")
    blob.delete(user_id, doc["blob_name"])
    repo.delete_resume(user_id, resume_id)
    profile = _profile_doc(repo, user_id)
    if profile.get("active_resume_id") == resume_id:
        remaining = repo.list_resumes(user_id)  # newest first
        profile.update(active_resume_id=remaining[0]["id"] if remaining else None, updated_at=utc_now())
        repo.save_profile(user_id, profile)


def reparse_resume(repo: Repository, blob: BlobStore, user_id: str, resume_id: str) -> ResumeOut:
    doc = repo.get_resume(user_id, resume_id)
    if not doc:
        raise LookupError("Resume not found.")
    doc["parsed"] = _parse_bytes(blob.download(user_id, doc["blob_name"]), Path(doc["blob_name"]).suffix)
    repo.save_resume(doc)
    return get_resume(repo, blob, user_id, resume_id)


def download_active_resume(repo: Repository, blob: BlobStore, user_id: str, dest_dir: Path) -> Path:
    """Copies the active resume from Blob Storage to a local file (the browser agent uploads from disk)."""
    resume_id = _profile_doc(repo, user_id).get("active_resume_id")
    doc = repo.get_resume(user_id, resume_id) if resume_id else None
    if not doc:
        raise ProfileError("Upload a resume first.")
    dest_dir.mkdir(parents=True, exist_ok=True)
    path = dest_dir / doc["filename"]
    path.write_bytes(blob.download(user_id, doc["blob_name"]))
    return path


def readiness(repo: Repository, user_id: str, prefs: Optional[Preferences] = None) -> Readiness:
    prefs = prefs or get_preferences(repo, user_id)
    missing: List[str] = []
    if prefs.notice_period_days is None:
        missing.append("notice_period_days")
    if prefs.expected_salary is None:
        missing.append("expected_salary")
    active_id = _profile_doc(repo, user_id).get("active_resume_id")
    if not active_id or not repo.get_resume(user_id, active_id):
        missing.append("resume")
    if not any(s.configured or s.cookies_saved for s in list_status(repo, user_id)):
        missing.append("linkedin_or_indeed_credentials")
    return Readiness(ready=not missing, missing=missing)


def build_profile(repo: Repository, blob: BlobStore, user: Dict[str, Any]) -> ProfileOut:
    user_id = user["id"]
    prefs = get_preferences(repo, user_id)
    resumes = list_resumes(repo, blob, user_id, include_parsed=False)
    active = next((r for r in resumes if r.active), None)
    active_full = get_resume(repo, blob, user_id, active.id) if active else None
    return ProfileOut(
        user=UserOut(id=user_id, email=user["email"], full_name=user["full_name"], created_at=user["created_at"]),
        preferences=prefs,
        credentials=list_status(repo, user_id),
        resumes=resumes,
        active_resume=active_full,
        readiness=readiness(repo, user_id, prefs),
    )


def build_candidate(repo: Repository, user: Dict[str, Any], blob: Optional[BlobStore] = None) -> CandidateProfile:
    """The active resume's parsed data plus the user's own preferences - what the browser agent fills into forms."""
    user_id = user["id"]
    resume_id = _profile_doc(repo, user_id).get("active_resume_id")
    doc = repo.get_resume(user_id, resume_id) if resume_id else None
    if not doc:
        raise ProfileError("Upload a resume first.")
    profile = CandidateProfile(**doc.get("parsed", {}))
    # A resume that could not be read completely still has to identify the person
    profile = profile.model_copy(update={
        "full_name": profile.full_name or user["full_name"],
        "email": profile.email or user["email"],
    })
    if not profile.first_name:
        parts = profile.full_name.split()
        profile = profile.model_copy(update={"first_name": parts[0] if parts else "", "last_name": parts[-1] if len(parts) > 1 else ""})
    prefs = get_preferences(repo, user_id)
    profile = apply_preferences(profile, prefs.model_dump())
    if blob and doc.get("blob_name"):
        try:
            links = blob.signed_links(
                user_id,
                doc["blob_name"],
                doc.get("filename", "resume.pdf"),
                doc.get("content_type", "application/pdf"),
                prefs.sas_expiry_days,
            )
            profile = profile.model_copy(update={"resume_url": links.view_url})
        except Exception:
            pass
    return profile


def check_platforms_ready(repo: Repository, user_id: str, platforms: List[str]) -> List[str]:
    """Names of requested platforms for which the user has neither credentials nor cookies."""
    status = {s.platform: s for s in list_status(repo, user_id)}
    return [p for p in platforms if p not in status or not (status[p].configured or status[p].cookies_saved)]
