"""History: every job the agent looked at - applied, skipped, failed - with filters, sorting and view modes."""

from __future__ import annotations
from datetime import date
from typing import Any, Dict, List, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from controllers.dependencies import current_user, get_repo, http_error
from services import history_service
from services.core_service import Repository
from models import HistoryItem, HistoryResponse, Outcome, Platform

router = APIRouter(prefix="/api/history", tags=["History"])


@router.get("", response_model=HistoryResponse, summary="Jobs the agent applied to, skipped or failed - with filters and view modes")
def history(
    platform: Optional[List[Platform]] = Query(None, description="Repeat to pick several: platform=indeed&platform=linkedin"),
    outcome: Optional[List[Outcome]] = Query(None, description="applied | skipped | failed | previewed | not_confirmed | other (repeatable)"),
    min_score: Optional[int] = Query(None, ge=0, le=100, description="Only jobs whose match score is at least this"),
    max_score: Optional[int] = Query(None, ge=0, le=100, description="Only jobs whose match score is at most this"),
    q: Optional[str] = Query(None, max_length=100, description="Text in the company name or job title"),
    date_from: Optional[date] = Query(None, description="YYYY-MM-DD, inclusive"),
    date_to: Optional[date] = Query(None, description="YYYY-MM-DD, inclusive"),
    run_id: Optional[str] = Query(None, description="Only jobs from one run"),
    sort: Literal["updated", "score", "company", "platform", "outcome"] = Query("updated", description="What to sort by"),
    order: Literal["asc", "desc"] = Query("desc"),
    limit: int = Query(25, ge=1, le=200, description="Page size"),
    offset: int = Query(0, ge=0, description="How many items to skip (paging)"),
    view: Literal["list", "grouped", "summary"] = Query("list", description="list: flat page | grouped: page grouped by platform | summary: counts only"),
    user: Dict[str, Any] = Depends(current_user),
    repo: Repository = Depends(get_repo),
):
    """
    The companies you applied to and the ones that were skipped (with the reason and match score), newest first.

    **Filter** by platform, outcome (applied / skipped / failed ...), score range and text. **View**: `list`, `grouped` by platform,
    or `summary` (counts only). The `summary` in every response counts everything that matches the filters, not just this page.
    Each item's `url` opens the job posting.
    """
    if min_score is not None and max_score is not None and min_score > max_score:
        raise HTTPException(422, "min_score cannot be greater than max_score")
    return history_service.query_history(
        repo, user["id"], run_id=run_id, platforms=list(dict.fromkeys(platform)) if platform else None,
        outcomes=list(dict.fromkeys(outcome)) if outcome else None, min_score=min_score, max_score=max_score, q=q,
        date_from=date_from.isoformat() if date_from else None, date_to=date_to.isoformat() if date_to else None,
        sort=sort, order=order, limit=limit, offset=offset, view=view,
    )


@router.get("/{item_id}", response_model=HistoryItem, summary="One job in full (score, reason, notes, link)")
def history_item(item_id: str, user: Dict[str, Any] = Depends(current_user), repo: Repository = Depends(get_repo)):
    try:
        return history_service.get_history_item(repo, user["id"], item_id)
    except LookupError as e:
        raise http_error(e)
