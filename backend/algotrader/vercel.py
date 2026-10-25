"""Running on Vercel: the dashboard and API as one Vercel Function, the bot as a Vercel Cron Job.

Vercel differs from a normal server in four ways, handled here:

- Files: only /tmp can be written and it's wiped between runs, so accounts and the bot's journal must be in
  PostgreSQL (DATABASE_URL, e.g. Supabase), the key that encrypts saved Alpaca keys must be the SECRET_KEY
  setting, and the trained model is uploaded with the site (backend/models, trained on your computer).
- Time: a request can last at most 5 minutes, so the live stream is replaced by polling and the bot runs
  once a day through /api/cron/trade (see vercel.json) instead of `python -m algotrader bot`.
- Addresses: every request reaches the app through Vercel's proxy, so the visitor's address comes from
  its headers (used to slow down password guessing).
- Admin: the site is public, so the first account does not become admin automatically.

If a required setting is missing, the site shows a page listing what to add instead of failing.
"""
from __future__ import annotations

import os
from dataclasses import replace
from pathlib import Path

from flask import Flask, jsonify, send_from_directory

from . import __version__

VERCEL_DEFAULTS = {"DATA_DIR": "/tmp/algotrader", "LIVE_STREAM": "false", "TRUST_PROXY": "true"}


def vercel_settings():
    from .config import load_settings, setup_logging
    for key, value in VERCEL_DEFAULTS.items():
        os.environ.setdefault(key, value)
    setup_logging()
    settings = load_settings()
    return replace(settings, data_dir=Path(os.environ["DATA_DIR"])).guard_exposed_dashboard(exposed=True)


def deployment_problems(settings) -> list[str]:
    """Settings Vercel needs that are missing, in words someone can act on."""
    from .db import is_postgres
    problems = []
    if not settings.has_credentials:
        problems.append("ALPACA_API_KEY and ALPACA_SECRET_KEY aren't set: add the bot's Alpaca paper keys.")
    if settings.user_accounts and not (settings.database_url and is_postgres(settings.database_url)):
        problems.append("DATABASE_URL isn't set: add your Supabase connection string. On Vercel nothing can be "
                        "saved in files, so accounts must be stored in Supabase.")
    if settings.user_accounts and not settings.secret_key:
        problems.append("SECRET_KEY isn't set: add a long random text (the same one as in your backend/.env). "
                        "It encrypts the Alpaca keys people save.")
    if not settings.cron_secret:
        problems.append("CRON_SECRET isn't set: add a long random text. It lets Vercel start the daily bot run "
                        "and stops anyone else from doing it.")
    try:
        settings.validate()
    except ValueError as exc:
        problems.append(f"Configuration error: {exc}")
    return problems


def setup_app(settings, problems: list[str]) -> Flask:
    """A stand-in site that explains what's missing (the dashboard reads it from /api/health)."""
    from .api import frontend_version
    app = Flask(__name__, static_folder=None)
    dist = Path(settings.frontend_dist)

    @app.get("/api/health")
    def health():
        return jsonify({"status": "setup", "version": __version__, "frontend_version": frontend_version(dist),
                        "mode": settings.mode, "alpaca_configured": settings.has_credentials,
                        "accounts": settings.user_accounts, "problems": problems})

    @app.get("/")
    @app.get("/<path:path>")
    def anything(path: str = ""):
        if path.startswith("api/"):
            return jsonify({"error": "This site isn't set up yet: " + " ".join(problems)}), 503
        target = dist / path
        if path and target.is_file() and dist.resolve() in target.resolve().parents:
            return send_from_directory(dist, path)
        if (dist / "index.html").exists():
            return send_from_directory(dist, "index.html")
        return "<h1>AlgoTrader isn't set up yet</h1><ul>" + "".join(f"<li>{p}</li>" for p in problems) + "</ul>"

    return app


def build_app() -> Flask:
    settings = vercel_settings()
    problems = deployment_problems(settings)
    if problems:
        import logging
        logging.getLogger("algotrader").error("Deployment isn't set up: %s", " ".join(problems))
        return setup_app(settings, problems)
    from .api import create_app
    return create_app(settings)
