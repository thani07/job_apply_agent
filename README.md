# JobApply

Multi-user autonomous job search and apply for **LinkedIn** and **Indeed**.

Each user signs up, saves a resume, their notice period, expected salary and a minimum match score, plus their LinkedIn / Indeed
logins. **Search and Apply** then opens a real browser, reads every job's requirements, scores how well the user matches
(0-100, deliberately lenient) and applies **only when the score reaches the user's minimum** - with no approval step.

```
Streamlit UI  ──HTTP + JWT──▶  FastAPI  ──▶  Azure Cosmos DB   users · profiles · resumes · credentials · applications · runs
                                   │     ──▶  Azure Blob Storage   resume files (one container per user, named by the user's uuid)
                                   └──▶  browser agent (browser-use + Azure OpenAI) ──▶ LinkedIn / Indeed
```

## Setup

1. **Azure resources**
   * *Cosmos DB* (NoSQL API): copy the endpoint and key. The database `jobapply` and its containers are created on first start.
   * *Storage account*: copy the connection string (it must contain the `AccountKey`; it is used to sign SAS links).
   * *Azure OpenAI*: a deployment such as `gpt-4o`.
2. **Configure**: `.env` (see `.env.example`). `JWT_SECRET` and `CREDENTIAL_ENCRYPTION_KEY` are already generated in this project's `.env`;
   fill in `COSMOS_ENDPOINT`, `COSMOS_KEY` and `AZURE_STORAGE_CONNECTION_STRING`.
3. **Install**: `pip install -r requirements.txt` then `playwright install chromium`.
4. **Run** (two terminals):
   ```
   python main.py                    # API  -> http://127.0.0.1:8000/docs
   streamlit run streamlit_app.py    # UI
   ```
   `GET /api/health` shows which settings are configured.

## Using it

1. **Sign up / log in** (Streamlit, or `POST /api/auth/signup`, `/login`; in Swagger click *Authorize* and enter the access token).
2. **Profile tab**:
   * **Preferences & Details**: notice period, expected salary, minimum match score, default search (role, location, number of jobs, platforms).
   * **Resumes**: upload a PDF/DOCX. The file goes to Blob Storage; the extracted data (skills, experience, education ...) is stored
     in Cosmos DB. *View* shows the PDF from a **SAS link** (view + download links, valid 1 year by default; the user can choose a shorter time).
   * **Credentials**: save the LinkedIn / Indeed login, then **Test login**. The test signs in with a real browser; on success the
     session cookies are stored (encrypted) and reused by the agent. If the site demands a CAPTCHA or code, test again with
     *Show the browser window* and finish it there, or import cookies exported from your own browser.
3. **Search & Apply**: press the button. Progress, every job's match score and the list of companies applied to (per platform, with a link to the job) update live.

### API

| | |
|---|---|
| `POST /api/auth/signup` · `/login` · `GET /api/auth/me` | account + JWT |
| `GET /api/profile` · `PUT /api/profile` | everything stored for the user; update preferences |
| `POST/GET /api/profile/resumes` · `GET/DELETE /api/profile/resumes/{id}` · `PUT .../active` · `POST .../reparse` | resumes, extracted data, SAS links |
| `GET /api/credentials` · `PUT/DELETE /api/credentials/{platform}` | logins (`platform` = `linkedin` \| `indeed`), passwords never returned |
| `POST /api/credentials/{platform}/test` | real-browser login test, stores cookies on success |
| `POST /api/credentials/{platform}/cookies` | import cookies |
| `POST /api/apply` · `GET /api/apply/runs[/{id}]` · `POST /api/apply/runs/{id}/input` | search and apply; run status; answer a verification-code prompt |
| `GET /api/history` · `GET /api/history/{id}` | jobs applied to / skipped / failed - filters, sorting, paging, view modes (below) |

### History

`GET /api/history` lists every job the agent looked at - the companies it **applied to** and the ones it **skipped** (with the reason and score) - newest first.

| query parameter | |
|---|---|
| `platform` | `indeed`, `linkedin` (repeat for both) |
| `outcome` | `applied`, `skipped`, `failed`, `previewed`, `not_confirmed` (repeatable) |
| `min_score`, `max_score` | match-score range 0-100 (jobs without a score are left out while a range is set) |
| `q` · `date_from` · `date_to` · `run_id` | text in company / job title · date range (YYYY-MM-DD) · one run only |
| `sort` · `order` | `updated` \| `score` \| `company` \| `platform` \| `outcome` · `asc` \| `desc` |
| `limit` · `offset` | paging (default 25 per page, max 200) |
| `view` | `list` (flat page) \| `grouped` (page grouped by platform) \| `summary` (counts only) |

The `summary` in every response (applied / skipped / failed counts, average score, counts per platform) covers everything that matches the filters, not just the page.
Each item has a `url` that opens the job; `GET /api/history/{id}` returns one job in full. The UI's **History** tab has all of this: filters, table / cards / by-platform views, sorting, paging and a detail panel.

### How the match score works

For each job the agent passes the requirements to `score_job_match`. An LLM compares them with the resume and returns 0-100.
The agent applies when **score ≥ the user's minimum** (minimum 70: a job scoring 80 is applied to, 50 is skipped). The rubric is lenient: related
skills count as matches, a shortfall of up to 2 years costs at most 10 points, "preferred / nice to have" items never count against
the candidate, vague postings score 75. Skipped jobs are logged with their score and reason. If the scoring model cannot be reached the job is *not* blocked.

"Submitted" is only recorded after the agent has actually been on a confirmation page ("Your application has been submitted");
otherwise the job is stored as *Not confirmed*, and errors as *Failed*.

## Security notes

* Passwords are hashed with bcrypt; JWTs are HS256-signed with `JWT_SECRET`; LinkedIn / Indeed passwords and cookies are Fernet-encrypted with `CREDENTIAL_ENCRYPTION_KEY`.
  Losing that key means saved logins cannot be decrypted (users re-enter them).
* Every record is stored against the user's uuid; every query is scoped to the signed-in user.
* Passwords are given to the browser agent as `<secret>` placeholders restricted to the platform's own domain - they are never in the LLM prompt.
* SAS links are read-only, single-file, and signed with the account key: anyone with a link can read that one resume until it expires, and rotating the storage key invalidates every link.
* Run the API behind HTTPS if it is reachable by anyone else, and restrict CORS (`main.py` currently allows all origins).
* There is no rate limiting on login yet.

## Project layout

```
main.py                 starts the FastAPI app (nothing else)
config.py               all settings, read once from .env / environment (get_settings())
models.py               every pydantic model: request and response payloads, resume data, job-match result
streamlit_app.py        the UI (a client of the API)
requirements.txt  .env  .env.example

controllers/            the HTTP layer - one router per module
  auth_controller.py        sign up, log in, current user
  profile_controller.py     profile + preferences
  resume_controller.py      resumes (upload, view / download links, activate, delete)
  credentials_controller.py LinkedIn / Indeed logins, login test, cookies
  apply_controller.py       search and apply, runs, verification-code prompts
  history_controller.py     history with filters, sorting and view modes
  health_controller.py      health check
  dependencies.py           shared: database, blob store, signed-in user

services/               the logic behind them - one service per module
  core_service.py           passwords / JWT / encryption, Cosmos DB, Blob Storage + SAS links, async helpers
  auth_service.py           accounts and tokens
  profile_service.py        preferences, resumes, readiness
  resume_parser_service.py  reading a resume (text extraction, LLM / heuristic parsing)
  credentials_service.py    encrypted logins and cookies
  platform_login_service.py real-browser login test for LinkedIn / Indeed
  apply_service.py          search-and-apply runs
  history_service.py        history queries
  agent_service.py          browser profile, Indeed / LinkedIn prompts and agents, LLM, running a platform
  agent_actions_service.py  what the agent can do: score_job_match, pick_option, upload_resume, log_job_status
```

Controllers call services; services never import controllers. `models.py` and `config.py` import nothing from either folder, so both can use them.
