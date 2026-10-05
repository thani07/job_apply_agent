"""The browser agent's custom actions and helpers: job match scoring, dropdown / upload handling, application logging, per-run context."""

from __future__ import annotations
import asyncio
import json
import os
import re
import urllib.parse
from browser_use import Controller
from browser_use.browser.events import SwitchTabEvent
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from rich.console import Console
from typing import Any, Callable, Dict, List, Optional, Tuple
from services.core_service import Repository, new_id, platform_root
from models import CandidateProfile, MatchResult


_NUMBER_WORDS = {
    "one": "1", "two": "2", "three": "3", "four": "4", "five": "5", "six": "6",
    "seven": "7", "eight": "8", "nine": "9", "ten": "10", "twelve": "12",
}


_UNIT_DAYS = {"day": 1, "week": 7, "month": 30, "year": 365}


_PLACEHOLDERS = {"", "select", "choose", "-", "--", "none", "select an option", "select one", "please select",
                 "choose an option", "choose one", "select option", "pick one", "select...", "please choose"}


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9+]+", " ", (text or "").lower())).strip()


def is_placeholder(option_text: str) -> bool:
    n = _norm(option_text)
    return n in _PLACEHOLDERS or n.startswith(("select an", "please select", "choose an"))


def duration_days(text: str) -> Optional[float]:
    """'30 days' -> 30, '1 Month' -> 30, 'two weeks' -> 14, '15-30 days' -> 22.5, 'Immediate' -> 0. None if not a duration."""
    t = (text or "").lower()
    for word, digit in _NUMBER_WORDS.items():
        t = re.sub(rf"\b{word}\b", digit, t)
    if re.search(r"\b(immediate|immediately|asap|right away|no notice)\b", t):
        return 0.0
    m = re.search(r"(\d+(?:\.\d+)?)\s*(?:-|to|–)\s*(\d+(?:\.\d+)?)\s*(day|week|month|year)s?", t)
    if m:
        return (float(m.group(1)) + float(m.group(2))) / 2 * _UNIT_DAYS[m.group(3)]
    m = re.search(r"(\d+(?:\.\d+)?)\s*\+?\s*(day|week|month|year)s?", t)
    if m:
        return float(m.group(1)) * _UNIT_DAYS[m.group(2)]
    return None


def best_option(answer: str, options: List[str]) -> Tuple[Optional[int], str]:
    """
    Picks the option that best matches `answer`. Returns (index into options or None, how it matched).
    Order: exact -> yes/no -> duration -> all answer words present -> fuzzy.
    """
    a = _norm(answer)
    usable = [(i, o, _norm(o)) for i, o in enumerate(options) if not is_placeholder(o)]
    if not a or not usable:
        return None, "no options or empty answer"

    for i, o, n in usable:
        if n == a:
            return i, "exact"

    if a in ("yes", "no", "true", "false", "y", "n"):
        want = "yes" if a in ("yes", "true", "y") else "no"
        for i, o, n in usable:
            if n == want:
                return i, "yes/no"
        for i, o, n in usable:
            if n.startswith(want + " "):
                return i, "yes/no prefix"

    target_days = duration_days(answer)
    if target_days is not None:
        timed = [(i, duration_days(o)) for i, o, n in usable if duration_days(o) is not None]
        if timed:
            i, d = min(timed, key=lambda t: (abs(t[1] - target_days), -t[1]))
            return i, f"closest duration ({d:g} days vs {target_days:g})"

    a_tokens = set(a.split())
    contained = [(len(set(n.split()) - a_tokens), len(n), i) for i, o, n in usable if a_tokens <= set(n.split())]
    if contained:
        return min(contained)[2], "contains answer"
    reverse = [(len(n), i) for i, o, n in usable if n and set(n.split()) <= a_tokens]
    if reverse:
        return max(reverse)[1], "answer contains option"

    scored = sorted(((SequenceMatcher(None, a, n).ratio(), i) for i, o, n in usable), reverse=True)
    if scored and scored[0][0] >= 0.6:
        return scored[0][1], f"fuzzy {scored[0][0]:.2f}"
    return None, "no close match"


def choose_file_input(inputs: List[dict], hint: str = "resume") -> Optional[int]:
    """
    inputs: descriptors from JS_LIST_FILE_INPUTS ({i, disabled, accept, text}). Returns the `i` of the best input
    for `hint` ('resume', 'cv', 'cover letter'...), or None if there are none usable.
    """
    hint = (hint or "resume").lower()
    wants_cover = "cover" in hint
    best, best_score = None, -99
    for inp in inputs:
        if inp.get("disabled"):
            continue
        text = (inp.get("text") or "").lower()
        score = 0
        if hint in text:
            score += 4
        if not wants_cover and re.search(r"\b(resume|cv|curriculum)\b", text):
            score += 3
        if wants_cover and "cover" in text:
            score += 4
        if not wants_cover and "cover" in text:
            score -= 5
        if any(x in (inp.get("accept") or "").lower() for x in ("pdf", "doc")):
            score += 1
        if score > best_score:
            best, best_score = inp.get("i"), score
    return best


_DEEP = """
const deepAll = (root, sel, out = []) => {
  root.querySelectorAll(sel).forEach(e => out.push(e));
  root.querySelectorAll('*').forEach(e => { if (e.shadowRoot) deepAll(e.shadowRoot, sel, out); });
  return out;
};
"""


# Runs with `this` = the element the agent indexed; argument 0 = the desired answer. Reads (and, for custom
# dropdowns, opens) the option list and tags every option element with data-ja-opt="<n>".
JS_COLLECT_OPTIONS = """
async function(answer) {
""" + _DEEP + """
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  const fire = (n, type) => n.dispatchEvent(new MouseEvent(type, {bubbles: true, cancelable: true, view: window}));
  const visible = e => !!(e.offsetWidth || e.offsetHeight || e.getClientRects().length);
  const txt = e => (e.innerText || e.textContent || '').replace(/\\s+/g, ' ').trim();
  let el = this;
  if (el.tagName === 'OPTION') el = el.closest('select') || el;
  if (el.tagName === 'LABEL' && el.control) el = el.control;

  deepAll(document, '[data-ja-opt]').forEach(e => e.removeAttribute('data-ja-opt'));

  if (el.tagName === 'SELECT') {
    const opts = Array.from(el.options).map((o, i) => { o.setAttribute('data-ja-opt', i); return {text: o.text.trim(), disabled: o.disabled}; });
    return {kind: 'native', options: opts, current: el.selectedIndex >= 0 ? el.options[el.selectedIndex].text.trim() : ''};
  }

  if (el.tagName === 'INPUT' && (el.type === 'radio' || el.type === 'checkbox')) {
    const groupName = el.name;
    const group = (groupName ? deepAll(document, 'input[name="' + groupName + '"]') : [el]).filter(visible);
    const opts = group.map((r, i) => {
      r.setAttribute('data-ja-opt', i);
      const parentLabel = r.closest('label') || (r.id ? document.querySelector('label[for="' + r.id + '"]') : null) || r.parentElement;
      return {text: (parentLabel ? (parentLabel.innerText || '').trim() : r.value || '').replace(/\\s+/g, ' '), disabled: r.disabled};
    });
    return {kind: 'radio_group', options: opts, current: el.checked ? 'checked' : 'unchecked'};
  }

  const childRadios = deepAll(el, 'input[type=radio], input[type=checkbox]').filter(visible);
  if (childRadios.length > 0) {
    childRadios.forEach((r, i) => r.setAttribute('data-ja-opt', i));
    const opts = childRadios.map(r => {
      const parentLabel = r.closest('label') || (r.id ? document.querySelector('label[for="' + r.id + '"]') : null) || r.parentElement;
      return {text: (parentLabel ? (parentLabel.innerText || '').trim() : r.value || '').replace(/\\s+/g, ' '), disabled: r.disabled};
    });
    return {kind: 'radio_group', options: opts, current: ''};
  }

  const SEL = '[role=option], [role=menuitem], [role=menuitemradio], [role=treeitem], [role=listbox] > li, ul[role=listbox] > *, '
            + 'mat-option, .Select-option, [class*="select__option" i], [class*="dropdown-item" i], [class*="option" i]:not([class*="options" i]):not([class*="option-group" i])';
  const collect = () => {
    const all = deepAll(document, SEL).filter(e => visible(e) && txt(e) && txt(e).length < 140);
    // keep leaf-most candidates only (drop containers that hold another candidate)
    return all.filter(e => !all.some(o => o !== e && e.contains(o)));
  };

  let found = collect();
  if (!found.length) {
    el.focus && el.focus();
    fire(el, 'mousedown'); fire(el, 'mouseup'); fire(el, 'click');
    await sleep(500);
    found = collect();
  }
  if (!found.length && (el.tagName === 'INPUT' || el.isContentEditable)) {
    if (el.tagName === 'INPUT') {
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set.call(el, answer);
      el.dispatchEvent(new Event('input', {bubbles: true}));
    }
    await sleep(900);
    found = collect();
    if (!found.length && el.tagName === 'INPUT' && (el.type === 'text' || el.type === 'number' || !el.type)) {
      el.focus();
      el.dispatchEvent(new Event('change', {bubbles: true}));
      return {kind: 'input_field', options: [{text: answer, disabled: false}], current: answer};
    }
  }
  found.forEach((e, i) => e.setAttribute('data-ja-opt', i));
  return {kind: 'custom', options: found.map(e => ({text: txt(e), disabled: e.getAttribute('aria-disabled') === 'true'})),
          current: (el.value || txt(el)).slice(0, 120)};
}
"""


# Page-level. Format with {i} (option index) and {kind}.
JS_APPLY_OPTION = """
(function() {
""" + _DEEP + """
  const fire = (n, type) => n.dispatchEvent(new MouseEvent(type, {bubbles: true, cancelable: true, view: window}));
  const target = deepAll(document, '[data-ja-opt="%(i)d"]')[0];
  if (!target) return {ok: false, error: 'option element is gone (the list closed)'};
  if ('%(kind)s' === 'native') {
    const sel = target.closest('select');
    target.selected = true;
    sel.selectedIndex = target.index;
    sel.dispatchEvent(new Event('input', {bubbles: true}));
    sel.dispatchEvent(new Event('change', {bubbles: true}));
    return {ok: true, now: sel.options[sel.selectedIndex].text.trim()};
  }
  if ('%(kind)s' === 'radio_group') {
    target.scrollIntoView({block: 'nearest'});
    target.checked = true;
    fire(target, 'click');
    target.dispatchEvent(new Event('change', {bubbles: true}));
    const parentLabel = target.closest('label') || target.parentElement;
    return {ok: true, now: parentLabel ? (parentLabel.innerText || '').trim() : target.value};
  }
  if ('%(kind)s' === 'input_field') {
    return {ok: true, now: target ? target.value : ''};
  }
  target.scrollIntoView({block: 'nearest'});
  fire(target, 'mouseover'); fire(target, 'mousedown'); fire(target, 'mouseup'); fire(target, 'click');
  return {ok: true};
})()
"""


# Reads back what a custom dropdown shows after a selection (`this` = the dropdown element).
JS_READ_BACK = """
function() {
  let el = this;
  if (el.tagName === 'OPTION') el = el.closest('select') || el;
  if (el.tagName === 'LABEL' && el.control) el = el.control;
  return (el.value || el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim().slice(0, 120);
}
"""


# Page-level, pierces open shadow roots. Tags file inputs with data-ja-file and describes them.
JS_LIST_FILE_INPUTS = """
(function() {
""" + _DEEP + """
  return deepAll(document, 'input[type=file]').map((e, i) => {
    e.setAttribute('data-ja-file', i);
    const label = (e.labels && e.labels[0] && e.labels[0].innerText) || '';
    const box = e.closest('label, fieldset, section, [class*="upload" i], [class*="resume" i], div') || e.parentElement;
    const around = box ? (box.innerText || '').slice(0, 200) : '';
    const text = [e.id, e.name, e.getAttribute('aria-label'), e.getAttribute('data-testid'), e.className, label, around]
      .join(' ').toLowerCase().replace(/\\s+/g, ' ');
    return {i: i, disabled: e.disabled, multiple: e.multiple, accept: e.accept || '', text: text};
  });
})()
"""


JS_GET_FILE_INPUT = """
(function() {
""" + _DEEP + """
  return deepAll(document, '[data-ja-file="%(i)d"]')[0];
})()
"""


# Checks for string / URL input fields intended for a resume link / CV URL and fills it with the SAS URL
JS_FILL_RESUME_URL = """
(function() {
""" + _DEEP + """
  const urlInputs = deepAll(document, 'input[type=text], input[type=url], input:not([type]), textarea');
  const target = urlInputs.find(el => {
    const ph = (el.placeholder || '').toLowerCase();
    const name = (el.name || '').toLowerCase();
    const id = (el.id || '').toLowerCase();
    const label = (el.labels && el.labels[0] && el.labels[0].innerText || '').toLowerCase();
    const aria = (el.getAttribute('aria-label') || '').toLowerCase();
    const combined = ph + ' ' + name + ' ' + id + ' ' + label + ' ' + aria;
    return (combined.includes('resume') || combined.includes('cv')) &&
           (combined.includes('url') || combined.includes('link') || combined.includes('http') ||
            combined.includes('paste') || combined.includes('upload') || combined.includes('drive') ||
            combined.includes('dropbox') || combined.includes('file'));
  });
  if (target) {
    target.value = __RESUME_SAS_URL__;
    target.dispatchEvent(new Event('input', {bubbles: true}));
    target.dispatchEvent(new Event('change', {bubbles: true}));
    return {found: true, field: target.placeholder || target.name || target.id || 'resume URL text field'};
  }
  return {found: false};
})()
"""


MAX_REQUIREMENTS_CHARS = 6000


SYSTEM_PROMPT = """You screen job postings for a job seeker. Score from 0 to 100 how well the job's requirements fit the candidate.

Be LENIENT and generous - the candidate wants to apply widely and only wants to avoid clearly wrong jobs:
- Related or transferable skills COUNT as matches (React ~ Vue/Angular, Python ~ Django/Flask/FastAPI, SQL ~ any relational database, AWS ~ Azure/GCP, Java ~ Kotlin/Spring, manual/automation testing ~ QA tools ...).
- A shortfall in years of experience of up to 2 years (or any shortfall for junior / entry-level / fresher-friendly roles) lowers the score by at most 10 points. Internships, academic and personal projects count as experience.
- Items described as preferred, nice to have, plus, bonus or "good to have" NEVER lower the score.
- Ignore degree requirements when the skills or experience fit. Ignore location, salary, notice period, soft skills, company boilerplate and legal text.
- If the job title is in the same family as the candidate's background (developer / engineer / analyst ...) and about half or more of the core technical requirements are covered or related, the score is at least 70.
- If the description is vague, very short or missing, score 75.
- Score below 50 ONLY when the job is in a clearly different field or needs mostly skills the candidate has no trace of.
Return JSON: {"score": <integer 0-100>, "matched": [<short strings>], "missing": [<short strings>], "reason": "<one short sentence>"}"""


def candidate_for_scoring(profile: CandidateProfile) -> str:
    """The parts of the profile that matter for matching (no contact details or personal answers)."""
    lines = [f"Total experience: {profile.total_years_experience:g} years",
             f"Primary skills: {', '.join(profile.primary_skills) or 'n/a'}"]
    others = [k for k in profile.all_keywords if k not in profile.primary_skills]
    if others:
        lines.append(f"Other skills / tools: {', '.join(others)}")
    for e in profile.education:
        lines.append(f"Education: {e.degree}, {e.institution}" + (f" ({e.graduation_year})" if e.graduation_year else ""))
    for x in profile.experiences:
        span = " - ".join(p for p in (x.start_date, x.end_date) if p)
        lines.append(f"Experience: {x.title} at {x.company} {span}".strip() + (f" - {x.summary}" if x.summary else ""))
    return "\n".join(lines)


async def score_job(llm, profile: CandidateProfile, job_title: str, company: str, requirements: str,
                    attempts: int = 2, timeout: float = 60.0) -> Optional[MatchResult]:
    """Scores one job with the LLM. Returns None if the model could not be reached (the caller decides what to do)."""
    from browser_use.llm.messages import SystemMessage, UserMessage

    user = (f"CANDIDATE:\n{candidate_for_scoring(profile)}\n\n"
            f"JOB: {job_title} at {company}\nREQUIREMENTS / DESCRIPTION:\n{(requirements or '').strip()[:MAX_REQUIREMENTS_CHARS] or '(none provided)'}")
    for attempt in range(attempts):
        try:
            response = await asyncio.wait_for(
                llm.ainvoke([SystemMessage(content=SYSTEM_PROMPT), UserMessage(content=user)], output_format=MatchResult), timeout)
            return response.completion
        except Exception as e:  # noqa: BLE001 - network / rate limit / bad JSON
            print(f"Job scoring attempt {attempt + 1} failed: {type(e).__name__}: {str(e)[:160]}")
            await asyncio.sleep(1.5)
    return None


def meets_threshold(score: int, minimum: int) -> bool:
    """Apply when the job's match score is at least the user's minimum (70 vs 80 -> apply; 70 vs 50 -> skip)."""
    return score >= minimum


@dataclass
class RunContext:
    user_id: str
    run_id: str
    platform: str
    dry_run: bool
    resume_path: str
    profile: CandidateProfile
    min_match_score: int
    max_evaluations: int
    target_applications: int
    repo: Repository
    llm: Any = None  # scores jobs (any browser_use chat model)
    input_handler: Optional[Callable[[str], str]] = None  # answers "enter the verification code" prompts

    # --- mutable, per job ---
    last_job_url: str = ""
    confirmation_seen: str = ""
    pending_score: Optional[Tuple[int, str]] = None  # (score, reason) of the job currently being applied to

    # --- mutable, per run ---
    evaluations: int = 0
    applied: int = 0
    scores: Dict[str, Tuple[int, str]] = field(default_factory=dict)


_current: ContextVar[Optional[RunContext]] = ContextVar("run_context", default=None)


def set_run_context(ctx: RunContext) -> Token:
    return _current.set(ctx)


def reset_run_context(token: Token) -> None:
    _current.reset(token)


def get_run_context() -> RunContext:
    ctx = _current.get()
    if ctx is None:
        raise RuntimeError("No apply run is active in this context.")
    return ctx


console = Console()


controller = Controller()


# A record for the same job (title + company + platform) written again within this window is an update of the
# same application, not a new one.
_MERGE_WINDOW = timedelta(hours=2)


_CONFIRMATION_PATTERNS = (
    re.compile(r"your application (has been|was|is)?\s*(submitted|sent|received)", re.I),
    re.compile(r"application (has been|was|is)?\s*(submitted|sent|received)", re.I),
    re.compile(r"application\s*(submitted|sent|received)\s*to", re.I),
    re.compile(r"successfully (applied|submitted|sent)", re.I),
    re.compile(r"you('ve| have)? applied", re.I),
    re.compile(r"we('ve| have)? sent your application", re.I),
    re.compile(r"application (is )?on its way", re.I),
    re.compile(r"(thank you|thanks) for applying", re.I),
    re.compile(r"return to job search", re.I),
    re.compile(r"keep track of your applications", re.I),
    re.compile(r"application complete", re.I),
    re.compile(r"you applied on", re.I),
    re.compile(r"application sent", re.I),
)


_CONFIRMATION_URL_MARKERS = (
    "post-apply", "postapply", "post_apply", "/apply/success", "application-submitted",
    "applied=1", "applied=true", "from=ia-apply", "status=applied"
)


APPLY_PAGE_MARKERS = ("smartapply.indeed.com", "indeedapply")


def canonical_job_url(url: str) -> str:
    """Turns an Indeed/LinkedIn search or job-view URL into a stable link to the job posting. '' if not a job URL."""
    if not url:
        return ""
    try:
        parsed = urllib.parse.urlparse(url)
    except Exception:
        return ""
    host = (parsed.netloc or "").lower()
    qs = urllib.parse.parse_qs(parsed.query)

    if "indeed." in host:
        job_id = (qs.get("vjk") or qs.get("jk") or [""])[0]
        if job_id:
            return f"{parsed.scheme}://{parsed.netloc}/viewjob?jk={job_id}"
    if "linkedin.com" in host:
        job_id = (qs.get("currentJobId") or [""])[0]
        if not job_id:
            m = re.search(r"/jobs/view/(?:[^/]*-)?(\d+)", parsed.path)
            job_id = m.group(1) if m else ""
        if job_id:
            return f"https://www.linkedin.com/jobs/view/{job_id}/"
    return ""


def _usable_job_url(url: str) -> str:
    """Only keep links a person can open later: a job posting, not Indeed's temporary apply-form pages."""
    job_url = canonical_job_url(url)
    if job_url:
        return job_url
    low = (url or "").lower()
    return url if url and ("/viewjob" in low or "/jobs/view/" in low) else ""


def job_key(job_title: str, company: str, platform: str) -> str:
    return "|".join((job_title.strip().lower(), company.strip().lower(), platform_root(platform)))


def record_application(ctx: RunContext, job_title: str, company: str, platform: str, url: str, status: str,
                       notes: str = "", match: Optional[Tuple[int, str]] = None) -> Dict[str, Any]:
    """Stores the outcome for a job, updating the existing record if the same job was logged in the last 2 hours."""
    now = datetime.now(timezone.utc)
    key = job_key(job_title, company, platform)
    doc = ctx.repo.find_recent_application(ctx.user_id, key, (now - _MERGE_WINDOW).isoformat())
    if doc is None:
        doc = {"id": new_id(), "user_id": ctx.user_id, "run_id": ctx.run_id, "job_key": key, "created_at": now.isoformat()}
    doc.update(job_title=job_title, company=company, platform=platform, status=status, updated_at=now.isoformat(),
               url=url or doc.get("url", ""), notes=notes or doc.get("notes", ""))
    if match:
        doc.update(match_score=match[0], match_reason=match[1])
    return ctx.repo.save_application(doc)


def _find_confirmation(url: str, text: str) -> str:
    low_url = (url or "").lower()
    for marker in _CONFIRMATION_URL_MARKERS:
        if marker in low_url:
            return f"url contains '{marker}'"
    for pattern in _CONFIRMATION_PATTERNS:
        m = pattern.search(text or "")
        if m:
            return f"page says '{m.group(0)}'"
    return ""


async def observe_step(browser_state_summary, model_output, step) -> None:  # noqa: ARG001 - signature fixed by browser-use
    """
    Agent step callback: remembers whether the page the agent is looking at is an application-confirmation page.
    log_job_status only accepts 'Submitted' when this has happened.
    """
    try:
        ctx = get_run_context()
        urls = [browser_state_summary.url] + [t.url for t in (browser_state_summary.tabs or [])]
        try:
            text = browser_state_summary.dom_state.llm_representation()
        except Exception:
            text = ""
        for url in urls:
            evidence = _find_confirmation(url, text if url == browser_state_summary.url else "")
            if evidence:
                ctx.confirmation_seen = evidence
                return
    except Exception:
        pass


@controller.action(
    "MANDATORY before applying to ANY job: score how well the job's requirements match the candidate and get the decision. "
    "Pass the job title, the company and `job_requirements` = a compact copy of the required skills / experience / qualifications / responsibilities "
    "(up to ~1500 characters). Returns 'DECISION: APPLY' or 'DECISION: SKIP' - obey it. Skipped jobs are logged automatically."
)
async def score_job_match(browser_session, job_title: str, company: str, job_requirements: str) -> str:  # browser_session is injected (unannotated on purpose)
    ctx = get_run_context()
    if ctx.evaluations >= ctx.max_evaluations:
        return (f"STOP: you have already evaluated {ctx.evaluations} jobs (the limit for this run). Do not look at more jobs - "
                f"call `done` now with a summary.")
    ctx.evaluations += 1

    try:  # remember which job posting this is, while its page is still open
        for tab in await browser_session.get_tabs():
            job_url = canonical_job_url(tab.url or "")
            if job_url:
                ctx.last_job_url = job_url
                break
    except Exception:
        pass

    result = await score_job(ctx.llm, ctx.profile, job_title, company, job_requirements)
    if result is None:
        # The model could not be reached. Being lenient, do not block the run: apply, and say the score is unknown.
        ctx.pending_score = None
        msg = "DECISION: APPLY (the match score could not be calculated right now, so no jobs are blocked). Continue with the application."
        console.print(f"[yellow]  {msg}[/yellow]")
        return msg

    minimum = ctx.min_match_score
    reason = result.reason or ("matched: " + ", ".join(result.matched[:5]))
    if meets_threshold(result.score, minimum):
        ctx.pending_score = (result.score, reason)
        msg = (f"DECISION: APPLY - match score {result.score}/100 is at least your minimum of {minimum}. "
               f"({reason}) Continue: click the Apply button. Submitted so far: {ctx.applied} of {ctx.target_applications}.")
    else:
        ctx.pending_score = None
        missing = f" Missing: {', '.join(result.missing[:5])}." if result.missing else ""
        await asyncio.to_thread(
            record_application, ctx, job_title, company, ctx.platform.title(), ctx.last_job_url, "Skipped",
            f"Match score {result.score}/100 is below your minimum of {minimum}. {reason}{missing}", (result.score, reason))
        ctx.last_job_url = ""
        msg = (f"DECISION: SKIP - match score {result.score}/100 is below your minimum of {minimum}. Do NOT apply. "
               f"The job is already logged. Go to the next job card.")
    console.print(f"[cyan]  {job_title} @ {company}: {msg}[/cyan]")
    return msg


@controller.action("Check whether submitting application is allowed under dry-run mode")
async def verify_before_submit(job_title: str, company: str, platform: str, url: str = "") -> str:
    """Guards application submission. In a dry run the agent must stop before clicking the final Submit."""
    ctx = get_run_context()
    if "company site" in platform.lower() or "external" in platform.lower():
        msg = (
            f"[RESTRICTED] Applications on external company sites or employer links are disabled. "
            f"Only native Easy Apply and Apply now are allowed. DO NOT submit for '{job_title}' at '{company}'. "
            f"Close this tab/modal and call log_job_status('{job_title}', '{company}', '{platform}', 'Skipped', "
            f"notes='Skipped: external company website / not native Easy Apply or Apply now')."
        )
        console.print(f"[bold red]  {msg}[/bold red]")
        return msg

    if ctx.dry_run:
        msg = (f"[DRY RUN ACTIVE] Form filled and resume verified for '{job_title}' at '{company}'. "
               f"DO NOT click the final submit button. Close or exit this application modal/tab and proceed immediately to the next job in the search list.")
        console.print(f"[yellow]  {msg}[/yellow]")
        await asyncio.to_thread(record_application, ctx, job_title, company, platform, url or ctx.last_job_url,
                                "Previewed", "Paused before final submit", ctx.pending_score)
        return msg

    # Live mode: recommend submit_application for direct, reliable submission
    return (
        f"[LIVE MODE] Submission is allowed for '{job_title}' at '{company}'. "
        f"Call submit_application('{job_title}', '{company}', '{platform}') NOW to automatically scroll down, "
        f"click the submit button, wait for confirmation, record the application as Submitted, and handle the 10-second anti-bot delay. "
        f"Alternatively, click the 'Submit your application' / 'Submit application' button manually, wait 3-5 seconds for confirmation, and call log_job_status."
    )


JS_HANDLE_NOTICE_PERIOD = """
(function(days, daysStr, monthsStr) {
""" + _DEEP + """
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  const fire = (n, type) => n.dispatchEvent(new MouseEvent(type, {bubbles: true, cancelable: true, view: window}));
  const visible = e => !!(e.offsetWidth || e.offsetHeight || e.getClientRects().length);
  const txt = e => (e.innerText || e.textContent || '').replace(/\\s+/g, ' ').trim().toLowerCase();

  const allElements = deepAll(document, 'fieldset, div[class*="question" i], div[class*="form" i], div[class*="group" i], div, section');
  let noticeContainer = allElements.find(el => {
    if (!visible(el)) return false;
    const t = txt(el);
    return (t.includes('notice period') || t.includes('notice') || t.includes('how soon can you start') ||
            t.includes('availability to join') || t.includes('joining period') || t.includes('available to start'))
           && t.length < 500;
  });

  const root = noticeContainer || document;

  const radios = deepAll(root, 'input[type=radio]').filter(visible);
  if (radios.length > 0) {
    let bestRadio = null;
    let bestScore = -1;

    radios.forEach(radio => {
      const parentLabel = radio.closest('label') || (radio.id ? document.querySelector('label[for="' + radio.id + '"]') : null) || radio.parentElement;
      const labelText = parentLabel ? txt(parentLabel) : (radio.value || '').toLowerCase();

      let score = 0;
      if (days === 0 && (labelText.includes('immediate') || labelText.includes('0 day') || labelText.includes('serving') || labelText.includes('15 day') || labelText.includes('less than'))) {
        score = 100;
      } else if (labelText.includes(daysStr + ' day') || labelText.includes(daysStr + 'day') || labelText.includes(monthsStr + ' month')) {
        score = 90;
      } else if (labelText.includes(daysStr) || labelText.includes(monthsStr)) {
        score = 70;
      } else if (days <= 30 && (labelText.includes('1 month') || labelText.includes('30 day') || labelText.includes('immediate'))) {
        score = 60;
      } else if (days <= 60 && (labelText.includes('2 month') || labelText.includes('60 day'))) {
        score = 60;
      } else if (days <= 90 && (labelText.includes('3 month') || labelText.includes('90 day'))) {
        score = 60;
      }

      if (score > bestScore) {
        bestScore = score;
        bestRadio = radio;
      }
    });

    if (bestRadio && bestScore > 0) {
      bestRadio.scrollIntoView({block: 'nearest'});
      bestRadio.checked = true;
      fire(bestRadio, 'click');
      bestRadio.dispatchEvent(new Event('change', {bubbles: true}));
      const parentLabel = bestRadio.closest('label') || bestRadio.parentElement;
      return {ok: true, kind: 'radio', value: parentLabel ? (parentLabel.innerText || '').trim() : bestRadio.value};
    }
  }

  const checkboxes = deepAll(root, 'input[type=checkbox]').filter(visible);
  if (checkboxes.length > 0) {
    for (const cb of checkboxes) {
      const parentLabel = cb.closest('label') || (cb.id ? document.querySelector('label[for="' + cb.id + '"]') : null) || cb.parentElement;
      const labelText = parentLabel ? txt(parentLabel) : '';
      if ((days === 0 && (labelText.includes('immediate') || labelText.includes('available'))) ||
          labelText.includes(daysStr) || labelText.includes(monthsStr)) {
        cb.scrollIntoView({block: 'nearest'});
        if (!cb.checked) {
          cb.checked = true;
          fire(cb, 'click');
          cb.dispatchEvent(new Event('change', {bubbles: true}));
        }
        return {ok: true, kind: 'checkbox', value: labelText};
      }
    }
  }

  const selects = deepAll(root, 'select').filter(visible);
  if (selects.length > 0) {
    const sel = selects[0];
    let bestOptIndex = -1;
    let bestOptScore = -1;
    Array.from(sel.options).forEach((opt, idx) => {
      const optText = (opt.text || '').toLowerCase().trim();
      let score = 0;
      if (days === 0 && (optText.includes('immediate') || optText.includes('0 day') || optText.includes('serving') || optText.includes('15 day'))) {
        score = 100;
      } else if (optText.includes(daysStr + ' day') || optText.includes(monthsStr + ' month')) {
        score = 90;
      } else if (optText.includes(daysStr) || optText.includes(monthsStr)) {
        score = 70;
      } else if (days <= 30 && (optText.includes('1 month') || optText.includes('30') || optText.includes('immediate'))) {
        score = 50;
      }
      if (score > bestOptScore) {
        bestOptScore = score;
        bestOptIndex = idx;
      }
    });

    if (bestOptIndex >= 0 && bestOptScore > 0) {
      sel.selectedIndex = bestOptIndex;
      sel.options[bestOptIndex].selected = true;
      sel.dispatchEvent(new Event('input', {bubbles: true}));
      sel.dispatchEvent(new Event('change', {bubbles: true}));
      return {ok: true, kind: 'select', value: sel.options[bestOptIndex].text.trim()};
    }
  }

  const textInputs = deepAll(root, 'input[type=text], input[type=number], input:not([type])').filter(visible);
  for (const input of textInputs) {
    const ph = (input.placeholder || '').toLowerCase();
    const name = (input.name || '').toLowerCase();
    const id = (input.id || '').toLowerCase();
    const label = (input.labels && input.labels[0] && input.labels[0].innerText || '').toLowerCase();
    const aria = (input.getAttribute('aria-label') || '').toLowerCase();
    const combined = ph + ' ' + name + ' ' + id + ' ' + label + ' ' + aria;

    if (combined.includes('notice') || combined.includes('how soon') || combined.includes('start') || combined.includes('available') || combined.includes('joining')) {
      let valToEnter = String(days);
      if (combined.includes('month') && !combined.includes('day')) {
        valToEnter = monthsStr;
      }
      input.focus();
      input.value = valToEnter;
      input.dispatchEvent(new Event('input', {bubbles: true}));
      input.dispatchEvent(new Event('change', {bubbles: true}));
      return {ok: true, kind: 'input', value: valToEnter};
    }
  }

  return {ok: false, error: 'Could not find a notice period field on the current page'};
})
"""


JS_BLOCK_SAVE_AND_CLOSE = """
(() => {
    function purgeSaveAndClose() {
        const elements = document.querySelectorAll('button, a, [role="button"], input[type="button"], input[type="submit"]');
        elements.forEach(el => {
            const text = (el.innerText || el.value || el.getAttribute('aria-label') || '').toLowerCase().trim();
            if (text.includes('save and close') || text.includes('save & close') ||
                text.includes('save and exit') || text.includes('save & exit') ||
                text.includes('save application') || text.includes('save and finish later') ||
                text === 'save draft') {
                el.style.display = 'none';
                el.style.visibility = 'hidden';
                el.style.pointerEvents = 'none';
                el.disabled = true;
                el.setAttribute('tabindex', '-1');
                el.setAttribute('aria-hidden', 'true');
            }
        });
    }

    purgeSaveAndClose();

    if (!window._save_close_blocker_installed) {
        window._save_close_blocker_installed = true;
        const observer = new MutationObserver(() => {
            purgeSaveAndClose();
        });
        if (document.body || document.documentElement) {
            observer.observe(document.body || document.documentElement, { childList: true, subtree: true });
        }

        document.addEventListener('click', (e) => {
            const target = e.target.closest('button, a, [role="button"]');
            if (target) {
                const t = (target.innerText || target.value || target.getAttribute('aria-label') || '').toLowerCase().trim();
                if (t.includes('save and close') || t.includes('save & close') ||
                    t.includes('save and exit') || t.includes('save & exit') ||
                    t.includes('save application') || t.includes('save and finish later') ||
                    t === 'save draft') {
                    e.preventDefault();
                    e.stopPropagation();
                    console.warn('Blocked click on Save and close button:', t);
                }
            }
        }, true);
    }
})()
"""


JS_CLICK_SUBMIT_BUTTON = """
(() => {
    // 1. Scroll main window and modal containers to the bottom
    window.scrollTo({ top: document.body.scrollHeight, behavior: 'instant' });
    const scrollables = document.querySelectorAll(
        '[role="dialog"], .ia-BasePage, .jobs-easy-apply-modal, main, .ia-JobApplicationForm, div[class*="modal"], div[class*="apply"]'
    );
    scrollables.forEach(el => { el.scrollTop = el.scrollHeight; });

    // 2. Identify candidate submit buttons
    const candidates = Array.from(document.querySelectorAll('button, a[role="button"], input[type="submit"]'));
    const isNegative = (text, label) => {
        const t = (text + ' ' + label).toLowerCase();
        return t.includes('back') || t.includes('previous') || t.includes('cancel') ||
               t.includes('discard') || t.includes('save') || t.includes('edit') || t.includes('return to job');
    };

    const submitBtn = candidates.find(b => {
        const text = (b.innerText || b.value || '').trim().toLowerCase();
        const label = (b.getAttribute('aria-label') || '').toLowerCase();
        const testId = (b.getAttribute('data-testid') || '').toLowerCase();
        const id = (b.id || '').toLowerCase();
        if (isNegative(text, label)) return false;
        return text === 'submit your application' ||
               text === 'submit application' ||
               label.includes('submit your application') ||
               label.includes('submit application') ||
               id.includes('review-submit-button') ||
               id.includes('submit-button') ||
               testId.includes('submit-application') ||
               testId.includes('submit');
    }) || candidates.find(b => {
        const text = (b.innerText || b.value || '').trim().toLowerCase();
        const label = (b.getAttribute('aria-label') || '').toLowerCase();
        if (isNegative(text, label)) return false;
        const isPrimary = b.classList.contains('artdeco-button--primary') ||
                          b.classList.contains('ia-continueButton') ||
                          b.classList.contains('btn-primary');
        return isPrimary && (text.includes('submit') || label.includes('submit'));
    });

    if (submitBtn) {
        submitBtn.scrollIntoView({ behavior: 'instant', block: 'center' });
        submitBtn.click();
        return { clicked: true, text: (submitBtn.innerText || submitBtn.value || 'submit').trim() };
    }
    return { clicked: false, message: 'Submit button not found' };
})()
"""

JS_INSPECT_PAGE_STATE = """
(() => {
    return {
        url: window.location.href,
        text: (document.body ? document.body.innerText : '').slice(0, 4000)
    };
})()
"""


@controller.action(
    "Submit the application on the final review page. Automatically scrolls down, clicks 'Submit your application' / 'Submit application', "
    "waits for the confirmation page, records the application as Submitted, and enforces the 10-second anti-bot delay. "
    "ALWAYS call this on the final review page instead of trying to click or log manually."
)
async def submit_application(browser_session, job_title: str, company: str, platform: str) -> str:  # browser_session is injected
    ctx = get_run_context()
    if "company site" in platform.lower() or "external" in platform.lower():
        msg = f"External site applications are disabled; skipped '{job_title}' at '{company}'."
        await asyncio.to_thread(record_application, ctx, job_title, company, platform, ctx.last_job_url, "Skipped", msg)
        return msg

    if ctx.dry_run:
        msg = f"[DRY RUN ACTIVE] Verified review page for '{job_title}' at '{company}'. Submit skipped. Proceed to next job."
        await asyncio.to_thread(record_application, ctx, job_title, company, platform, ctx.last_job_url, "Previewed", "Paused before submit", ctx.pending_score)
        return msg

    try:
        cdp = await browser_session.get_or_create_cdp_session()
        client, sid = cdp.cdp_client, cdp.session_id

        try:
            await client.send.Runtime.evaluate(
                params={"expression": JS_BLOCK_SAVE_AND_CLOSE, "returnByValue": True},
                session_id=sid,
            )
        except Exception:
            pass

        eval_click = await client.send.Runtime.evaluate(
            params={"expression": JS_CLICK_SUBMIT_BUTTON, "returnByValue": True},
            session_id=sid,
        )
        click_data = (eval_click.get("result") or {}).get("value") or {}
        if not click_data.get("clicked"):
            console.print(f"[yellow]  Submit button not clicked automatically. Checking current page confirmation...[/yellow]")
        else:
            console.print(f"[green]  Clicked submit button ('{click_data.get('text')}') for {job_title} @ {company}. Waiting for confirmation...[/green]")

        await asyncio.sleep(4.0)

        eval_inspect = await client.send.Runtime.evaluate(
            params={"expression": JS_INSPECT_PAGE_STATE, "returnByValue": True},
            session_id=sid,
        )
        inspect_data = (eval_inspect.get("result") or {}).get("value") or {}
        current_url = inspect_data.get("url", "")
        page_text = inspect_data.get("text", "")

        evidence = _find_confirmation(current_url, page_text) or ctx.confirmation_seen
        if not evidence:
            low_text = page_text.lower()
            if any(k in low_text for k in ("submitted", "applied", "application sent", "thank you for applying", "return to job search")):
                evidence = "Page text indicates application completed"

        if evidence:
            ctx.confirmation_seen = evidence
            notes = f"Confirmed: {evidence}"
            status = "Submitted"
        else:
            if click_data.get("clicked"):
                ctx.confirmation_seen = f"Submit button '{click_data.get('text')}' clicked"
                notes = f"Confirmed: {ctx.confirmation_seen}"
                status = "Submitted"
            else:
                return (
                    f"Could not locate the submit button on this page for '{job_title}'. "
                    f"Please scroll to the bottom of the page/modal and click the 'Submit your application' button manually, "
                    f"then call log_job_status('{job_title}', '{company}', '{platform}', 'Submitted')."
                )

        await asyncio.to_thread(record_application, ctx, job_title, company, platform, ctx.last_job_url, status, notes, ctx.pending_score)
        ctx.applied += 1
        ctx.last_job_url = ""
        ctx.confirmation_seen = ""
        ctx.pending_score = None
        console.print(f"[bold green]✔ Successfully submitted application:[/bold green] {job_title} @ {company} -> {status} ({ctx.applied}/{ctx.target_applications})")

        console.print(f"[bold yellow]⏳ Applied for '{job_title}'. Waiting 10 seconds before next job to prevent bot detection...[/bold yellow]")
        await asyncio.sleep(10.0)

        msg = f"SUCCESS: Application for '{job_title}' at '{company}' submitted and confirmed ({notes}). Total applications submitted: {ctx.applied} of {ctx.target_applications}. Waited 10s anti-bot delay."
        if ctx.applied >= ctx.target_applications:
            msg += " TARGET REACHED - call `done` now with a summary."
        else:
            msg += " Proceed directly to the next job in the search list."
        return msg
    except Exception as e:
        return f"submit_application encountered an error: {e}. If the submit button is visible, click it manually and call log_job_status."


JS_FILTER_EASY_APPLY = """
(() => {
    // 1. Prevent clicks on company profile links from navigating away
    if (!window._company_clicks_blocked) {
        window._company_clicks_blocked = true;
        document.addEventListener('click', (e) => {
            const companyLink = e.target.closest('a[href*="/company/"], a[href*="/cmp/"]');
            if (companyLink) {
                e.preventDefault();
                e.stopPropagation();
                console.warn('Blocked click on company profile link:', companyLink.href);
            }
        }, true);
    }

    // 2. Locate the Easy Apply filter button in the filter bar
    const buttons = Array.from(document.querySelectorAll('button, a'));
    const btn = buttons.find(b => {
        const text = (b.innerText || '').trim().toLowerCase();
        const label = (b.getAttribute('aria-label') || '').trim().toLowerCase();
        const id = (b.id || '').toLowerCase();
        return (
            (text === 'easy apply' || label.includes('easy apply') || id.includes('easy_apply') || id === 'searchfilter_f_al') &&
            !b.closest('.jobs-search-results-list') &&
            !b.closest('.job-card-container') &&
            !b.closest('.jobs-details')
        );
    });

    if (!btn) {
        return { found: false, message: "Easy Apply filter pill not found in the search header bar." };
    }

    const isSelected = btn.getAttribute('aria-checked') === 'true' ||
                       btn.getAttribute('aria-pressed') === 'true' ||
                       btn.classList.contains('artdeco-pill--selected') ||
                       btn.classList.contains('active');

    if (isSelected) {
        return { found: true, already_active: true, message: "Easy Apply filter is already active." };
    }

    btn.click();
    return { found: true, already_active: false, message: "Clicked 'Easy Apply' filter button." };
})()
"""


@controller.action(
    "Ensure the 'Easy Apply' filter is activated on LinkedIn job search. "
    "Call this as your FIRST action right after reaching the LinkedIn job search page before clicking any job cards."
)
async def filter_by_easy_apply(browser_session) -> str:  # browser_session is injected
    """Activates the 'Easy Apply' filter on the LinkedIn job search results page and blocks accidental company profile clicks."""
    try:
        cdp = await browser_session.get_or_create_cdp_session()
        client, sid = cdp.cdp_client, cdp.session_id
        eval_res = await client.send.Runtime.evaluate(
            params={"expression": JS_FILTER_EASY_APPLY, "returnByValue": True},
            session_id=sid,
        )
        data = (eval_res.get("result") or {}).get("value") or {}
        if data.get("found"):
            if not data.get("already_active"):
                await asyncio.sleep(2.5)  # wait for results to re-filter
                return "Successfully clicked 'Easy Apply' filter. The job listings are now filtered to Easy Apply jobs only."
            return "Easy Apply filter is already active. Search results only contain Easy Apply jobs."
        return f"Easy Apply filter check completed: {data.get('message', 'filter pill checked')}. Ready to evaluate jobs."
    except Exception as e:
        return f"Error activating Easy Apply filter: {e}"


@controller.action(
    "Call this IMMEDIATELY after clicking 'Apply now' / 'Easily apply'. "
    "Waits (up to ~40s) for the application page to finish loading, switches to its tab if it opened in a new tab, "
    "and tells you where you are. Do NOT navigate back to the search page while this page is still loading."
)
async def wait_for_apply_page(browser_session, search_url: str = "", timeout_seconds: int = 40) -> str:  # browser_session is injected
    """Indeed opens the apply flow (smartapply.indeed.com or native in-page flow) in a new tab / slow-loading page."""
    ctx = get_run_context()
    ctx.confirmation_seen = ""  # a new application starts here
    timeout_seconds = max(5, min(int(timeout_seconds), 90))
    search_host_path = search_url.split("?")[0] if search_url else ""
    deadline = asyncio.get_event_loop().time() + timeout_seconds
    last_seen = "no apply page detected yet"

    while asyncio.get_event_loop().time() < deadline:
        try:
            tabs = await browser_session.get_tabs()
            current_url = await browser_session.get_current_page_url()
        except Exception as e:
            last_seen = f"tab inspection failed: {e}"
            await asyncio.sleep(1.5)
            continue

        for tab in tabs:  # remember which job posting we are applying to (the search tab still shows it)
            job_url = canonical_job_url(tab.url or "")
            if job_url:
                ctx.last_job_url = job_url
                break

        candidate = next((t for t in tabs if any(m in (t.url or "").lower() for m in APPLY_PAGE_MARKERS)), None)
        if candidate is None:
            # Check if an external company site tab opened
            for tab in reversed(tabs):
                url = tab.url or ""
                is_blank = url in ("", "about:blank") or url.startswith("chrome://")
                is_search = bool(search_host_path) and url.startswith(search_host_path)
                if not is_blank and not is_search and "indeed.com" not in url and "linkedin.com" not in url:
                    msg = (f"External company website detected ({url}). Applying on external sites / company websites is disabled. "
                           f"Close this tab, call log_job_status with status 'Skipped' and reason 'External company website', "
                           f"and proceed to the next job card.")
                    console.print(f"[yellow]  {msg}[/yellow]")
                    return msg

        if candidate is not None:
            if candidate.url != current_url:
                try:
                    await browser_session.event_bus.dispatch(SwitchTabEvent(target_id=candidate.target_id))
                except Exception as e:
                    last_seen = f"found apply tab {candidate.url} but switching failed: {e}"
                    await asyncio.sleep(1.5)
                    continue
            await asyncio.sleep(3)  # let the form render
            try:
                cdp = await browser_session.get_or_create_cdp_session()
                client, sid = cdp.cdp_client, cdp.session_id
                await client.send.Runtime.evaluate(
                    params={"expression": JS_BLOCK_SAVE_AND_CLOSE, "returnByValue": True},
                    session_id=sid,
                )
            except Exception:
                pass
            msg = (f"Apply page is open at {candidate.url}. You are now on it. Continue filling the application here. "
                   f"Do NOT click 'Save and close' or go back to the search results until this job is submitted.")
            console.print(f"[green]  {msg}[/green]")
            return msg

        last_seen = f"still on {current_url}"
        await asyncio.sleep(2)

    msg = (f"No separate apply page detected after {timeout_seconds}s ({last_seen}). "
           f"The apply form may be an in-page modal/overlay on the current page: look at the current page for an application "
           f"dialog before doing anything else. If nothing opened, click the Apply button once more and call this action again.")
    console.print(f"[yellow] {msg}[/yellow]")
    return msg


@controller.action("Prompt the user for an interactive input, such as an email verification code (OTP) or 2FA code")
async def ask_human_for_input(prompt_message: str) -> str:
    """Asks the user for a verification code. Runs off the event loop so the browser stays responsive."""
    ctx = get_run_context()
    console.print(f"\n[bold yellow] Action Required:[/bold yellow] {prompt_message}")
    handler = ctx.input_handler or (lambda _msg: input(">> Enter code / input: ").strip())
    return await asyncio.to_thread(handler, prompt_message)


@controller.action(
    "Log job application status into application history. Statuses: 'Submitted' (ONLY after the confirmation page "
    "'Your application has been submitted' is visible), 'Previewed' (dry run), 'Failed' (error, e.g. network error, "
    "could not finish) or 'Skipped'. Put the reason in notes."
)
async def log_job_status(job_title: str, company: str, platform: str, status: str, notes: str = "", url: str = "") -> str:
    """
    Records the outcome. `url` should be the link to the job posting. A live 'Submitted' claim is only recorded
    when a confirmation page was actually seen; the agent's own word is not enough.
    """
    ctx = get_run_context()
    job_url = _usable_job_url(url) or ctx.last_job_url

    claimed = status.strip().lower()
    result_msg = None
    if claimed in ("submitted", "applied", "success", "successful", "completed") and not ctx.dry_run:
        if ctx.confirmation_seen:
            status = "Submitted"
            notes = (notes + " | " if notes else "") + f"Confirmed: {ctx.confirmation_seen}"
        else:
            status = "Not confirmed"
            notes = (notes + " | " if notes else "") + "Agent reported success but no confirmation page was seen"
            result_msg = (
                f"NOT recorded as submitted: the 'application submitted' confirmation page was never seen for "
                f"'{job_title}'. Do not claim success. If the application form is still open, finish it, click "
                f"'Submit your application', wait for the confirmation, then call log_job_status again. "
                f"If it cannot be completed (e.g. network error), log it with status 'Failed' and the reason in notes."
            )
    elif ctx.dry_run and claimed in ("submitted", "applied", "success", "successful", "completed", "previewed", "preview"):
        status = "Previewed"  # nothing is submitted in a dry run, whatever the agent claims

    if ("company site" in platform.lower() or "external" in platform.lower()) and claimed in ("submitted", "applied", "success", "successful", "completed"):
        status = "Skipped"
        notes = (notes + " | " if notes else "") + "External site applications are disabled; skipped"

    await asyncio.to_thread(record_application, ctx, job_title, company, platform, job_url, status, notes, ctx.pending_score)
    if status in ("Submitted", "Previewed"):
        ctx.applied += 1
    ctx.last_job_url = ""
    ctx.confirmation_seen = ""
    ctx.pending_score = None
    console.print(f"[cyan] Logged job:[/cyan] {job_title} @ {company} -> {status}")

    # Sleep 10 seconds after applying a job to avoid bot detection
    if status == "Submitted" and not ctx.dry_run:
        console.print(f"[bold yellow]⏳ Applied for '{job_title}'. Waiting 10 seconds before next job to prevent bot detection...[/bold yellow]")
        await asyncio.sleep(10)

    if result_msg:
        return result_msg
    msg = f"Logged application status: {status}. Applications so far: {ctx.applied} of {ctx.target_applications}."
    if status == "Submitted":
        msg += " Waited 10s anti-bot delay."
    if ctx.applied >= ctx.target_applications:
        msg += " TARGET REACHED - call `done` now with a summary."
    return msg


@controller.action(
    "Choose an answer in ANY dropdown: native <select>, custom listbox/combobox, type-ahead field (notice period, willing to relocate, "
    "country, state, education, gender, 'how did you hear about us'...). Pass the element `index` of the dropdown and the `answer` in plain words "
    "('Yes', 'India', '1 Month', '30 days', \"Bachelor's\"). It opens the list itself, picks the closest matching option and confirms the result. "
    "ALWAYS use this instead of select_dropdown or clicking through the list by hand."
)
async def pick_option(browser_session, index: int, answer: str) -> str:  # browser_session is injected
    node = await browser_session.get_element_by_index(index)
    if node is None:
        return f"Element index {index} is not available - the page changed. Look at the current page state and use the new index."
    try:
        session = await browser_session.cdp_client_for_node(node)
        client, sid = session.cdp_client, session.session_id
        resolved = await client.send.DOM.resolveNode(params={"backendNodeId": node.backend_node_id}, session_id=sid)
        object_id = resolved["object"]["objectId"]

        collected = await client.send.Runtime.callFunctionOn(
            params={"functionDeclaration": JS_COLLECT_OPTIONS, "objectId": object_id,
                    "arguments": [{"value": answer}], "awaitPromise": True, "returnByValue": True},
            session_id=sid,
        )
        data = (collected.get("result") or {}).get("value")
        if not data:
            return f"Could not read options from element {index}: {collected.get('exceptionDetails', {}).get('text', 'unknown error')}"

        if data.get("kind") == "input_field":
            msg = f"Entered '{answer}' into input field {index}."
            console.print(f"[green]  {msg}[/green]")
            return msg
        texts = [o["text"] for o in data["options"]]
        usable = [o["text"] for o in data["options"] if not o.get("disabled") and not is_placeholder(o["text"])]
        if not texts:
            return (f"No options are visible for element {index}. Click the dropdown once so its list opens, then call pick_option again "
                    f"(using the index of the dropdown), or type the answer into the field and press Enter with send_keys.")

        best, how = best_option(answer, [o["text"] if not o.get("disabled") else "" for o in data["options"]])
        if best is None:
            return (f"No option matches '{answer}' well ({how}). Available options: {usable}. "
                    f"Call pick_option again with the exact text of the option that best fits the question.")

        applied = await client.send.Runtime.evaluate(
            params={"expression": JS_APPLY_OPTION % {"i": best, "kind": data["kind"]}, "returnByValue": True},
            session_id=sid,
        )
        result = (applied.get("result") or {}).get("value") or {}
        if not result.get("ok"):
            return f"Found '{texts[best]}' but could not select it: {result.get('error', 'unknown error')}. Try pick_option again."

        await asyncio.sleep(0.4)
        now = result.get("now")
        if now is None:
            back = await client.send.Runtime.callFunctionOn(
                params={"functionDeclaration": JS_READ_BACK, "objectId": object_id, "returnByValue": True}, session_id=sid)
            now = (back.get("result") or {}).get("value", "")
        msg = f"Selected '{texts[best]}' for answer '{answer}' ({how}). The field now shows: '{now}'."
        console.print(f"[green]  {msg}[/green]")
        return msg
    except Exception as e:  # noqa: BLE001 - report to the agent instead of crashing the step
        return f"pick_option failed on element {index}: {e}. Try clicking the dropdown and choosing the option by index."

@controller.action(
    "Automatically answers the Notice Period question on the current form. Handles radio buttons, checkboxes, "
    "dropdowns (<select> / custom comboboxes), and text/number input fields with string placeholders (e.g. 'Notice period in days'). "
    "Optionally pass `field_index` if you know the element index, or omit it to auto-detect and fill the notice period field on the page."
)
async def answer_notice_period(browser_session, field_index: Optional[int] = None) -> str:  # browser_session is injected
    ctx = get_run_context()
    profile = ctx.profile
    days = profile.notice_period_days if profile.notice_period_days is not None else 0
    days_str = str(days)
    months_str = str(max(1, round(days / 30))) if days >= 15 else "0"

    if field_index is not None:
        answer_text = "Immediate" if days == 0 else f"{days} days"
        return await pick_option(browser_session, field_index, answer_text)

    try:
        cdp = await browser_session.get_or_create_cdp_session()
        client, sid = cdp.cdp_client, cdp.session_id

        eval_res = await client.send.Runtime.evaluate(
            params={"expression": f"({JS_HANDLE_NOTICE_PERIOD})({days}, {json.dumps(days_str)}, {json.dumps(months_str)})",
                    "returnByValue": True, "awaitPromise": True},
            session_id=sid,
        )
        data = (eval_res.get("result") or {}).get("value") or {}
        if data.get("ok"):
            kind = data.get("kind", "field")
            val = data.get("value", "")
            msg = f"Successfully set Notice Period ({kind}): '{val}' based on candidate profile ({days} days)."
            console.print(f"[green]  {msg}[/green]")
            return msg

        ans = "Immediate" if days == 0 else f"{days} days"
        return (
            f"Notice period auto-detection could not find a clear notice period container. "
            f"Candidate notice period is {days} days ({ans}). Look at the page: if it is a radio button, click the '{ans}' radio; "
            f"if it is a dropdown, call pick_option with '{ans}'; if it is a text field, type '{days_str}'."
        )
    except Exception as e:
        ans = "Immediate" if days == 0 else f"{days} days"
        return f"answer_notice_period encountered: {e}. Use '{ans}' (or '{days_str}') to answer the notice period question manually."



@controller.action(
    "Attach the candidate's resume to the application's file-upload field. NO index needed: it finds the resume/CV upload field itself, "
    "including hidden file inputs behind an 'Upload resume' / 'Attach' / 'Drop file here' button. Use it whenever a form asks for a resume, "
    "CV or 'upload a file'. Do NOT click the upload button (that opens a native file dialog you cannot use). "
    "`field_hint` is 'resume' (default) or 'cover letter'."
)
async def upload_resume(browser_session, field_hint: str = "resume") -> str:  # browser_session is injected
    path = get_run_context().resume_path
    if not path or not os.path.isfile(path):
        return "No resume file is configured for this run."
    if "cover" in field_hint.lower():
        return "There is no cover letter file to upload. If a cover letter is optional, skip it; otherwise type a short cover letter into the text box if one is offered."
    try:
        cdp = await browser_session.get_or_create_cdp_session()
        client, sid = cdp.cdp_client, cdp.session_id
        listed = await client.send.Runtime.evaluate(params={"expression": JS_LIST_FILE_INPUTS, "returnByValue": True}, session_id=sid)
        inputs = (listed.get("result") or {}).get("value") or []
        if not inputs:
            resume_sas_url = getattr(get_run_context().profile, "resume_url", None) or ""
            # First check if there is an active text / URL field for resume link
            if resume_sas_url:
                try:
                    fill_expr = JS_FILL_RESUME_URL.replace("__RESUME_SAS_URL__", json.dumps(resume_sas_url))
                    fill_res = await client.send.Runtime.evaluate(
                        params={"expression": fill_expr, "returnByValue": True},
                        session_id=sid,
                    )
                    fill_data = (fill_res.get("result") or {}).get("value") or {}
                    if fill_data.get("found"):
                        msg = (f"Found resume URL text field ('{fill_data.get('field')}'). "
                               f"Entered candidate's direct resume SAS link from Blob Storage: '{resume_sas_url}'. "
                               f"Wait 2 seconds and proceed to the next step.")
                        console.print(f"[green]  {msg}[/green]")
                        return msg
                except Exception:
                    pass

            return ("No file-upload field exists on the page yet. If the form has an 'Upload resume' / 'Add resume' button or a resume step, "
                    "click that button once to reveal the upload field (do not pick a file in any OS dialog), then call upload_resume again. "
                    f"If the form has a string text box for 'Resume URL' / 'Resume link' / 'Link to resume' / 'CV link': "
                    f"enter the candidate's direct resume SAS link from Blob Storage: '{resume_sas_url}'. "
                    "NEVER enter the candidate's LinkedIn URL into a resume field.")
        chosen = choose_file_input(inputs, field_hint)
        if chosen is None:
            return "Only disabled upload fields were found."
        node = await client.send.Runtime.evaluate(params={"expression": JS_GET_FILE_INPUT % {"i": chosen}}, session_id=sid)
        object_id = (node.get("result") or {}).get("objectId")
        if not object_id:
            return "The upload field disappeared before it could be used. Call upload_resume again."
        try:
            await client.send.DOM.enable(session_id=sid)
        except Exception:
            pass
        await client.send.DOM.setFileInputFiles(params={"files": [path], "objectId": object_id}, session_id=sid)
        await asyncio.sleep(1.0)
        check = await client.send.Runtime.callFunctionOn(
            params={"functionDeclaration": "function(){return this.files && this.files.length ? this.files[0].name : ''}",
                    "objectId": object_id, "returnByValue": True}, session_id=sid)
        attached = (check.get("result") or {}).get("value", "")
        if not attached:
            return "The file could not be attached to the upload field. Try upload_resume again, or use upload_file with the field's index."
        msg = (f"Resume '{attached}' attached to the upload field ({len(inputs)} upload field(s) on the page). "
               f"Wait 2-3 seconds and check that the page shows the file name / a success tick before continuing.")
        console.print(f"[green]  {msg}[/green]")
        return msg
    except Exception as e:  # noqa: BLE001
        return f"upload_resume failed: {e}. Try upload_file with the index of the upload field instead."
