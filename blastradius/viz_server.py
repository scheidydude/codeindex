"""Minimal HTTP server for the blastradius visualization UI."""
from __future__ import annotations
import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

HERE = Path(__file__).parent
VIZ_HTML = HERE.parent / "viz" / "explorer.html"

REPO_PATH  = "."
INDEX_FILE: Path = Path("blastradius.json")


def _run_analysis(repo_path: str, output: Path) -> bool:
    from blastradius.index import build
    try:
        build(repo_path, output)
        return True
    except Exception as e:
        print(f"[analyzer error] {e}", file=sys.stderr)
        return False


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        print(f"  {self.address_string()} {fmt % args}")

    def _send_json(self, data: dict, status: int = 200) -> None:
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", len(body))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, path: Path, content_type: str) -> None:
        try:
            data = path.read_bytes()
        except FileNotFoundError:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", len(data))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path = self.path.split("?")[0]

        if path in ("/", "/index.html"):
            self._send_file(VIZ_HTML, "text/html; charset=utf-8")

        elif path == "/graph":
            if INDEX_FILE.exists():
                self._send_file(INDEX_FILE, "application/json")
            else:
                self._send_json({"error": "blastradius.json not found — run: blastradius analyze <repo>"}, 404)

        elif path == "/refresh":
            ok = _run_analysis(REPO_PATH, INDEX_FILE)
            self._send_json({"ok": ok})

        else:
            self.send_error(404)


def _start_watcher(repo_path: str) -> None:
    try:
        from watchdog.observers import Observer
        from watchdog.events import FileSystemEventHandler
    except ImportError:
        print("watchdog not installed — run: pip install watchdog", file=sys.stderr)
        return

    WATCHED_EXTS = {
        ".py", ".js", ".ts", ".jsx", ".tsx", ".mjs", ".cjs",
        ".rb", ".go", ".rs", ".java", ".kt", ".php",
        ".yml", ".yaml", ".sql", ".prisma",
    }

    class _Watcher(FileSystemEventHandler):
        def __init__(self):
            self._timer = None

        def _refresh(self):
            print("[watch] change detected, re-analyzing…", file=sys.stderr)
            _run_analysis(repo_path, INDEX_FILE)

        def on_modified(self, event):
            if event.is_directory:
                return
            ext = Path(event.src_path).suffix
            if ext in WATCHED_EXTS:
                if self._timer:
                    self._timer.cancel()
                self._timer = threading.Timer(1.0, self._refresh)
                self._timer.start()

    observer = Observer()
    observer.schedule(_Watcher(), repo_path, recursive=True)
    observer.start()
    print(f"[watch] watching {repo_path}", file=sys.stderr)


def serve(repo_path: str, port: int = 8080, watch: bool = False, output: Path | None = None) -> None:
    global REPO_PATH, INDEX_FILE
    REPO_PATH  = repo_path
    INDEX_FILE = output or (Path(repo_path).resolve() / "blastradius.json")

    print(f"Analyzing {repo_path} …", file=sys.stderr)
    _run_analysis(repo_path, INDEX_FILE)

    if watch:
        _start_watcher(repo_path)

    server = HTTPServer(("", port), _Handler)
    print(f"\nServing at http://localhost:{port}/\n  repo: {repo_path}\n  index: {INDEX_FILE}\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
