"""The signed-in user's profile: everything stored for them, and their preferences."""

from __future__ import annotations
from typing import Any, Dict

from fastapi import APIRouter, Depends, HTTPException

from controllers.dependencies import current_user, get_blob, get_repo
from services import profile_service
from services.core_service import BlobStore, Repository
from models import PreferencesUpdate, ProfileOut

router = APIRouter(prefix="/api/profile", tags=["Profile"])


@router.get("", response_model=ProfileOut, summary="Everything stored for the user")
def get_profile(user: Dict[str, Any] = Depends(current_user), repo: Repository = Depends(get_repo), blob: BlobStore = Depends(get_blob)):
    """
    The user, their preferences (notice period, expected salary, minimum match score, default search ...),
    LinkedIn/Indeed credential status (never the passwords), all resumes, the active resume with its extracted data and
    SAS links to view/download it, and what is still missing before the agent can apply.
    """
    return profile_service.build_profile(repo, blob, user)


@router.put("", response_model=ProfileOut, summary="Update preferences (only the fields you send change)")
def update_profile(body: PreferencesUpdate, user: Dict[str, Any] = Depends(current_user),
                   repo: Repository = Depends(get_repo), blob: BlobStore = Depends(get_blob)):
    try:
        profile_service.update_preferences(repo, user["id"], body)
    except ValueError as e:
        raise HTTPException(422, str(e))
    return profile_service.build_profile(repo, blob, user)
