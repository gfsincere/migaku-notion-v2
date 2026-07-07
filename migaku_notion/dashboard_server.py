"""Local HTTP server for the progress dashboard.

Serves a static HTML UI and a small JSON API backed by state.db.
"""
from __future__ import annotations

import json
import logging
import threading
import webbrowser
from datetime import date, datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import requests

from . import config
from .commands.sync_cmd import run_full_refresh
from .export import build_csv_bytes, filter_rows
from .migaku import auth
from .migaku.word_actions import apply_word_action
from .progress_stats import build_live_stats_payload, build_progress_payload
from .hsk.compare import build_hsk_gaps_from_cache, build_hsk_report_from_cache
from .state import StateCache


log = logging.getLogger("migaku-notion")

STATIC_DIR = Path(__file__).resolve().parent / "dashboard_static"

_sync_lock = threading.Lock()
_sync_state: dict[str, object] = {
    "running": False,
    "lang": None,
    "notion": False,
    "started_at": None,
    "finished_at": None,
    "exit_code": None,
    "error": None,
}


def _sync_status_payload() -> dict[str, object]:
    with _sync_lock:
        return dict(_sync_state)


def _start_background_sync(lang: str, *, notion: bool = False) -> tuple[int, dict[str, object]]:
    lang = lang.strip() or config.DEFAULT_LANG
    with _sync_lock:
        if _sync_state["running"]:
            return 409, {
                "error": "Sync already running",
                "sync": dict(_sync_state),
            }
        _sync_state.update({
            "running": True,
            "lang": lang,
            "notion": notion,
            "started_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            "finished_at": None,
            "exit_code": None,
            "error": None,
        })

    def _worker() -> None:
        try:
            code = run_full_refresh(lang, no_notion=not notion)
            with _sync_lock:
                _sync_state["exit_code"] = code
                if code != 0:
                    _sync_state["error"] = f"sync exited with code {code}"
        except Exception as exc:  # noqa: BLE001
            log.exception("Dashboard sync failed")
            with _sync_lock:
                _sync_state["exit_code"] = 1
                _sync_state["error"] = str(exc)
        finally:
            with _sync_lock:
                _sync_state["running"] = False
                _sync_state["finished_at"] = (
                    datetime.now(timezone.utc).replace(microsecond=0).isoformat()
                )

    threading.Thread(target=_worker, daemon=True, name="dashboard-sync").start()
    return 202, {"ok": True, "started": True, "sync": _sync_status_payload()}


def _load_progress_json(lang: str) -> bytes:
    if not config.STATE_DB_PATH.exists():
        body = {"error": "state.db not found — run sync first", "lang": lang}
        return json.dumps(body).encode("utf-8")

    with StateCache(config.STATE_DB_PATH) as cache:
        snapshots = cache.list_progress_snapshots(lang)
        payload = build_progress_payload(snapshots, lang=lang)
    return json.dumps(payload).encode("utf-8")


def _load_hsk_json(lang: str) -> bytes:
    if not config.STATE_DB_PATH.exists():
        body = {"error": "state.db not found — run sync first", "lang": lang}
        return json.dumps(body).encode("utf-8")
    try:
        with StateCache(config.STATE_DB_PATH) as cache:
            payload = build_hsk_report_from_cache(cache, lang)
    except Exception as exc:  # noqa: BLE001
        payload = {"error": str(exc), "lang": lang}
    return json.dumps(payload).encode("utf-8")


def _load_hsk_gaps_json(lang: str, standard: str, mode: str) -> bytes:
    if not config.STATE_DB_PATH.exists():
        body = {"error": "state.db not found — run sync first", "lang": lang}
        return json.dumps(body).encode("utf-8")
    try:
        with StateCache(config.STATE_DB_PATH) as cache:
            payload = build_hsk_gaps_from_cache(
                cache, lang, standard=standard, mode=mode,
            )
    except ValueError as exc:
        payload = {"error": str(exc), "lang": lang}
    except Exception as exc:  # noqa: BLE001
        payload = {"error": str(exc), "lang": lang}
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


def _post_word_action_json(body: bytes) -> tuple[int, bytes]:
    try:
        req = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return 400, json.dumps({"error": "Invalid JSON body"}).encode("utf-8")

    word = (req.get("word") or "").strip()
    lang = (req.get("lang") or config.DEFAULT_LANG).strip()
    action = (req.get("action") or req.get("status") or "KNOWN").strip().upper()

    if not word:
        return 400, json.dumps({"error": "word is required"}).encode("utf-8")

    if not config.STATE_DB_PATH.exists():
        return 400, json.dumps({"error": "state.db not found — run sync first"}).encode("utf-8")

    try:
        session = auth.auth_session_from_env(
            refresh_token=config.migaku_refresh_token(),
            email=config.migaku_email(),
            password=config.migaku_password(),
        )
    except RuntimeError as exc:
        return 401, json.dumps({"error": str(exc)}).encode("utf-8")

    try:
        with StateCache(config.STATE_DB_PATH) as cache:
            payload = apply_word_action(
                session,
                cache,
                dict_form=word,
                lang=lang,
                action=action,
                optimistic=True,
            )
            standard = (req.get("gaps_standard") or "hsk30").strip()
            mode = (req.get("gaps_mode") or "exclusive").strip()
            payload["live"] = build_live_stats_payload(cache, lang)
            payload["gaps"] = build_hsk_gaps_from_cache(
                cache, lang, standard=standard, mode=mode,
            )
    except ValueError as exc:
        return 400, json.dumps({"error": str(exc)}).encode("utf-8")
    except RuntimeError as exc:
        return 502, json.dumps({"error": str(exc)}).encode("utf-8")
    except requests.RequestException:
        return 504, json.dumps({
            "error": "Migaku is not responding — try again in a moment.",
        }).encode("utf-8")

    return 200, json.dumps(payload, ensure_ascii=False).encode("utf-8")


def _load_vocab_csv(lang: str, status_param: str) -> tuple[int, bytes, str]:
    if not config.STATE_DB_PATH.exists():
        return 400, b"state.db not found - run sync first", "text/plain; charset=utf-8"

    statuses = [s.strip().upper() for s in status_param.split(",") if s.strip()]
    if not statuses:
        statuses = ["KNOWN", "LEARNING"]

    with StateCache(config.STATE_DB_PATH) as cache:
        rows = filter_rows(
            list(cache.load_all().values()),
            lang or None,
            statuses,
            include_archived=False,
        )

    if not rows:
        return 404, b"No rows match export filter", "text/plain; charset=utf-8"

    status_slug = "-".join(s.lower() for s in statuses)
    filename = f"migaku-vocab-{lang}-{status_slug}-{date.today().isoformat()}.csv"
    return 200, build_csv_bytes(rows), filename


class DashboardHandler(BaseHTTPRequestHandler):
    server_version = "migaku-notion-dashboard/0.1"

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"

        if path == "/api/progress":
            lang = parse_qs(parsed.query).get("lang", [config.DEFAULT_LANG])[0]
            data = _load_progress_json(lang)
            self._send(200, data, "application/json; charset=utf-8")
            return

        if path == "/api/hsk":
            lang = parse_qs(parsed.query).get("lang", [config.DEFAULT_LANG])[0]
            data = _load_hsk_json(lang)
            self._send(200, data, "application/json; charset=utf-8")
            return

        if path == "/api/hsk/gaps":
            qs = parse_qs(parsed.query)
            lang = qs.get("lang", [config.DEFAULT_LANG])[0]
            standard = qs.get("standard", ["hsk30"])[0]
            mode = qs.get("mode", ["exclusive"])[0]
            data = _load_hsk_gaps_json(lang, standard, mode)
            self._send(200, data, "application/json; charset=utf-8")
            return

        if path == "/api/export.csv":
            qs = parse_qs(parsed.query)
            lang = qs.get("lang", [config.DEFAULT_LANG])[0]
            status = qs.get("status", ["KNOWN,LEARNING"])[0]
            code, data, filename = _load_vocab_csv(lang, status)
            if code != 200:
                self._send(code, data, "text/plain; charset=utf-8")
                return
            self._send_download(
                200,
                data,
                "text/csv; charset=utf-8",
                filename,
            )
            return

        if path == "/api/sync/status":
            payload = {"sync": _sync_status_payload()}
            self._send(200, json.dumps(payload).encode("utf-8"), "application/json; charset=utf-8")
            return

        if path in ("/", "/index.html"):
            self._serve_file(STATIC_DIR / "index.html", "text/html; charset=utf-8")
            return

        if path.startswith("/static/"):
            rel = path[len("/static/") :]
            candidate = (STATIC_DIR / rel).resolve()
            if not str(candidate).startswith(str(STATIC_DIR.resolve())):
                self._send(403, b"Forbidden", "text/plain")
                return
            if candidate.is_file():
                ctype = "text/css" if candidate.suffix == ".css" else "application/octet-stream"
                self._serve_file(candidate, ctype)
                return

        self._send(404, b"Not found", "text/plain")

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"

        if path in ("/api/word/status", "/api/word/action"):
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length) if length else b"{}"
            code, data = _post_word_action_json(body)
            self._send(code, data, "application/json; charset=utf-8")
            return

        if path == "/api/sync":
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length) if length else b"{}"
            try:
                req = json.loads(body.decode("utf-8") or "{}")
            except (UnicodeDecodeError, json.JSONDecodeError):
                req = {}
            lang = (req.get("lang") or config.DEFAULT_LANG).strip()
            notion = bool(req.get("notion"))
            code, payload = _start_background_sync(lang, notion=notion)
            self._send(
                code,
                json.dumps(payload).encode("utf-8"),
                "application/json; charset=utf-8",
            )
            return

        self._send(404, b"Not found", "text/plain")

    def _serve_file(self, path: Path, content_type: str) -> None:
        if not path.is_file():
            self._send(404, b"Not found", "text/plain")
            return
        self._send(200, path.read_bytes(), content_type)

    def _send(self, code: int, body: bytes, content_type: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_download(
        self,
        code: int,
        body: bytes,
        content_type: str,
        filename: str,
    ) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args: object) -> None:
        log.debug("dashboard %s - %s", self.address_string(), fmt % args)


def serve_dashboard(
    *,
    host: str = "127.0.0.1",
    port: int = 59009,
    lang: str = config.DEFAULT_LANG,
    open_browser: bool = True,
) -> int:
    if not STATIC_DIR.is_dir():
        log.error("Dashboard assets missing at %s", STATIC_DIR)
        return 1

    url = f"http://{host}:{port}/?lang={lang}"
    httpd = ThreadingHTTPServer((host, port), DashboardHandler)
    log.info("Progress dashboard at %s (Ctrl+C to stop)", url)
    print(f"\n  {url}\n")
    if open_browser:
        webbrowser.open(url)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        log.info("Dashboard stopped.")
    finally:
        httpd.server_close()
    return 0
