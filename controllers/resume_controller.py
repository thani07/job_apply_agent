"""Resumes: upload (file to Blob Storage, data to Cosmos DB), list, view / download links, activate, re-read, delete."""

from __future__ import annotations
from typing import Any, Dict, List

from fastapi import APIRouter, Depends, File, Form, UploadFile, status

from controllers.dependencies import current_user, get_blob, get_repo, http_error
from services import profile_service
from services.core_service import BlobStore, ConfigError, Repository
from config import get_settings
from services.profile_service import ProfileError
from models import ResumeOut

router = APIRouter(prefix="/api/profile/resumes", tags=["Profile"])


@router.post("", response_model=ResumeOut, status_code=status.HTTP_201_CREATED, summary="Upload a resume (PDF or DOCX)")
def upload_resume(file: UploadFile = File(...), make_active: bool = Form(True), user: Dict[str, Any] = Depends(current_user),
                  repo: Repository = Depends(get_repo), blob: BlobStore = Depends(get_blob)):
    """Stores the file in the user's own blob container, extracts its data and returns SAS links to view/download it."""
    limit = get_settings().max_resume_bytes
    data = file.file.read(limit + 1)
    try:
        return profile_service.add_resume(repo, blob, user["id"], file.filename or "", data, make_active)
    except (ProfileError, ConfigError) as e:
        raise http_error(e)


@router.get("", response_model=List[ResumeOut], summary="List resumes with view/download links")
def list_resumes(user: Dict[str, Any] = Depends(current_user), repo: Repository = Depends(get_repo), blob: BlobStore = Depends(get_blob)):
    return profile_service.list_resumes(repo, blob, user["id"])


@router.get("/{resume_id}", response_model=ResumeOut, summary="One resume: extracted data + view/download links")
def get_resume(resume_id: str, user: Dict[str, Any] = Depends(current_user), repo: Repository = Depends(get_repo), blob: BlobStore = Depends(get_blob)):
    try:
        return profile_service.get_resume(repo, blob, user["id"], resume_id)
    except LookupError as e:
        raise http_error(e)


@router.put("/{resume_id}/active", status_code=status.HTTP_204_NO_CONTENT, summary="Use this resume for applications")
def activate_resume(resume_id: str, user: Dict[str, Any] = Depends(current_user), repo: Repository = Depends(get_repo)):
    try:
        profile_service.set_active_resume(repo, user["id"], resume_id)
    except LookupError as e:
        raise http_error(e)


@router.post("/{resume_id}/reparse", response_model=ResumeOut, summary="Extract the resume data again")
def reparse_resume(resume_id: str, user: Dict[str, Any] = Depends(current_user), repo: Repository = Depends(get_repo), blob: BlobStore = Depends(get_blob)):
    try:
        return profile_service.reparse_resume(repo, blob, user["id"], resume_id)
    except LookupError as e:
        raise http_error(e)


@router.delete("/{resume_id}", status_code=status.HTTP_204_NO_CONTENT, summary="Delete a resume (file and data)")
def delete_resume(resume_id: str, user: Dict[str, Any] = Depends(current_user), repo: Repository = Depends(get_repo), blob: BlobStore = Depends(get_blob)):
    try:
        profile_service.delete_resume(repo, blob, user["id"], resume_id)
    except LookupError as e:
        raise http_error(e)
