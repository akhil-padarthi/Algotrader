"""Vercel entry point: Vercel looks for a Flask app named `app` in this file.

The real code is in backend/algotrader (see backend/algotrader/vercel.py and DEPLOYMENT.md, Option D).
For running on your own computer use `python -m algotrader serve` in the backend folder instead.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "backend"))

from algotrader.vercel import build_app  # noqa: E402

app = build_app()
