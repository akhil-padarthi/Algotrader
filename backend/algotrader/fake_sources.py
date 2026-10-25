"""Stand-in for FRED (St. Louis Fed) used by the tests and the offline demo."""
from __future__ import annotations

import pandas as pd
from flask import Flask, Response, jsonify, request


class FakeFred:
    """Serves a DataFrame (index = date, one column per series) the way FRED does:
    /graph/fredgraph.csv?id=X&cosd=... (no key) and /fred/series/observations (with a key).
    ``old_style`` reproduces FRED's older CSV ("DATE" header, "." for missing values)."""

    def __init__(self, macro: pd.DataFrame, api_key: str = "fred-test-key", old_style: bool = False):
        self.macro = macro.sort_index()
        self.api_key = api_key
        self.old_style = old_style
        self.requests: list[str] = []
        self.down = False
        self.app = self._make_app()

    def _series(self, sid: str, start: str | None) -> pd.Series | None:
        if sid not in self.macro:
            return None
        s = self.macro[sid]
        return s[s.index >= pd.Timestamp(start)] if start else s

    def _make_app(self) -> Flask:
        app = Flask("fake_fred")
        ff = self

        @app.before_request
        def log_request():
            ff.requests.append(request.full_path)
            if ff.down:
                return Response("Service Unavailable", 503)
            return None

        @app.get("/graph/fredgraph.csv")
        def csv():
            sid = request.args.get("id", "")
            s = ff._series(sid, request.args.get("cosd"))
            if s is None:
                return Response("Bad Request. The series does not exist.", 404)
            head = "DATE" if ff.old_style else "observation_date"
            blank = "." if ff.old_style else ""
            lines = [f"{head},{sid}"] + [f"{d:%Y-%m-%d},{blank if pd.isna(v) else v}" for d, v in s.items()]
            return Response("\n".join(lines) + "\n", mimetype="text/csv")

        @app.get("/fred/series/observations")
        def observations():
            if request.args.get("api_key") != ff.api_key:
                return jsonify({"error_code": 400, "error_message": "Bad Request. The value for variable api_key is not registered."}), 400
            s = ff._series(request.args.get("series_id", ""), request.args.get("observation_start"))
            if s is None:
                return jsonify({"error_code": 400, "error_message": "Bad Request. The series does not exist."}), 400
            return jsonify({"observations": [{"date": f"{d:%Y-%m-%d}", "value": "." if pd.isna(v) else str(v)}
                                             for d, v in s.items()]})

        return app
