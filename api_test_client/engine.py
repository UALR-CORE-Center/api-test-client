"""HTTP requests, assertions, variable extraction, and request sequences.

The engine deliberately uses only Python's standard library. It does not share
cookies or credentials between requests and never follows an off-origin redirect.
"""

from __future__ import annotations

import base64
import copy
import json
import re
import socket
import time
import uuid
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from http.client import HTTPException
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


MAX_RESPONSE_BYTES = 2 * 1024 * 1024
_VARIABLE = re.compile(r"\{\{\s*([^{}]+?)\s*\}\}")
_RESERVED_VARIABLES = frozenset(("$uuid", "$timestamp", "$timestamp_minus_1h"))


class RequestError(ValueError):
    """An invalid request, variable, or JSON path."""


class _BlockedRedirect(HTTPError):
    pass


def _origin(url: str) -> tuple[str, str, int]:
    parsed = urlsplit(url)
    return parsed.scheme.lower(), (parsed.hostname or "").lower(), parsed.port or (
        443 if parsed.scheme.lower() == "https" else 80
    )


class _RedirectPolicy(HTTPRedirectHandler):
    def __init__(self, follow: bool):
        super().__init__()
        self.follow = follow

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not self.follow:
            return None
        try:
            same_origin = _origin(req.full_url) == _origin(newurl)
        except ValueError:
            same_origin = False
        if not same_origin:
            raise _BlockedRedirect(
                req.full_url, code, "Cross-origin redirect blocked", headers, fp
            )
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _variable_map(values: Any) -> dict[str, Any]:
    """Accept either a key/value row list or a name/value mapping."""
    if values is None:
        return {}
    if isinstance(values, Mapping):
        return {str(key): value for key, value in values.items()}
    result = {}
    for row in values:
        if not isinstance(row, Mapping):
            raise RequestError("Variables must be key/value rows or a mapping.")
        if row.get("enabled", True) and str(row.get("key", "")).strip():
            result[str(row["key"])] = row.get("value", "")
    return result


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (dict, list, bool)):
        return json.dumps(value, separators=(",", ":"), ensure_ascii=False)
    return str(value)


def _interpolate(value: Any, variables: Mapping[str, Any]) -> str:
    def resolve_json(value: Any, trail: tuple[str, ...]) -> Any:
        if isinstance(value, str):
            return expand(value, trail)
        if isinstance(value, dict):
            return {key: resolve_json(item, trail) for key, item in value.items()}
        if isinstance(value, list):
            return [resolve_json(item, trail) for item in value]
        return value

    def expand(text: str, trail: tuple[str, ...] = ()) -> str:
        def replace(match):
            expression = match.group(1).strip()
            modifier = re.fullmatch(r"(.+?)\s*\|\s*json", expression)
            name = modifier.group(1).strip() if modifier else expression
            if name not in variables:
                raise RequestError(f"Variable '{{{{{name}}}}}' is not defined.")
            if name in trail:
                raise RequestError(f"Variable '{name}' contains a circular reference.")
            if len(trail) >= 20:
                raise RequestError("Variable references exceed the maximum nesting depth.")
            next_trail = (*trail, name)
            if modifier:
                try:
                    return json.dumps(resolve_json(variables[name], next_trail),
                                      separators=(",", ":"), ensure_ascii=False, allow_nan=False)
                except (TypeError, ValueError) as exc:
                    if isinstance(exc, RequestError):
                        raise
                    raise RequestError(f"Variable '{name}' cannot be encoded as JSON.") from exc
            return expand(_text(variables[name]), next_trail)

        return _VARIABLE.sub(replace, text)

    return expand(_text(value))


def _run_variables() -> dict[str, str]:
    """Generate stable, credential-free values for one request or sequence run."""
    now = datetime.now(timezone.utc)
    return {
        "$uuid": str(uuid.uuid4()),
        "$timestamp": now.isoformat().replace("+00:00", "Z"),
        "$timestamp_minus_1h": (now - timedelta(hours=1)).isoformat().replace("+00:00", "Z"),
    }


def _expected_value(value: Any, variables: Mapping[str, Any]) -> Any:
    """Interpolate expectation strings while retaining literal JSON types."""
    if isinstance(value, str):
        return _interpolate(value, variables)
    if isinstance(value, dict):
        return {key: _expected_value(item, variables) for key, item in value.items()}
    if isinstance(value, list):
        return [_expected_value(item, variables) for item in value]
    return value


def _json_equal(value: Any, expected: Any) -> bool:
    """Compare JSON values without Python's boolean/number equivalence."""
    if isinstance(value, bool) or isinstance(expected, bool):
        return isinstance(value, bool) and isinstance(expected, bool) and value == expected
    if value is None or expected is None:
        return value is None and expected is None
    if isinstance(value, (int, float)) and isinstance(expected, (int, float)):
        return value == expected
    if isinstance(value, dict) or isinstance(expected, dict):
        return isinstance(value, dict) and isinstance(expected, dict) and value.keys() == expected.keys() and all(
            _json_equal(item, expected[key]) for key, item in value.items()
        )
    if isinstance(value, list) or isinstance(expected, list):
        return isinstance(value, list) and isinstance(expected, list) and len(value) == len(expected) and all(
            _json_equal(item, other) for item, other in zip(value, expected)
        )
    return type(value) is type(expected) and value == expected


def _pairs(rows: Any, variables: Mapping[str, Any]) -> list[tuple[str, str]]:
    if isinstance(rows, Mapping):
        rows = [{"key": key, "value": value} for key, value in rows.items()]
    result = []
    for row in rows or []:
        if not isinstance(row, Mapping):
            raise RequestError("Headers and parameters must be key/value rows.")
        if row.get("enabled", True) and str(row.get("key", "")).strip():
            result.append(
                (_interpolate(row["key"], variables), _interpolate(row.get("value", ""), variables))
            )
    return result


def _json_path(document: Any, path: str) -> Any:
    """Resolve paths such as $.items[0].id without executing expressions."""
    if not isinstance(path, str) or not path.startswith("$"):
        raise RequestError("JSON paths must start with '$', for example '$.items[0].id'.")
    cursor = 1
    value = document
    while cursor < len(path):
        if path[cursor] == ".":
            end = cursor + 1
            while end < len(path) and path[end] not in ".[":
                end += 1
            key = path[cursor + 1:end]
            if not key or any(character in key for character in "]* \t\r\n"):
                raise RequestError(f"Invalid JSON path '{path}'.")
            if not isinstance(value, dict) or key not in value:
                raise RequestError(f"JSON path '{path}' was not found (missing '{key}').")
            value = value[key]
            cursor = end
        elif path[cursor] == "[":
            match = re.match(r"\[(\d+)\]", path[cursor:])
            if not match:
                raise RequestError(f"Invalid JSON path '{path}'; use a nonnegative array index.")
            index = int(match.group(1))
            if not isinstance(value, list) or index >= len(value):
                raise RequestError(f"JSON path '{path}' was not found (missing index {index}).")
            value = value[index]
            cursor += len(match.group(0))
        else:
            raise RequestError(f"Invalid JSON path '{path}'.")
    return value


def _assertions(response: dict, assertions: Any, context: Mapping[str, Any]) -> list[dict]:
    results = []
    for assertion in assertions or []:
        kind = assertion.get("type", "") if isinstance(assertion, Mapping) else ""
        name = (assertion.get("name") or kind or "Unknown assertion") if isinstance(assertion, Mapping) else "Unknown assertion"
        passed = False
        message = ""
        try:
            if not isinstance(assertion, Mapping):
                raise RequestError("Assertions must specify an assertion type.")
            if "expected_variable" in assertion:
                if "expected" in assertion:
                    raise RequestError("Specify either 'expected' or 'expected_variable', not both.")
                variable_name = assertion["expected_variable"]
                if not isinstance(variable_name, str) or not variable_name.strip():
                    raise RequestError("'expected_variable' must be a nonempty variable name.")
                if variable_name not in context:
                    raise RequestError(f"Expected variable '{variable_name}' is not defined.")
                expected = context[variable_name]
            else:
                expected = _expected_value(assertion.get("expected"), context)
            if kind == "status":
                try:
                    expected = int(expected)
                except (ValueError, TypeError, OverflowError) as exc:
                    raise RequestError("HTTP status assertions expect an integer.") from exc
                passed = response["status"] == expected
                message = f"Expected HTTP {expected}; received HTTP {response['status']}."
            elif kind in ("json_equals", "json_exists", "json_greater_than", "json_length_equals"):
                if not response.get("_json_valid", response.get("json") is not None):
                    raise RequestError("Response body is not valid JSON.")
                value = _json_path(response["json"], assertion.get("path", "$"))
                if kind == "json_greater_than":
                    if isinstance(value, bool) or not isinstance(value, (int, float)) or isinstance(expected, bool) or not isinstance(expected, (int, float)):
                        raise RequestError("'json_greater_than' requires numeric JSON and expected values.")
                    passed = value > expected
                    message = "JSON value exceeds the expected value." if passed else "JSON value does not exceed the expected value."
                elif kind == "json_length_equals":
                    if not isinstance(value, (str, list, dict)):
                        raise RequestError("'json_length_equals' requires a JSON string, array, or object.")
                    if isinstance(expected, bool) or not isinstance(expected, int) or expected < 0:
                        raise RequestError("'json_length_equals' expects a nonnegative integer.")
                    passed = len(value) == expected
                    message = "JSON length matches." if passed else "JSON length does not match the expected value."
                else:
                    passed = kind == "json_exists" or _json_equal(value, expected)
                    message = "JSON path exists." if kind == "json_exists" else (
                        "JSON value matches." if passed else "JSON value does not match the expected value."
                    )
            elif kind == "body_contains":
                passed = _text(expected) in response["body"]
                message = "Response contains the expected text." if passed else "Response does not contain the expected text."
            elif kind == "header_equals":
                header = str(assertion.get("path", "")).lower()
                value = next((value for key, value in response["headers"].items() if key.lower() == header), None)
                passed = value == _text(expected)
                message = "Header value matches." if passed else f"Header '{header}' does not match the expected value."
            elif kind == "time_below":
                try:
                    limit = float(expected)
                except (ValueError, TypeError, OverflowError) as exc:
                    raise RequestError("Time assertions expect a number of milliseconds.") from exc
                passed = response["elapsed_ms"] < limit
                message = f"Expected less than {limit:g} ms; received {response['elapsed_ms']:.1f} ms."
            else:
                raise RequestError(f"Unknown assertion type '{kind}'.")
        except (RequestError, TypeError, ValueError, AttributeError) as exc:
            message = str(exc)
        results.append({"passed": bool(passed), "name": str(name), "message": message})
    return results


def _base_response() -> dict:
    return {
        "ok": False,
        "status": None,
        "reason": "",
        "headers": {},
        "body": "",
        "json": None,
        "elapsed_ms": 0.0,
        "size_bytes": 0,
        "truncated": False,
        "error": None,
        "assertions": [],
        "extracted": {},
        "request": {"method": "", "url": ""},
    }


def _read_response(stream, deadline: float) -> tuple[bytes, bool]:
    chunks = []
    size = 0
    while size <= MAX_RESPONSE_BYTES:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Request timed out while reading the response.")
        # urllib's HTTPResponse uses this socket for reads. Lower its timeout so
        # a slow response cannot restart the full request timeout on every chunk.
        raw = getattr(getattr(stream, "fp", None), "raw", None)
        sock = getattr(raw, "_sock", None)
        if sock is not None:
            sock.settimeout(remaining)
        reader = getattr(stream, "read1", stream.read)
        chunk = reader(min(65536, MAX_RESPONSE_BYTES + 1 - size))
        if not chunk:
            break
        chunks.append(chunk)
        size += len(chunk)
    body = b"".join(chunks)
    return body[:MAX_RESPONSE_BYTES], size > MAX_RESPONSE_BYTES


def run_request(request, environment, variables=None, globals=None, *, _dynamic_context=None) -> dict:
    """Execute one request and evaluate its assertions and required extractions.

    Variable precedence is globals, environment variables, environment built-ins
    (base_url/api_key), then runtime variables. Generated run variables are
    reserved and take precedence over all user values. An explicit status
    assertion can make an expected non-2xx response successful; otherwise
    success requires 2xx.
    """
    response = _base_response()
    started = time.monotonic()
    json_valid = False
    context = {}
    dynamic_context = _run_variables() if _dynamic_context is None else _dynamic_context
    try:
        environment = environment or {}
        context = _variable_map(globals)
        context.update(_variable_map(environment.get("variables")))
        context.update({"base_url": environment.get("base_url", ""), "api_key": environment.get("api_key", "")})
        context.update(_variable_map(variables))
        context.update({name: dynamic_context[name] for name in _RESERVED_VARIABLES})
        method = str(request.get("method", "GET")).upper().strip()
        if not re.fullmatch(r"[A-Z][A-Z0-9!#$%&'*+.^_`|~-]*", method):
            raise RequestError("HTTP method is invalid.")
        url = _interpolate(request.get("url", ""), context).strip()
        if not url:
            raise RequestError("Request URL is required.")
        if not urlsplit(url).scheme:
            base = _interpolate(context.get("base_url", ""), context).rstrip("/")
            if not base:
                raise RequestError("A relative URL requires an environment base URL.")
            url = base + "/" + url.lstrip("/")
        parsed = urlsplit(url)
        if parsed.scheme.lower() not in ("http", "https") or not parsed.hostname:
            raise RequestError("Only HTTP and HTTPS URLs with a hostname are allowed.")
        if parsed.username is not None or parsed.password is not None:
            raise RequestError("Credentials embedded in URLs are not supported; use the Authentication settings instead.")
        try:
            parsed.port
        except ValueError as exc:
            raise RequestError("The request URL has an invalid port.") from exc
        query = parse_qsl(parsed.query, keep_blank_values=True)
        query.extend(_pairs(request.get("params"), context))
        headers = {}
        for key, value in _pairs(request.get("headers"), context):
            if not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", key) or "\r" in value or "\n" in value:
                raise RequestError("A request header name or value is invalid.")
            headers[key] = value

        def set_header(key: str, value: str, replace: bool = True):
            found = next((existing for existing in headers if existing.lower() == key.lower()), None)
            if found is not None and not replace:
                return
            if found is not None:
                del headers[found]
            if not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", key) or "\r" in value or "\n" in value:
                raise RequestError("An authentication header name or value is invalid.")
            headers[key] = value

        auth = request.get("auth") or {"type": "inherit"}
        if auth.get("type", "inherit") == "inherit":
            auth = environment.get("auth") or {"type": "none"}
        auth = {key: _interpolate(value, context) for key, value in auth.items()}
        auth_type = auth.get("type", "none")
        if auth_type == "bearer":
            token = auth.get("token", auth.get("value"))
            if token is None:
                token = _interpolate(context.get("api_key", ""), context)
            set_header("Authorization", "Bearer " + token)
        elif auth_type == "api_key":
            value = auth.get("value")
            if value is None:
                value = _interpolate(context.get("api_key", ""), context)
            name = auth.get("header") or "X-API-Key"
            location = auth.get("location", "header")
            if location == "query":
                query = [(key, item) for key, item in query if key != name]
                query.append((name, value))
            elif location == "header":
                set_header(name, value)
            else:
                raise RequestError("API key location must be 'header' or 'query'.")
        elif auth_type == "basic":
            credentials = auth.get("username", "") + ":" + auth.get("password", "")
            set_header("Authorization", "Basic " + base64.b64encode(credentials.encode("utf-8")).decode("ascii"))
        elif auth_type != "none":
            raise RequestError(f"Unknown authentication type '{auth_type}'.")

        url = urlunsplit((parsed.scheme, parsed.netloc, parsed.path or "/", urlencode(query), ""))
        response["request"] = {"method": method, "url": url}
        body_type = request.get("body_type", "none")
        data = None
        if body_type in ("json", "text", "form"):
            body = _interpolate(request.get("body", ""), context)
            if body_type == "json":
                try:
                    json.loads(body)
                except (ValueError, TypeError) as exc:
                    raise RequestError("Request body is not valid JSON.") from exc
                set_header("Content-Type", "application/json", replace=False)
            elif body_type == "form":
                set_header("Content-Type", "application/x-www-form-urlencoded", replace=False)
            else:
                set_header("Content-Type", "text/plain; charset=utf-8", replace=False)
            data = body.encode("utf-8")
        elif body_type != "none":
            raise RequestError(f"Unknown body type '{body_type}'.")
        timeout = float(request.get("timeout", 30))
        if not 1 <= timeout <= 300:
            raise RequestError("Timeout must be between 1 and 300 seconds.")
        network_request = Request(url, data=data, headers=headers, method=method)
        opener = build_opener(_RedirectPolicy(bool(request.get("follow_redirects", False))))
        deadline = time.monotonic() + timeout
        try:
            stream = opener.open(network_request, timeout=timeout)
        except HTTPError as exc:
            stream = exc
            if isinstance(exc, _BlockedRedirect):
                response["error"] = "Cross-origin redirect blocked; credentials were not forwarded."
        with stream:
            response["status"] = stream.code
            response["reason"] = str(stream.reason or "")
            response["headers"] = dict(stream.headers.items())
            raw, response["truncated"] = _read_response(stream, deadline)
            response["size_bytes"] = len(raw)
            charset = stream.headers.get_content_charset() or "utf-8"
            try:
                response["body"] = raw.decode(charset, errors="replace")
            except LookupError:
                response["body"] = raw.decode("utf-8", errors="replace")
            if response["truncated"]:
                response["error"] = "Response exceeded the 2 MiB limit and was truncated."
            else:
                try:
                    response["json"] = json.loads(response["body"])
                    json_valid = True
                except ValueError:
                    pass
    except (TimeoutError, socket.timeout):
        response["error"] = "Request timed out."
    except URLError as exc:
        if isinstance(exc.reason, (TimeoutError, socket.timeout)):
            response["error"] = "Request timed out."
        else:
            response["error"] = f"Connection failed: {exc.reason}"
    except HTTPException as exc:
        response["error"] = f"Invalid HTTP response: {exc}"
    except (RequestError, ValueError, TypeError, AttributeError, OSError) as exc:
        response["error"] = str(exc)
    response["elapsed_ms"] = round((time.monotonic() - started) * 1000, 2)
    response["_json_valid"] = json_valid
    response["assertions"] = _assertions(response, request.get("assertions"), context)
    response.pop("_json_valid", None)
    for extraction in request.get("extract") or []:
        try:
            if not isinstance(extraction, Mapping):
                raise RequestError("Extractions must specify a variable name and JSON path.")
            name = str(extraction.get("name", "")).strip()
            if not name:
                raise RequestError("Extracted variables require a name.")
            if not json_valid:
                raise RequestError("Response body is not valid JSON.")
            response["extracted"][name] = _json_path(response["json"], extraction.get("path", "$"))
        except (RequestError, TypeError, AttributeError) as exc:
            name = extraction.get("name", "") if isinstance(extraction, Mapping) else ""
            response["error"] = response["error"] or f"Extraction '{name}' failed: {exc}"
    has_status_assertion = any(isinstance(item, Mapping) and item.get("type") == "status" for item in request.get("assertions") or [])
    status_ok = has_status_assertion or (response["status"] is not None and 200 <= response["status"] < 300)
    response["ok"] = bool(status_ok and not response["error"] and all(item["passed"] for item in response["assertions"]))
    return response


def run_sequence(sequence, requests, environment, variables=None, globals=None, cancel_event=None, progress_callback=None) -> dict:
    """Run ordered requests, publishing extractions from successful steps only.

    An optional progress observer receives an independent snapshot after each
    completed step. Observer errors cannot interrupt request execution.
    A required step stops the sequence on failure, even in continue mode.
    """
    dynamic_context = _run_variables()
    result = {"ok": True, "steps": [], "variables": {}, "run_variables": dynamic_context.copy(), "error": None, "cancelled": False}
    lookup = requests if isinstance(requests, Mapping) else {request["id"]: request for request in requests}
    runtime = _variable_map(variables)
    for step in sequence.get("steps") or []:
        if cancel_event is not None and cancel_event.is_set():
            result.update(ok=False, cancelled=True, error="Sequence cancelled.")
            break
        request_id = step.get("request_id")
        request = lookup.get(request_id)
        if request is None:
            result.update(ok=False, error=f"Sequence step refers to missing request '{request_id}'.")
            break
        step_variables = {**runtime, **result["variables"], **_variable_map(step.get("variables"))}
        response = run_request(request, environment, step_variables, globals, _dynamic_context=dynamic_context)
        result["steps"].append({"request_id": request_id, "name": request.get("name", request_id), "response": response})
        if response["ok"]:
            result["variables"].update(response["extracted"])
        stop = False
        if not response["ok"]:
            result["ok"] = False
            if sequence.get("stop_on_failure", True) or step.get("required", False):
                result["error"] = f"Sequence stopped because request '{request.get('name', request_id)}' failed."
                stop = True
        if progress_callback is not None:
            try:
                progress_callback(copy.deepcopy(result))
            except Exception:
                # Reporting progress is independent of running the requests.
                pass
        if stop:
            break
    if cancel_event is not None and cancel_event.is_set():
        result.update(ok=False, cancelled=True, error="Sequence cancelled.")
    return result
