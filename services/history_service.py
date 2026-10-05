"""History: every job the agent looked at, with filters, sorting and view modes."""

from __future__ import annotations
from typing import Any, Dict, List, Optional
from services.core_service import Repository, platform_root
from models import HistoryItem, HistoryResponse, HistorySummary


def outcome_of(status: str) -> str:
    """Canonical outcome of a stored status: applied | skipped | failed | previewed | not_confirmed | other."""
    s = (status or "").lower()
    if s == "submitted":
        return "applied"
    if "not confirmed" in s:
        return "not_confirmed"
    if "skip" in s:
        return "skipped"
    if "preview" in s or "dry_run" in s:
        return "previewed"
    if "fail" in s or "error" in s:
        return "failed"
    return "other"


def history_item(d: Dict[str, Any]) -> HistoryItem:
    return HistoryItem(
        id=d["id"], run_id=d.get("run_id"), platform=platform_root(d.get("platform", "")), platform_detail=d.get("platform", ""),
        company=d.get("company", ""), job_title=d.get("job_title", ""), url=d.get("url") or None, status=d.get("status", ""),
        outcome=outcome_of(d.get("status", "")), match_score=d.get("match_score"), match_reason=d.get("match_reason"),
        notes=d.get("notes") or None, created_at=d.get("created_at"), updated_at=d.get("updated_at", ""),
    )


def _summarise(items: List[HistoryItem]) -> HistorySummary:
    summary = HistorySummary(total=len(items))
    scores = [i.match_score for i in items if i.match_score is not None]
    summary.avg_score = round(sum(scores) / len(scores), 1) if scores else None
    for i in items:
        setattr(summary, i.outcome, getattr(summary, i.outcome) + 1)
        counts = summary.by_platform.setdefault(i.platform or "other", {})
        counts[i.outcome] = counts.get(i.outcome, 0) + 1
    return summary


_SORT_KEYS = {
    "updated": lambda i: i.updated_at,
    "company": lambda i: i.company.lower(),
    "platform": lambda i: (i.platform, i.company.lower()),
    "outcome": lambda i: (i.outcome, i.company.lower()),
}


def query_history(repo: Repository, user_id: str, *, run_id: Optional[str] = None, platforms: Optional[List[str]] = None,
                  outcomes: Optional[List[str]] = None, min_score: Optional[int] = None, max_score: Optional[int] = None,
                  q: Optional[str] = None, date_from: Optional[str] = None, date_to: Optional[str] = None,
                  sort: str = "updated", order: str = "desc", limit: int = 25, offset: int = 0, view: str = "list") -> HistoryResponse:
    """
    Filters: platform(s), outcome(s), match-score range (jobs without a score are left out while a score filter is on),
    text in company / job title, date range (YYYY-MM-DD). Sorting, paging, and three views:
    list (flat page), grouped (page grouped by platform) and summary (counts only).
    """
    items = [history_item(d) for d in repo.list_applications(user_id, run_id=run_id, limit=2000)]
    if platforms:
        wanted = {p.lower() for p in platforms}
        items = [i for i in items if i.platform in wanted]
    if outcomes:
        items = [i for i in items if i.outcome in set(outcomes)]
    if min_score is not None or max_score is not None:
        items = [i for i in items if i.match_score is not None
                 and (min_score is None or i.match_score >= min_score) and (max_score is None or i.match_score <= max_score)]
    if q and q.strip():
        needle = q.strip().lower()
        items = [i for i in items if needle in i.company.lower() or needle in i.job_title.lower()]
    if date_from:
        items = [i for i in items if i.updated_at[:10] >= date_from]
    if date_to:
        items = [i for i in items if i.updated_at[:10] <= date_to]

    summary = _summarise(items)
    descending = order == "desc"
    if sort == "score":
        scored = sorted((i for i in items if i.match_score is not None), key=lambda i: i.match_score, reverse=descending)
        items = scored + [i for i in items if i.match_score is None]  # unscored jobs always last
    else:
        items = sorted(items, key=_SORT_KEYS.get(sort, _SORT_KEYS["updated"]), reverse=descending)

    total = len(items)
    page = items[offset:offset + limit]
    response = HistoryResponse(total=total, offset=offset, limit=limit, view=view, summary=summary)
    if view == "grouped":
        groups: Dict[str, List[HistoryItem]] = {}
        for i in page:
            groups.setdefault(i.platform or "other", []).append(i)
        response.groups = groups
    elif view == "list":
        response.items = page
    return response


def get_history_item(repo: Repository, user_id: str, item_id: str) -> HistoryItem:
    doc = repo.get_application(user_id, item_id)
    if not doc:
        raise LookupError("Not found.")
    return history_item(doc)
