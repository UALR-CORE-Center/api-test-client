"""Versioned local workspaces, atomic saves, and credential-aware exports."""

from __future__ import annotations

import copy
import base64
import json
import os
from pathlib import Path
import re
import tempfile
import threading
from urllib.parse import parse_qsl, quote, quote_plus, urlsplit, urlunsplit

SECRET_NAME = re.compile(r"authorization|cookie|password|passwd|secret|token|api[-_]?key", re.I)
MAX_WORKSPACE_BYTES = 4 * 1024 * 1024


def _encode_query(query):
    def encode(value):
        parts = re.split(r"(\{\{[^{}]+\}\})", value)
        return "".join(part if part.startswith("{{") and part.endswith("}}") else quote_plus(part, safe="") for part in parts)
    return "&".join(f"{encode(key)}={encode(value)}" for key, value in query)


def default_path() -> Path:
    return Path(os.environ.get("API_TEST_CLIENT_WORKSPACE", Path.home() / ".api-test-client" / "workspace.json")).expanduser()


def new_workspace(base_url: str = "http://127.0.0.1:8765") -> dict:
    def request(id_, name, method, url, **extra):
        return {"id": id_, "name": name, "method": method, "url": url,
                "params": [], "headers": [], "body_type": "none", "body": "",
                "auth": {"type": "inherit"}, "timeout": 30, "follow_redirects": False,
                "assertions": [{"type": "status", "expected": 200}], "extract": [], **extra}
    return {
        "version": 1,
        "active_environment_id": "local-demo",
        "variables": [],
        "environments": [{"id": "local-demo", "name": "Local demo", "base_url": base_url,
                          "api_key": "", "auth": {"type": "none"},
                          "variables": [{"key": "message", "value": "Hello from API Test Client", "enabled": True, "secret": False}]}],
        "requests": [
            request("demo-health", "Check API health", "GET", "{{base_url}}/demo/health",
                    extract=[{"name": "demo_id", "path": "$.id"}]),
            request("demo-echo", "Send a JSON payload", "POST", "{{base_url}}/demo/echo",
                    body_type="json", body='{"message": "{{message}}"}'),
            request("demo-chain", "Use a previous response", "GET", "{{base_url}}/demo/items/{{demo_id}}",
                    assertions=[{"type": "status", "expected": 200},
                                {"type": "json_equals", "path": "$.id", "expected": "demo-123"}]),
        ],
        "sequences": [{"id": "demo-sequence", "name": "Health → fetch item", "stop_on_failure": True,
                       "steps": [{"id": "step-health", "request_id": "demo-health", "variables": {}},
                                 {"id": "step-item", "request_id": "demo-chain", "variables": {}}]}],
    }


def validate_workspace(value: dict) -> dict:
    """Reject malformed imported collections before touching the current file."""
    if not isinstance(value, dict) or value.get("version") != 1:
        raise ValueError("Workspace must be an object with version: 1.")
    value = copy.deepcopy(value)

    def rows(items, label):
        if not isinstance(items, list) or len(items) > 1000:
            raise ValueError(f"{label} must be a list with at most 1,000 entries.")
        for row in items:
            if not isinstance(row, dict) or not isinstance(row.get("key", ""), str) or not isinstance(row.get("value", ""), str):
                raise ValueError(f"{label} entries need string key and value fields.")
            if not isinstance(row.get("enabled", True), bool) or not isinstance(row.get("secret", False), bool):
                raise ValueError(f"{label} enabled/secret fields must be booleans.")

    def collection(key):
        items = value.setdefault(key, [])
        if not isinstance(items, list) or len(items) > 1000:
            raise ValueError(f"{key} must be a list with at most 1,000 entries.")
        ids = set()
        for item in items:
            if not isinstance(item, dict) or not isinstance(item.get("id"), str) or not item["id"] or len(item["id"]) > 200:
                raise ValueError(f"Each {key} entry needs a nonempty string id.")
            if item["id"] in ids:
                raise ValueError(f"Duplicate {key} id: {item['id']}")
            ids.add(item["id"])
            if not isinstance(item.get("name", ""), str):
                raise ValueError(f"{key} names must be strings.")
        return items, ids

    def auth(item, inherit=False):
        a = item.setdefault("auth", {"type": "inherit" if inherit else "none"})
        if not isinstance(a, dict) or a.get("type", "none") not in ({"inherit"} if inherit else set()) | {"none", "bearer", "api_key", "basic"}:
            raise ValueError("Unsupported authentication type.")
        for k, v in a.items():
            if not isinstance(v, str):
                raise ValueError("Authentication fields must be strings.")
        if a.get("location", "header") not in {"header", "query"}:
            raise ValueError("API key location must be header or query.")

    rows(value.setdefault("variables", []), "Global variables")
    environments, environment_ids = collection("environments")
    for env in environments:
        for key in ("base_url", "api_key"):
            if not isinstance(env.setdefault(key, ""), str):
                raise ValueError(f"Environment {key} must be a string.")
        rows(env.setdefault("variables", []), "Environment variables")
        auth(env)
    requests, request_ids = collection("requests")
    for request in requests:
        if request.get("method", "GET") not in {"GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"}:
            raise ValueError("Unsupported HTTP method.")
        for key in ("url", "body"):
            if not isinstance(request.setdefault(key, ""), str):
                raise ValueError(f"Request {key} must be a string.")
        if request.get("body_type", "none") not in {"none", "json", "text", "form"}:
            raise ValueError("Unsupported request body type.")
        timeout = request.get("timeout", 30)
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 1 <= timeout <= 300:
            raise ValueError("Request timeout must be between 1 and 300 seconds.")
        if not isinstance(request.get("follow_redirects", False), bool):
            raise ValueError("follow_redirects must be a boolean.")
        rows(request.setdefault("params", []), "Query parameters")
        rows(request.setdefault("headers", []), "Headers")
        auth(request, True)
        for field in ("assertions", "extract"):
            if not isinstance(request.setdefault(field, []), list) or len(request[field]) > 1000 or any(not isinstance(row, dict) for row in request[field]):
                raise ValueError(f"Request {field} must be a list of objects.")
    sequences, _ = collection("sequences")
    for sequence in sequences:
        if not isinstance(sequence.get("stop_on_failure", True), bool):
            raise ValueError("stop_on_failure must be a boolean.")
        steps = sequence.setdefault("steps", [])
        if not isinstance(steps, list) or len(steps) > 1000:
            raise ValueError("Sequence steps must be a list with at most 1,000 entries.")
        for step in steps:
            if not isinstance(step, dict) or step.get("request_id") not in request_ids:
                raise ValueError("A sequence step references an unknown request.")
            if not isinstance(step.get("required", False), bool):
                raise ValueError("Step required must be a boolean.")
            if not isinstance(step.setdefault("variables", {}), dict):
                raise ValueError("Step variables must be a JSON object.")
    if value.get("active_environment_id") not in environment_ids:
        value["active_environment_id"] = environments[0]["id"] if environments else None
    if len(json.dumps(value).encode()) > MAX_WORKSPACE_BYTES:
        raise ValueError("Workspace exceeds the 4 MiB limit.")
    return value


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class WorkspaceStore:
    def __init__(self, path: Path, base_url="http://127.0.0.1:8765"):
        self.path = Path(path).expanduser()
        self.lock = threading.RLock()
        if not self.path.exists():
            self.save(new_workspace(base_url))
        else:
            self.load()  # Fail on corruption without replacing a user's workspace.

    def load(self):
        with self.lock:
            if self.path.stat().st_size > MAX_WORKSPACE_BYTES:
                raise ValueError("Workspace exceeds the 4 MiB limit.")
            return validate_workspace(json.loads(self.path.read_text(encoding="utf-8")))

    def save(self, workspace):
        workspace = validate_workspace(workspace)
        with self.lock:
            atomic_write(self.path, json.dumps(workspace, indent=2, ensure_ascii=False) + "\n")
        return workspace


def _known_secrets(workspace):
    # Use the request engine's interpolation rules, including JSON modifiers,
    # so redaction recognizes the credentials that were actually transmitted.
    from .engine import _interpolate

    secrets = set()

    def variables(rows):
        if isinstance(rows, dict):
            return rows
        return {row.get("key", ""): row.get("value", "") for row in rows if isinstance(row, dict) and row.get("enabled", True)}

    global_variables = variables(workspace.get("variables", []))
    runtime = variables(workspace.get("runtime", []))
    environments = workspace.get("environments", [])
    environment_contexts = [(environment, {
        **global_variables, **variables(environment.get("variables", [])),
        "base_url": environment.get("base_url", ""), "api_key": environment.get("api_key", ""),
        **runtime,
    }) for environment in environments]
    contexts = [{**global_variables, **runtime}] + [context for _, context in environment_contexts]
    secret_names = {row.get("key") for rows in [workspace.get("variables", []), workspace.get("runtime", [])] +
                    [environment.get("variables", []) for environment in environments]
                    for row in rows if isinstance(row, dict) and
                    (row.get("secret") or SECRET_NAME.search(str(row.get("key", ""))))}
    secret_names.add("api_key")

    def remember(value, context):
        try:
            resolved = _interpolate(value, context)
        except (ValueError, TypeError, RecursionError):
            return  # Missing/cyclic variables cannot produce a sent credential.
        if resolved and "{{" not in resolved:
            secrets.add(resolved)

    def remember_overrides(context):
        for name in secret_names:
            if name in context:
                remember(context[name], context)

    def visit(value, contexts):
        if isinstance(value, list):
            for item in value:
                visit(item, contexts)
        elif isinstance(value, dict):
            if value.get("secret") or SECRET_NAME.search(str(value.get("key", ""))):
                for context in contexts:
                    remember(value.get("value", ""), context)
            for key, item in value.items():
                if SECRET_NAME.search(str(key)) and isinstance(item, str):
                    for context in contexts:
                        remember(item, context)
                if key == "auth" and isinstance(item, dict):
                    for field in ("token", "value", "password"):
                        if item.get(field):
                            for context in contexts:
                                remember(item[field], context)
                    if item.get("type") == "basic":
                        for context in contexts:
                            try:
                                username = _interpolate(item.get("username", ""), context)
                                password = _interpolate(item.get("password", ""), context)
                            except (ValueError, TypeError, RecursionError):
                                continue
                            if password and "{{" not in username + password:
                                secrets.add(base64.b64encode(f"{username}:{password}".encode()).decode())
                visit(item, contexts)

    visit(workspace, contexts)
    for context in contexts:
        remember_overrides(context)

    # A run freezes its selected environment and inline sequence. Otherwise,
    # consider saved sequence overrides when preparing a portable workspace.
    selected_environment = workspace.get("run_environment")
    if isinstance(selected_environment, dict):
        environment_contexts = [(selected_environment, {
            **global_variables, **variables(selected_environment.get("variables", [])),
            "base_url": selected_environment.get("base_url", ""),
            "api_key": selected_environment.get("api_key", ""), **runtime,
        })]
    elif not environment_contexts:
        environment_contexts = [({}, contexts[0])]
    inline_sequence = workspace.get("run_sequence")
    sequences = [inline_sequence] if isinstance(inline_sequence, dict) else workspace.get("sequences", [])
    requests = {request.get("id"): request for request in workspace.get("requests", [])}
    for sequence in sequences:
        for step in sequence.get("steps", []):
            for environment, context in environment_contexts:
                effective = {**context, **variables(step.get("variables", {}))}
                remember_overrides(effective)
                visit(step, [effective])
                visit(requests.get(step.get("request_id"), {}), [effective])
                visit({"auth": environment.get("auth", {})}, [effective])
    return sorted(secrets, key=len, reverse=True)


def _json_secret_variants(secrets):
    """Credentials can also appear inside serialized JSON string values."""
    return {variant for secret in secrets for variant in (
        secret, json.dumps(secret, ensure_ascii=False)[1:-1], json.dumps(secret, ensure_ascii=True)[1:-1])}


def redact_report(value, workspace):
    """Mask known credentials and common response token fields in CLI reports."""
    raw_secrets = _json_secret_variants(_known_secrets(workspace))
    secrets = sorted({variant for secret in raw_secrets for variant in (secret, quote(secret, safe=""), quote_plus(secret, safe=""))}, key=len, reverse=True)
    credential_params = {item.get("auth", {}).get("header", "X-API-Key").lower()
                         for item in workspace.get("environments", []) + workspace.get("requests", [])
                         if item.get("auth", {}).get("type") == "api_key" and item.get("auth", {}).get("location") == "query"}
    def visit(item):
        if isinstance(item, dict):
            result = {}
            for key, val in item.items():
                result[key] = "[REDACTED]" if SECRET_NAME.search(str(key)) and isinstance(val, (str, int, float)) and val != "" else visit(val)
                if key in {"url", "base_url"} and isinstance(result[key], str) and "?" in result[key]:
                    parsed = urlsplit(result[key])
                    query = [(k, "[REDACTED]" if SECRET_NAME.search(k) or k.lower() in credential_params else v)
                             for k, v in parse_qsl(parsed.query, keep_blank_values=True)]
                    result[key] = urlunsplit(parsed._replace(query=_encode_query(query)))
            # The response's raw JSON text also needs structured token redaction.
            if isinstance(result.get("body"), str):
                try:
                    parsed = json.loads(result["body"])
                    result["body"] = json.dumps(visit(parsed), ensure_ascii=False)
                except (ValueError, TypeError):
                    pass
            return result
        if isinstance(item, list):
            return [visit(val) for val in item]
        if isinstance(item, str):
            for secret in secrets:
                item = item.replace(secret, "[REDACTED]")
        return item
    return visit(value)


def export_workspace(workspace):
    """Portable collections omit locally held credentials by default."""
    exported = copy.deepcopy(workspace)
    secrets = sorted(_json_secret_variants(_known_secrets(workspace)), key=len, reverse=True)
    credential_headers = {"authorization", "cookie", "proxy-authorization", "x-api-key"}
    for item in workspace.get("environments", []) + workspace.get("requests", []):
        if item.get("auth", {}).get("type") == "api_key":
            credential_headers.add(item["auth"].get("header", "X-API-Key").lower())
    def visit(value):
        if isinstance(value, list):
            return [visit(item) for item in value]
        if isinstance(value, dict):
            result = {key: visit(item) for key, item in value.items()}
            if value.get("secret") or SECRET_NAME.search(str(value.get("key", ""))) or str(value.get("key", "")).lower() in credential_headers:
                result["value"] = ""
            if "api_key" in result:
                result["api_key"] = ""
            if isinstance(result.get("auth"), dict):
                for field in ("token", "value", "password"):
                    if field in result["auth"]:
                        result["auth"][field] = ""
            for key in ("url", "base_url"):
                if isinstance(result.get(key), str) and "?" in result[key]:
                    parsed = urlsplit(result[key])
                    query = [(k, "" if SECRET_NAME.search(k) or k.lower() in credential_headers else v) for k, v in parse_qsl(parsed.query, keep_blank_values=True)]
                    result[key] = urlunsplit(parsed._replace(query=_encode_query(query)))
            return result
        if isinstance(value, str):
            for secret in secrets:
                value = value.replace(secret, "")
        return value
    return visit(exported)
