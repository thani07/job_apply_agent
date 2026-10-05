"""
JobApply API - starts the FastAPI app.

    python main.py                 http://127.0.0.1:8000  (Swagger UI at /docs)
    python main.py --port 9000     choose another port

Endpoints live in controllers/ (one file per module), the logic behind them in services/.
The Streamlit UI is a separate process:  streamlit run streamlit_app.py
"""

from __future__ import annotations
import argparse
import asyncio
import sys

import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from config import get_settings
from controllers import (
    apply_controller, auth_controller, credentials_controller, health_controller, history_controller, profile_controller,
    resume_controller,
)

# Suppress harmless Python 3.12 Windows asyncio proactor pipe shutdown noise
if sys.platform == "win32":
    from asyncio.proactor_events import _ProactorBasePipeTransport

    _original_del = _ProactorBasePipeTransport.__del__

    def _safe_proactor_del(self):
        try:
            _original_del(self)
        except Exception:
            pass

    _ProactorBasePipeTransport.__del__ = _safe_proactor_del


app = FastAPI(
    title="JobApply API",
    version="2.0.0",
    description="Multi-user autonomous job search and apply for LinkedIn and Indeed.",
    openapi_tags=[
        {"name": "Authentication", "description": "Sign up / log in. Every other endpoint needs the JWT (use the Authorize button)."},
        {"name": "Profile", "description": "Preferences, resumes stored in Azure Blob Storage, and LinkedIn / Indeed credentials."},
        {"name": "Search & Apply", "description": "Autonomous job search and apply, gated by the job match score."},
        {"name": "History", "description": "Every job the agent looked at, with filters, sorting and view modes."},
        {"name": "Health", "description": "Configuration check."},
    ],
)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

for controller in (health_controller, auth_controller, profile_controller, resume_controller, credentials_controller,
                   apply_controller, history_controller):
    app.include_router(controller.router)


def main() -> None:
    parser = argparse.ArgumentParser(description="JobApply API server")
    settings = get_settings()
    parser.add_argument("--host", default=settings.api_host)
    parser.add_argument("--port", type=int, default=settings.api_port)
    args = parser.parse_args()

    if sys.platform == "win32":
        # Browser automation launches Chrome as a subprocess, which needs the Proactor event loop.
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    print(f"Swagger UI: http://{args.host}:{args.port}/docs")
    # No auto-reload: it restarts the server (and any run in progress) whenever a file changes.
    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
