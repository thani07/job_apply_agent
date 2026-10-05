"""Autonomous search and apply: start a run, follow its progress, answer a verification-code prompt."""

from __future__ import annotations
from typing import Any, Dict, List

from fastapi import APIRouter, Body, Depends, status

from controllers.dependencies import current_user, get_blob, get_repo, http_error
from services import apply_service
from services.apply_service import RunError
from models import ApplyRequest, InputRequest, RunDetail, RunOut
from services.core_service import BlobStore, Repository
from services.profile_service import ProfileError

router = APIRouter(prefix="/api/apply", tags=["Search & Apply"])


@router.post("", response_model=RunOut, status_code=status.HTTP_202_ACCEPTED, summary="Search and apply")
def start_apply(body: ApplyRequest = Body(default_factory=ApplyRequest), user: Dict[str, Any] = Depends(current_user),
                repo: Repository = Depends(get_repo), blob: BlobStore = Depends(get_blob)):
    """
    Starts an autonomous run in the background and returns immediately - poll `GET /api/apply/runs/{id}`.

    Anything you leave out is taken from your profile (default search, minimum match score, ...). For every job the agent
    reads the requirements, scores how well you match (0-100, deliberately lenient) and applies **only if the score is at
    least your minimum**. It never asks for approval. Platforms run one after another.
    """
    try:
        return apply_service.start_run(repo, blob, user, body)
    except (RunError, ProfileError) as e:
        raise http_error(e)


@router.get("/runs", response_model=List[RunOut], summary="Your recent runs")
def list_runs(user: Dict[str, Any] = Depends(current_user), repo: Repository = Depends(get_repo)):
    return apply_service.list_runs(repo, user["id"])


@router.get("/runs/{run_id}", response_model=RunDetail, summary="Status of a run and the jobs it handled")
def get_run(run_id: str, user: Dict[str, Any] = Depends(current_user), repo: Repository = Depends(get_repo)):
    """Includes `pending_input` when the agent is waiting for a verification code (answer with POST .../input)."""
    try:
        return apply_service.get_run(repo, user["id"], run_id)
    except LookupError as e:
        raise http_error(e)


@router.post("/runs/{run_id}/input", status_code=status.HTTP_204_NO_CONTENT, summary="Answer a verification-code prompt")
def answer_prompt(run_id: str, body: InputRequest, user: Dict[str, Any] = Depends(current_user)):
    try:
        apply_service.submit_input(user["id"], run_id, body.answer)
    except RunError as e:
        raise http_error(e)
