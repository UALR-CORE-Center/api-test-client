"""Loopback-only web interface. API calls are executed by the Python process."""

from __future__ import annotations

import copy
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import secrets
import threading
from urllib.parse import unquote, urlsplit

from . import __version__
from .engine import run_request, run_sequence
from .workspace import MAX_WORKSPACE_BYTES, WorkspaceStore, export_workspace, validate_workspace

STATIC = Path(__file__).parent / "static"


def select_environment(workspace, identifier=None):
    identifier = identifier or workspace.get("active_environment_id")
    matches = [e for e in workspace["environments"] if e["id"] == identifier]
    if not matches:
        matches = [e for e in workspace["environments"] if e["name"] == identifier]
    if len(matches) != 1:
        raise ValueError("Choose an existing environment by its unique name or id.")
    return matches[0]


class ClientServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, workspace_path):
        if address[0] != "127.0.0.1":
            raise ValueError("The API client must bind to 127.0.0.1.")
        super().__init__(address, ClientHandler)
        self.token = secrets.token_urlsafe(32)
        self.store = WorkspaceStore(workspace_path, f"http://127.0.0.1:{self.server_port}")
        self.jobs = {}
        self.jobs_lock = threading.RLock()
        self.request_slots = threading.BoundedSemaphore(8)

    def start_sequence(self, sequence, workspace, environment, variables):
        with self.jobs_lock:
            if sum(j["state"] == "running" for j in self.jobs.values()) >= 4:
                raise ValueError("Four sequences are already running. Wait for a run to finish.")
            for id_ in list(self.jobs):
                if len(self.jobs) >= 50 and self.jobs[id_]["state"] == "completed":
                    del self.jobs[id_]
            id_ = secrets.token_urlsafe(12)
            job = {"state": "running", "result": None, "cancel": threading.Event()}
            self.jobs[id_] = job

        def execute():
            def progress(snapshot):
                with self.jobs_lock:
                    job["result"] = snapshot
            try:
                result = run_sequence(sequence, workspace["requests"], environment,
                                      variables=variables, globals=workspace["variables"], cancel_event=job["cancel"],
                                      progress_callback=progress)
            except Exception:
                result = {"ok": False, "steps": [], "variables": {}, "error": "Sequence execution failed.", "cancelled": False}
            with self.jobs_lock:
                job["result"] = result
                job["state"] = "completed"
        threading.Thread(target=execute, name=f"sequence-{id_}", daemon=True).start()
        return id_

    def server_close(self):
        with getattr(self, "jobs_lock", threading.RLock()):
            for job in getattr(self, "jobs", {}).values():
                job["cancel"].set()
        super().server_close()


class ClientHandler(BaseHTTPRequestHandler):
    server_version = f"API-Test-Client/{__version__}"

    def setup(self):
        super().setup()
        self.connection.settimeout(15)

    def log_message(self, format, *args):
        # URLs/payloads may contain credentials. Keep the local server quiet.
        pass

    def respond(self, status, value, content_type="application/json; charset=utf-8", extra=None):
        data = value if isinstance(value, bytes) else json.dumps(value, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'")
        if extra:
            for key, item in extra.items():
                self.send_header(key, item)
        self.end_headers()
        if self.command != "HEAD":
            try:
                self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError):
                pass

    def check_browser(self):
        allowed = {f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}"}
        host = self.headers.get("Host", "")
        if host not in allowed:
            self.respond(403, {"error": "Untrusted Host header."})
            return False
        origin = self.headers.get("Origin")
        if origin and origin != f"http://{host}":
            self.respond(403, {"error": "Cross-origin requests are not permitted."})
            return False
        if self.headers.get("Sec-Fetch-Site") == "cross-site":
            self.respond(403, {"error": "Cross-origin requests are not permitted."})
            return False
        return True

    def check_token(self):
        if not hmac.compare_digest(self.headers.get("X-Client-Token", ""), self.server.token):
            self.respond(403, {"error": "Invalid session token. Reload the app."})
            return False
        return True

    def read_json(self):
        if self.headers.get("Transfer-Encoding"):
            raise ValueError("Chunked API payloads are not supported.")
        if self.headers.get_content_type() != "application/json":
            raise ValueError("Send an application/json payload.")
        try:
            size = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise ValueError("Invalid Content-Length.") from exc
        if not 0 < size <= MAX_WORKSPACE_BYTES:
            raise ValueError("Payload must contain JSON and be at most 4 MiB.")
        data = self.rfile.read(size)
        if len(data) != size:
            raise ValueError("Incomplete JSON payload.")
        value = json.loads(data)
        if not isinstance(value, dict):
            raise ValueError("Payload must be a JSON object.")
        return value

    def do_GET(self):
        self.handle_route()

    def do_POST(self):
        self.handle_route()

    def do_PUT(self):
        self.handle_route()

    def do_HEAD(self):
        self.handle_route()

    def handle_route(self):
        if not self.check_browser():
            return
        path = urlsplit(self.path).path
        if path in {"/static/styles.css", "/static/app.js"}:
            path = path.removeprefix("/static")
        try:
            if self.command in {"GET", "HEAD"} and path in {"/", "/index.html", "/styles.css", "/app.js"}:
                filename, content_type = {"/": ("index.html", "text/html; charset=utf-8"),
                                          "/index.html": ("index.html", "text/html; charset=utf-8"),
                                          "/styles.css": ("styles.css", "text/css; charset=utf-8"),
                                          "/app.js": ("app.js", "application/javascript; charset=utf-8")}[path]
                self.respond(200, (STATIC / filename).read_bytes(), content_type)
                return
            if self.command == "GET" and path == "/api/session":
                self.respond(200, {"token": self.server.token})
                return
            # A tiny in-process demo API makes the initial workspace runnable offline.
            if self.command in {"GET", "HEAD"} and path == "/demo/health":
                self.respond(200, {"status": "healthy", "id": "demo-123", "service": "API Test Client demo"})
                return
            if self.command == "GET" and path.startswith("/demo/items/"):
                id_ = unquote(path[len("/demo/items/"):])
                self.respond(200, {"id": id_, "name": "Example item", "ready": True})
                return
            if self.command == "POST" and path == "/demo/echo":
                self.respond(200, {"received": self.read_json(), "method": "POST"})
                return
            if not path.startswith("/api/"):
                self.respond(404, {"error": "Not found."})
                return
            if not self.check_token():
                return
            if self.command == "GET" and path == "/api/workspace":
                self.respond(200, self.server.store.load())
            elif self.command == "PUT" and path == "/api/workspace":
                self.respond(200, self.server.store.save(self.read_json()))
            elif self.command == "GET" and path == "/api/export":
                self.respond(200, export_workspace(self.server.store.load()), extra={"Content-Disposition": 'attachment; filename="api-test-client-workspace.json"'})
            elif self.command == "POST" and path == "/api/import":
                self.respond(200, self.server.store.save(self.read_json().get("workspace")))
            elif self.command == "POST" and path in {"/api/run", "/api/sequences/run"}:
                payload = self.read_json()
                workspace = self.server.store.load()
                environment = select_environment(workspace, payload.get("environment_id"))
                variables = payload.get("variables", {})
                if not isinstance(variables, dict):
                    raise ValueError("Run variables must be an object.")
                if path == "/api/run":
                    request = payload.get("request")
                    if not isinstance(request, dict):
                        raise ValueError("Run requires a request object.")
                    candidate = copy.deepcopy(workspace)
                    candidate["requests"] = [{"id": "inline-request", "name": "Request", **request}]
                    candidate["sequences"] = []
                    request = validate_workspace(candidate)["requests"][0]
                    if not self.server.request_slots.acquire(blocking=False):
                        self.respond(429, {"error": "Too many requests are running."})
                        return
                    try:
                        response = run_request(request, environment, variables=variables, globals=workspace["variables"])
                    finally:
                        self.server.request_slots.release()
                    self.respond(200, response)
                else:
                    sequence = payload.get("sequence")
                    if not isinstance(sequence, dict) or not sequence.get("steps"):
                        raise ValueError("Add at least one request to the sequence.")
                    candidate = copy.deepcopy(workspace)
                    candidate["sequences"] = [{"id": "inline-sequence", "name": "Sequence", **sequence}]
                    sequence = validate_workspace(candidate)["sequences"][0]
                    self.respond(202, {"run_id": self.server.start_sequence(sequence, workspace, environment, variables)})
            elif path.startswith("/api/runs/"):
                segments = path.strip("/").split("/")
                id_ = segments[2] if len(segments) >= 3 else ""
                with self.server.jobs_lock:
                    job = self.server.jobs.get(id_)
                    if job is None:
                        self.respond(404, {"error": "Run not found."})
                    elif self.command == "GET" and len(segments) == 3:
                        self.respond(200, {"state": job["state"], "result": job["result"]})
                    elif self.command == "POST" and segments[3:] == ["cancel"]:
                        job["cancel"].set()
                        self.respond(200, {"cancelled": True})
                    else:
                        self.respond(404, {"error": "Not found."})
            else:
                self.respond(404, {"error": "Not found."})
        except (ValueError, TypeError, KeyError, RecursionError) as exc:
            self.respond(400, {"error": str(exc) or "Invalid payload."})
        except TimeoutError:
            self.respond(408, {"error": "Timed out while reading the API payload."})
        except OSError:
            self.respond(500, {"error": "Unable to read or write the local workspace."})


def serve(workspace_path, port=8765, open_browser=True):
    import webbrowser
    server = ClientServer(("127.0.0.1", port), workspace_path)
    url = f"http://127.0.0.1:{server.server_port}"
    print(f"API Test Client: {url}\nWorkspace: {server.store.path}\nPress Ctrl+C to stop.")
    if open_browser:
        threading.Timer(0.3, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        server.server_close()
