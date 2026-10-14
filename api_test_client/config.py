"""Load portable startup tests and private overrides without changing the sources."""

from __future__ import annotations

import copy
import hashlib
import json
import math
from pathlib import Path

from .workspace import MAX_WORKSPACE_BYTES, atomic_write, default_path, validate_workspace


def _path(value) -> Path:
    return Path(value).expanduser().resolve()


def _read_text(path: Path, label: str, *, json_file=False) -> str:
    try:
        with path.open("rb") as stream:
            raw = stream.read(MAX_WORKSPACE_BYTES + 1)
    except OSError as exc:
        raise ValueError(f"Cannot read {label}: {path}") from exc
    if len(raw) > MAX_WORKSPACE_BYTES:
        raise ValueError(f"{label} exceeds the 4 MiB limit: {path}")
    try:
        return raw.decode("utf-8-sig" if json_file else "utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"{label} must use UTF-8: {path}") from exc


def _read_object(path: Path, label: str) -> dict:
    def reject_constant(_):
        raise ValueError("Non-finite JSON numbers are not supported.")
    def finite_float(raw):
        number = float(raw)
        if not math.isfinite(number):
            reject_constant(raw)
        return number
    try:
        value = json.loads(_read_text(path, label, json_file=True), parse_constant=reject_constant, parse_float=finite_float)
    except json.JSONDecodeError as exc:
        # Do not echo source snippets: a syntax error may be next to credentials.
        raise ValueError(f"Invalid JSON in {label} at line {exc.lineno}, column {exc.colno}: {path}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a JSON object.")
    return value


def _variable_value(value) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("Variable values must contain finite JSON numbers.")
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def _variables(value, *, private=False) -> list[dict]:
    """Accept editor-style rows or the convenient {name: value} shorthand."""
    if isinstance(value, dict):
        value = [{"key": key, "value": item} for key, item in value.items()]
    if not isinstance(value, list) or len(value) > 1000:
        raise ValueError("Variables must be an object or a list with at most 1,000 entries.")
    rows = copy.deepcopy(value)
    keys = set()
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("key"), str):
            raise ValueError("Variable entries need string key fields.")
        if row["key"] in keys:
            raise ValueError("Variable keys must be unique within each scope.")
        keys.add(row["key"])
        row["value"] = _variable_value(row.get("value", ""))
        row.setdefault("enabled", True)
        row.setdefault("secret", private)
        if not isinstance(row["enabled"], bool) or not isinstance(row["secret"], bool):
            raise ValueError("Variable enabled/secret fields must be booleans.")
    return rows


def _environments(value, *, private=False) -> list[dict]:
    if not isinstance(value, list) or len(value) > 1000:
        raise ValueError("Environments must be a list with at most 1,000 entries.")
    environments = copy.deepcopy(value)
    ids = set()
    for environment in environments:
        if (not isinstance(environment, dict) or not isinstance(environment.get("id"), str)
                or not environment["id"] or len(environment["id"]) > 200):
            raise ValueError("Each environment needs a nonempty string id of at most 200 characters.")
        if environment["id"] in ids:
            raise ValueError("Environment ids must be unique.")
        ids.add(environment["id"])
        if "variables" in environment:
            environment["variables"] = _variables(environment["variables"], private=private)
    return environments


def _merge_variables(public: list[dict], private: list[dict]) -> list[dict]:
    merged = {row["key"]: row for row in public}
    for row in private:
        merged[row["key"]] = {**merged.get(row["key"], {}), **row}
    return list(merged.values())


def load_config_sources(config_path, private_path=None) -> tuple[dict, tuple[Path, ...]]:
    """Return a validated effective workspace and every source file it reads.

    Private overlays can change environment settings and variables only. Request
    definitions and sequences always come from the reusable public configuration.
    Values are left as templates; interpolation happens only when a request runs.
    """
    source = _path(config_path)
    workspace = _read_object(source, "Startup configuration")
    if isinstance(workspace.get("version"), bool) or workspace.get("version") != 1:
        raise ValueError("Startup configuration must have version: 1.")
    sources = [source]
    workspace["variables"] = _variables(workspace.get("variables", []))
    workspace["environments"] = _environments(workspace.get("environments", []))
    requests = workspace.get("requests", [])
    if not isinstance(requests, list):
        raise ValueError("Requests must be a list.")
    for request in requests:
        if not isinstance(request, dict):
            raise ValueError("Requests must contain JSON objects.")
        if "body_file" in request:
            if "body" in request:
                raise ValueError("A request cannot specify both body and body_file.")
            reference = request.pop("body_file")
            if not isinstance(reference, str) or not reference.strip():
                raise ValueError("Request body_file must be a nonempty path string.")
            body_source = _path(source.parent / Path(reference).expanduser())
            request["body"] = _read_text(body_source, "Request body file")
            request.setdefault("body_type", "json")
            sources.append(body_source)
    if private_path is not None:
        private_source = _path(private_path)
        private = _read_object(private_source, "Private configuration")
        allowed = {"version", "environments", "variables", "active_environment_id"}
        if set(private) - allowed:
            raise ValueError("Private configuration may override only environments, variables, and active_environment_id (optional version: 1).")
        if "version" in private and (isinstance(private["version"], bool) or private["version"] != 1):
            raise ValueError("Private configuration version must be 1 when supplied.")
        if "variables" in private:
            workspace["variables"] = _merge_variables(workspace["variables"], _variables(private["variables"], private=True))
        environments = {environment["id"]: environment for environment in workspace["environments"]}
        for environment in _environments(private.get("environments", []), private=True):
            previous = environments.get(environment["id"], {})
            merged = {**previous, **environment}
            if "variables" in environment:
                merged["variables"] = _merge_variables(previous.get("variables", []), environment["variables"])
            if isinstance(previous.get("auth"), dict) and isinstance(environment.get("auth"), dict):
                merged["auth"] = {**previous["auth"], **environment["auth"]}
            environments[environment["id"]] = merged
        workspace["environments"] = list(environments.values())
        if "active_environment_id" in private:
            workspace["active_environment_id"] = private["active_environment_id"]
        sources.append(private_source)
    active = workspace.get("active_environment_id")
    if active is not None and (not isinstance(active, str) or active not in {env["id"] for env in workspace["environments"]}):
        raise ValueError("active_environment_id must identify a configured environment.")
    try:
        validated = validate_workspace(workspace)
    except (TypeError, OverflowError) as exc:
        raise ValueError("Startup configuration contains a field with an unsupported JSON type.") from exc
    return validated, tuple(dict.fromkeys(sources))


def load_config(config_path, private_path=None) -> dict:
    """Load startup tests with an optional separate private settings file."""
    return load_config_sources(config_path, private_path)[0]


def paths_collide(left, right) -> bool:
    """Protect source files through symlinks and existing hard links, too."""
    left, right = _path(left), _path(right)
    if left == right:
        return True
    try:
        return left.samefile(right)
    except OSError:
        return False


def prepare_workspace(config_path=None, private_path=None, workspace_path=None) -> tuple[Path, tuple[Path, ...]]:
    """Initialize a writable import copy only after all source validation passes."""
    if private_path is not None and config_path is None:
        raise ValueError("--private-config requires --config.")
    if config_path is None:
        return Path(workspace_path).expanduser() if workspace_path is not None else default_path(), ()
    workspace, sources = load_config_sources(config_path, private_path)
    if workspace_path is None:
        digest = hashlib.sha256(str(_path(config_path)).encode("utf-8")).hexdigest()[:16]
        destination = Path.home() / ".api-test-client" / "imports" / f"{digest}.workspace.json"
    else:
        destination = Path(workspace_path).expanduser()
    if any(paths_collide(destination, source) for source in sources):
        raise ValueError("Writable --workspace must not overwrite a configuration or body source file.")
    rendered = json.dumps(workspace, indent=2, ensure_ascii=False, allow_nan=False) + "\n"
    if len(rendered.encode("utf-8")) > MAX_WORKSPACE_BYTES:
        raise ValueError("Formatted effective workspace exceeds the 4 MiB limit.")
    atomic_write(destination, rendered)
    return destination, sources
