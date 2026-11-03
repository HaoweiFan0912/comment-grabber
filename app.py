"""Local web UI: run python app.py and it opens in your browser.

It only listens on 127.0.0.1, so other devices on your network can't reach it.
"""

from __future__ import annotations

import argparse
import logging
import socket
import subprocess
import sys
import threading
import webbrowser
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Optional

from flask import Flask, abort, jsonify, request

from fetcher import (
    DAILY_QUOTA,
    DEFAULT_OUTPUT_DIR,
    MAX_COMMENTS_LIMIT,
    MAX_VIDEOS_LIMIT,
    CommentFetcher,
    FetchOptions,
    load_api_key,
)

HOST = "127.0.0.1"
DEFAULT_PORT = 8765
IS_MAC = sys.platform == "darwin"

PICK_FOLDER_SCRIPT = """
on run argv
    activate
    set promptText to "Choose a folder for the comment files"
    if (count of argv) > 0 then
        set startFolder to POSIX file (item 1 of argv)
        return POSIX path of (choose folder with prompt promptText default location startFolder)
    end if
    return POSIX path of (choose folder with prompt promptText)
end run
"""

app = Flask(__name__, static_folder="static", static_url_path="/static")


class Job:
    """Runs one fetch at a time; the page polls it for progress and log lines."""

    def __init__(self):
        self.lock = threading.Lock()
        self.fetcher: Optional[CommentFetcher] = None
        self.logs: list = []
        self.running = False

    def start(self, api_key: str, options: FetchOptions) -> bool:
        with self.lock:
            if self.running:
                return False
            self.running = True
            self.logs = []
            self.fetcher = CommentFetcher(api_key, options, log=self.add_log)
        threading.Thread(target=self._run, daemon=True).start()
        return True

    def _run(self) -> None:
        fetcher = self.fetcher
        try:
            fetcher.run()
        except Exception as e:  # last resort: make sure the page learns the job has ended
            message = f"Unexpected error: {e}".replace(fetcher.api_key, "***")
            fetcher.result.status, fetcher.result.message = "failed", message
            self.add_log("error", message)
        finally:
            with self.lock:
                self.running = False

    def add_log(self, level: str, message: str) -> None:
        with self.lock:
            self.logs.append({"time": datetime.now().strftime("%H:%M:%S"), "level": level, "message": message})

    def stop(self) -> None:
        with self.lock:
            if self.running:
                self.fetcher.stop()

    def snapshot(self, since: int) -> dict:
        with self.lock:
            fetcher = self.fetcher
            return {
                "running": self.running,
                "logs": self.logs[since:],
                "log_count": len(self.logs),
                "progress": asdict(fetcher.progress) if fetcher else None,
                "result": asdict(fetcher.result) if fetcher and not self.running else None,
                "filtering_comments": bool(fetcher and fetcher.options.comment_must_include),
            }

    def last_file(self) -> Optional[str]:
        with self.lock:
            if self.fetcher is None or self.running:
                return None
            return self.fetcher.result.excel_path or self.fetcher.result.output_path


job = Job()


@app.before_request
def reject_foreign_requests():
    # Only serve this app's own page, so other websites can't use your browser to call the local API
    allowed = app.config["ALLOWED_HOSTS"]
    origin = request.headers.get("Origin")
    if request.host not in allowed or (origin and origin.split("://", 1)[-1] not in allowed):
        abort(403)


@app.get("/")
def index():
    return app.send_static_file("index.html")


@app.get("/api/config")
def config():
    return jsonify(
        key_ready=load_api_key() is not None,
        default_dir=str(DEFAULT_OUTPUT_DIR),
        daily_quota=DAILY_QUOTA,
        max_videos=MAX_VIDEOS_LIMIT,
        max_comments=MAX_COMMENTS_LIMIT,
        can_pick_folder=IS_MAC,
        can_reveal=IS_MAC,
    )


@app.get("/api/status")
def status():
    return jsonify(job.snapshot(request.args.get("since", 0, type=int)))


@app.post("/api/start")
def start():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify(error="Invalid request"), 400
    try:
        options = FetchOptions.from_dict(data)
        options.validate()
    except ValueError as e:
        return jsonify(error=str(e)), 400
    api_key = load_api_key()
    if not api_key:
        return jsonify(error="No API key yet: set YOUTUBE_API_KEY in .env, save it, and try again"), 400
    if not job.start(api_key, options):
        return jsonify(error="A job is already running"), 409
    return jsonify(ok=True)


@app.post("/api/stop")
def stop():
    job.stop()
    return jsonify(ok=True)


@app.post("/api/pick-folder")
def pick_folder():
    if not IS_MAC:
        return jsonify(error="The folder picker isn't available on this system — type the folder path instead"), 400
    data = request.get_json(silent=True) or {}
    args = ["osascript", "-"]
    current = str(data.get("current", "")).strip()
    if current and Path(current).expanduser().is_dir():
        args.append(str(Path(current).expanduser()))
    proc = subprocess.run(args, input=PICK_FOLDER_SCRIPT, capture_output=True, text=True)
    if proc.returncode != 0:
        if "-128" in proc.stderr:  # the user pressed Cancel
            return jsonify(cancelled=True)
        return jsonify(error=f"Couldn't open the folder picker: {proc.stderr.strip()}"), 500
    return jsonify(path=proc.stdout.strip().rstrip("/") or "/")


@app.post("/api/reveal")
def reveal():
    path = job.last_file()
    if not IS_MAC or not path or not Path(path).exists():
        return jsonify(error="File not found"), 404
    subprocess.run(["open", "-R", path], check=False)
    return jsonify(ok=True)


def _pick_port(preferred: int) -> int:
    with socket.socket() as s:
        try:
            s.bind((HOST, preferred))
            return preferred
        except OSError:
            pass
    with socket.socket() as s:
        s.bind((HOST, 0))
        return s.getsockname()[1]


def main() -> None:
    parser = argparse.ArgumentParser(description="Comment Grabber web UI")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--no-browser", action="store_true", help="don't open the browser automatically")
    args = parser.parse_args()

    port = _pick_port(args.port)
    app.config["ALLOWED_HOSTS"] = {f"{HOST}:{port}", f"localhost:{port}"}
    url = f"http://{HOST}:{port}"
    logging.getLogger("werkzeug").setLevel(logging.WARNING)  # don't print every polling request
    print(f"Comment Grabber is running at {url}")
    print("Close this window or press Ctrl+C to quit.")
    if not args.no_browser:
        threading.Timer(1.0, webbrowser.open, args=(url,)).start()
    app.run(host=HOST, port=port, threaded=True)


if __name__ == "__main__":
    main()
