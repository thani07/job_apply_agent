""""Search and apply" runs: validates the user is ready, starts the browser agent in a background thread and tracks status."""

from __future__ import annotations
import shutil
import threading
from typing import Any, Callable, Dict, List, Optional
from services.core_service import BlobStore, Repository, new_id, new_worker_loop, utc_now
from config import get_settings
from services.credentials_service import get_login_material
from services.history_service import history_item
from services.profile_service import build_candidate, check_platforms_ready, download_active_resume, get_preferences, readiness
from models import ApplyRequest, RunDetail, RunOut, TaskOut


_INPUT_TIMEOUT_SECONDS = 600


class RunError(Exception):
    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.status_code = status_code


class _PendingInput:
    def __init__(self, user_id: str):
        self.user_id = user_id
        self.prompt: Optional[str] = None
        self.answer: Optional[str] = None
        self.event = threading.Event()


_lock = threading.Lock()


_live_runs: Dict[str, str] = {}          # user_id -> run_id of the run currently executing in this process


_inputs: Dict[str, _PendingInput] = {}   # run_id -> pending verification-code prompt


def _input_handler(run_id: str, user_id: str) -> Callable[[str], str]:
    pending = _PendingInput(user_id)
    _inputs[run_id] = pending

    def handler(prompt: str) -> str:
        pending.answer = None
        pending.event = threading.Event()
        pending.prompt = prompt
        answered = pending.event.wait(timeout=_INPUT_TIMEOUT_SECONDS)
        answer = pending.answer if answered else ""
        pending.prompt = None
        return answer or ""

    return handler


def submit_input(user_id: str, run_id: str, answer: str) -> None:
    pending = _inputs.get(run_id)
    if pending is None or pending.user_id != user_id or pending.prompt is None:
        raise RunError("The run is not waiting for any input.", 409)
    pending.answer = answer.strip()
    pending.event.set()


def start_run(repo: Repository, blob: BlobStore, user: Dict[str, Any], req: ApplyRequest) -> RunOut:
    user_id = user["id"]
    prefs = get_preferences(repo, user_id)
    search = prefs.default_search

    platforms = list(dict.fromkeys(req.platforms or search.platforms))
    keywords = (req.keywords or search.keywords).strip()
    location = (req.location or search.location).strip()
    limit = req.limit_per_platform or search.limit_per_platform
    min_score = prefs.min_match_score if req.min_match_score is None else req.min_match_score

    problems: List[str] = []
    if not keywords:
        problems.append("keywords (the role to search for)")
    if not location:
        problems.append("location")
    if not platforms:
        problems.append("platforms")
    state = readiness(repo, user_id, prefs)
    problems += [m for m in state.missing if m != "linkedin_or_indeed_credentials"]
    no_login = check_platforms_ready(repo, user_id, platforms)
    if no_login:
        problems.append("credentials for " + ", ".join(no_login))
    if problems:
        raise RunError("Cannot start yet. Missing: " + "; ".join(problems) + ".", 400)

    with _lock:
        if user_id in _live_runs:
            raise RunError("You already have a run in progress. Wait for it to finish.", 409)
        run_id = new_id()
        _live_runs[user_id] = run_id

    try:
        candidate = build_candidate(repo, user, blob=blob)
        work_dir = get_settings().data_dir / "users" / user_id / "runs" / run_id
        resume_path = str(download_active_resume(repo, blob, user_id, work_dir))
        credentials = {p: get_login_material(repo, user_id, p) for p in platforms}

        run = {
            "id": run_id, "user_id": user_id, "status": "pending", "platforms": platforms, "created_at": utc_now(), "finished_at": None,
            "params": {"keywords": keywords, "location": location, "limit_per_platform": limit, "min_match_score": min_score,
                       "dry_run": req.dry_run, "headless": req.headless},
            "tasks": [{"platform": p, "status": "pending", "error": None, "started_at": None, "finished_at": None} for p in platforms],
        }
        repo.save_run(run)
        handler = _input_handler(run_id, user_id)
    except BaseException:
        with _lock:
            _live_runs.pop(user_id, None)
        raise

    threading.Thread(
        target=_worker,
        args=(repo, run, candidate, resume_path, credentials, handler, work_dir),
        daemon=True,
        name=f"run-{run_id[:8]}",
    ).start()
    return _run_out(run)


def _worker(repo: Repository, run: Dict[str, Any], candidate, resume_path: str, credentials: Dict[str, Any],
            handler: Callable[[str], str], work_dir) -> None:
    """Runs each platform in turn. Blocks a dedicated thread; has its own event loop (needed for browser subprocesses on Windows)."""
    from services.agent_service import RunSpec, execute_job_application

    params = run["params"]
    run["status"] = "running"
    repo.save_run(run)
    loop = new_worker_loop()
    try:
        for task in run["tasks"]:
            task.update(status="running", started_at=utc_now())
            repo.save_run(run)
            spec = RunSpec(
                repo=repo, user_id=run["user_id"], run_id=run["id"], platform=task["platform"], keywords=params["keywords"],
                location=params["location"], limit=params["limit_per_platform"], dry_run=params["dry_run"], headless=params["headless"],
                profile=candidate, min_match_score=params["min_match_score"], resume_path=resume_path,
                credentials=credentials.get(task["platform"]), input_handler=handler,
            )
            try:
                loop.run_until_complete(execute_job_application(spec))
                task.update(status="completed")
            except Exception as e:  # noqa: BLE001 - one platform failing must not stop the next one
                task.update(status="failed", error=f"{type(e).__name__}: {str(e)[:300]}")
            task["finished_at"] = utc_now()
            repo.save_run(run)
        run["status"] = "failed" if any(t["status"] == "failed" for t in run["tasks"]) else "completed"
    except BaseException as e:  # noqa: BLE001
        run["status"] = "failed"
        for task in run["tasks"]:
            if task["status"] in ("pending", "running"):
                task.update(status="failed", error=f"{type(e).__name__}: {str(e)[:300]}", finished_at=utc_now())
    finally:
        run["finished_at"] = utc_now()
        try:
            repo.save_run(run)
        finally:
            try:
                loop.close()
            except Exception:
                pass
            shutil.rmtree(work_dir, ignore_errors=True)  # the temporary local copy of the resume
            _inputs.pop(run["id"], None)
            with _lock:
                if _live_runs.get(run["user_id"]) == run["id"]:
                    del _live_runs[run["user_id"]]


def _run_out(run: Dict[str, Any]) -> RunOut:
    pending = _inputs.get(run["id"])
    return RunOut(
        id=run["id"], status=run["status"], platforms=run["platforms"], params=run["params"], created_at=run["created_at"],
        finished_at=run.get("finished_at"), tasks=[TaskOut(**t) for t in run["tasks"]],
        pending_input=pending.prompt if pending else None,
    )


def _reconcile(repo: Repository, run: Dict[str, Any]) -> Dict[str, Any]:
    """A run that says 'running' but has no live thread in this process was interrupted by a restart."""
    if run["status"] in ("pending", "running") and _live_runs.get(run["user_id"]) != run["id"]:
        run["status"] = "failed"
        run["finished_at"] = run.get("finished_at") or utc_now()
        for task in run["tasks"]:
            if task["status"] in ("pending", "running"):
                task.update(status="failed", error="Interrupted (the server restarted).")
        repo.save_run(run)
    return run


def get_run(repo: Repository, user_id: str, run_id: str) -> RunDetail:
    run = repo.get_run(user_id, run_id)
    if not run:
        raise LookupError("Run not found.")
    run = _reconcile(repo, run)
    return RunDetail(run=_run_out(run), applications=[history_item(d) for d in repo.list_applications(user_id, run_id=run_id)])


def list_runs(repo: Repository, user_id: str, limit: int = 20) -> List[RunOut]:
    return [_run_out(_reconcile(repo, r)) for r in repo.list_runs(user_id, limit)]
