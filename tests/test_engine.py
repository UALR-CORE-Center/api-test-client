"""Exercise the request engine against real, local HTTP servers."""

import base64
import json
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from api_test_client.engine import MAX_RESPONSE_BYTES, run_request, run_sequence


class _Handler(BaseHTTPRequestHandler):
    foreign_url = ""
    visited = []

    def log_message(self, *_):
        pass

    def do_GET(self):
        self.handle_request()

    def do_POST(self):
        self.handle_request()

    def send_body(self, status, value, content_type="application/json"):
        data = json.dumps(value).encode() if content_type == "application/json" else value
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def handle_request(self):
        self.visited.append(self.path)
        path = urlsplit(self.path).path
        if path in ("/echo", "/api/echo", "/items/42"):
            body = self.rfile.read(int(self.headers.get("Content-Length", 0))).decode()
            self.send_body(200, {
                "path": path,
                "query": parse_qs(urlsplit(self.path).query, keep_blank_values=True),
                "headers": dict(self.headers.items()),
                "body": body,
                "method": self.command,
            })
        elif path == "/created":
            self.send_body(201, {"id": 42, "items": [{"name": "first"}], "optional": None})
        elif path == "/null":
            self.send_body(200, None)
        elif path == "/missing":
            self.send_body(404, {"error": "missing"})
        elif path == "/plain":
            self.send_body(200, b"Hello world", "text/plain; charset=utf-8")
        elif path == "/redirect":
            self.send_response(302)
            self.send_header("Location", "/created")
            self.send_header("Content-Length", "0")
            self.end_headers()
        elif path == "/cross":
            self.send_response(302)
            self.send_header("Location", self.foreign_url + "/echo")
            self.send_header("Content-Length", "0")
            self.end_headers()
        elif path == "/large":
            self.send_body(200, b"a" * (MAX_RESPONSE_BYTES + 2048), "text/plain")
        elif path == "/slow":
            time.sleep(1.2)
            self.send_body(200, {"done": True})
        elif path == "/drip":
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", "6")
            self.end_headers()
            for _ in range(3):
                try:
                    self.wfile.write(b"aa")
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    break
                time.sleep(0.7)
        else:
            self.send_body(500, {"error": "Unknown test route"})


class EngineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.foreign = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        cls.threads = []
        for server in (cls.foreign, cls.server):
            worker = threading.Thread(target=server.serve_forever, daemon=True)
            worker.start()
            cls.threads.append(worker)
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"
        _Handler.foreign_url = f"http://127.0.0.1:{cls.foreign.server_port}"

    @classmethod
    def tearDownClass(cls):
        for server in (cls.foreign, cls.server):
            server.shutdown()
            server.server_close()
        for worker in cls.threads:
            worker.join(timeout=2)

    def setUp(self):
        self.environment = {"base_url": self.base, "api_key": "test-key", "auth": {"type": "none"}}
        _Handler.visited.clear()

    def request(self, **overrides):
        return {"id": "test", "name": "Test request", "method": "GET", "url": "/echo", **overrides}

    def run_http(self, **overrides):
        return run_request(self.request(**overrides), self.environment)

    def test_interpolation_and_precedence_across_request_fields(self):
        self.environment["variables"] = [
            {"key": "name", "value": "environment"},
            {"key": "message", "value": "Hello {{name}}"},
        ]
        response = run_request(self.request(
            method="POST", url="{{base_url}}/echo?original=1",
            params=[{"key": "name", "value": "{{name}}"}, {"key": "skip", "value": "x", "enabled": False}],
            headers=[{"key": "X-Name", "value": "{{name}}"}],
            body_type="json", body='{"message":"{{message}}"}',
        ), self.environment, {"name": "runtime"}, [{"key": "name", "value": "global"}])
        self.assertTrue(response["ok"], response)
        echoed = response["json"]
        self.assertEqual(echoed["query"], {"original": ["1"], "name": ["runtime"]})
        self.assertEqual(echoed["headers"]["X-Name"], "runtime")
        self.assertEqual(json.loads(echoed["body"]), {"message": "Hello runtime"})
        self.assertEqual(echoed["headers"]["Content-Type"], "application/json")

    def test_environment_variables_override_globals(self):
        self.environment["variables"] = [{"key": "x", "value": "environment"}]
        result = run_request(self.request(params=[{"key": "x", "value": "{{x}}"}]), self.environment, globals={"x": "global"})
        self.assertEqual(result["json"]["query"]["x"], ["environment"])

    def test_base_url_with_path_prefix(self):
        self.environment["base_url"] += "/api"
        self.assertEqual(self.run_http()["json"]["path"], "/api/echo")

    def test_missing_variable_fails_before_network(self):
        result = self.run_http(headers=[{"key": "X-Value", "value": "{{missing}}"}])
        self.assertFalse(result["ok"])
        self.assertIn("missing", result["error"])
        self.assertEqual(_Handler.visited, [])

    def test_variable_cycle_is_reported(self):
        self.environment["variables"] = {"a": "{{b}}", "b": "{{a}}"}
        result = self.run_http(url="/{{a}}")
        self.assertIn("circular", result["error"])

    def test_bearer_inherited_auth(self):
        self.environment["auth"] = {"type": "bearer", "token": "{{api_key}}"}
        result = self.run_http()
        self.assertEqual(result["json"]["headers"]["Authorization"], "Bearer test-key")

    def test_request_auth_none_overrides_environment(self):
        self.environment["auth"] = {"type": "bearer"}
        result = self.run_http(auth={"type": "none"})
        self.assertNotIn("Authorization", result["json"]["headers"])

    def test_explicit_token_does_not_resolve_unused_environment_key(self):
        self.environment["api_key"] = "{{undefined}}"
        result = self.run_http(auth={"type": "bearer", "token": "explicit"})
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["json"]["headers"]["Authorization"], "Bearer explicit")

    def test_api_key_header_and_query_auth(self):
        result = self.run_http(auth={"type": "api_key", "value": "{{api_key}}", "header": "X-Key"})
        self.assertEqual(result["json"]["headers"]["X-Key"], "test-key")
        result = self.run_http(url="/echo?key=old", auth={"type": "api_key", "location": "query", "header": "key"})
        self.assertEqual(result["json"]["query"]["key"], ["test-key"])

    def test_basic_auth(self):
        result = self.run_http(auth={"type": "basic", "username": "user", "password": "{{api_key}}"})
        self.assertEqual(result["json"]["headers"]["Authorization"], "Basic " + base64.b64encode(b"user:test-key").decode())

    def test_json_body_validation_occurs_before_network(self):
        result = self.run_http(method="POST", body_type="json", body="{bad}")
        self.assertFalse(result["ok"])
        self.assertIn("valid JSON", result["error"])
        self.assertEqual(_Handler.visited, [])

    def test_text_and_form_content_types(self):
        for body_type, content_type in (("text", "text/plain; charset=utf-8"), ("form", "application/x-www-form-urlencoded")):
            with self.subTest(body_type=body_type):
                result = self.run_http(method="POST", body_type=body_type, body="key={{api_key}}")
                self.assertEqual(result["json"]["body"], "key=test-key")
                self.assertEqual(result["json"]["headers"]["Content-Type"], content_type)

    def test_http_error_response_preserved_and_default_failed(self):
        result = self.run_http(url="/missing")
        self.assertEqual(result["status"], 404)
        self.assertEqual(result["json"], {"error": "missing"})
        self.assertFalse(result["ok"])
        self.assertIsNone(result["error"])

    def test_expected_404_can_pass(self):
        result = self.run_http(url="/missing", assertions=[{"type": "status", "expected": 404}, {"type": "json_equals", "path": "$.error", "expected": "missing"}])
        self.assertTrue(result["ok"], result)

    def test_all_assertion_types_and_extraction(self):
        result = self.run_http(url="/created", assertions=[
            {"type": "status", "expected": 201},
            {"type": "json_equals", "path": "$.items[0].name", "expected": "first"},
            {"type": "json_exists", "path": "$.optional"},
            {"type": "body_contains", "expected": "first"},
            {"type": "header_equals", "path": "content-type", "expected": "application/json"},
            {"type": "time_below", "expected": 5000},
        ], extract=[{"name": "asset_id", "path": "$.id"}])
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["extracted"], {"asset_id": 42})
        self.assertEqual(len(result["assertions"]), 6)

    def test_json_null_root_is_valid_for_multiple_assertions(self):
        result = self.run_http(url="/null", assertions=[{"type": "json_exists", "path": "$"}, {"type": "json_equals", "path": "$", "expected": None}], extract=[{"name": "null_value", "path": "$"}])
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["extracted"], {"null_value": None})

    def test_missing_json_path_fails_assertion(self):
        result = self.run_http(url="/created", assertions=[{"type": "json_exists", "path": "$.items[3].id"}])
        self.assertFalse(result["ok"])
        self.assertIn("missing index 3", result["assertions"][0]["message"])

    def test_missing_extraction_fails_response(self):
        result = self.run_http(url="/created", extract=[{"name": "asset_id", "path": "$.missing"}])
        self.assertFalse(result["ok"])
        self.assertIn("Extraction 'asset_id' failed", result["error"])

    def test_unsupported_json_path_does_not_evaluate_code(self):
        result = self.run_http(url="/created", assertions=[{"type": "json_exists", "path": "$.items[*].name"}])
        self.assertFalse(result["ok"])
        self.assertIn("Invalid JSON path", result["assertions"][0]["message"])

    def test_non_json_body_can_use_text_assertions(self):
        result = self.run_http(url="/plain", assertions=[{"type": "body_contains", "expected": "Hello"}])
        self.assertTrue(result["ok"])
        self.assertIsNone(result["json"])
        result = self.run_http(url="/plain", assertions=[{"type": "json_exists", "path": "$.id"}])
        self.assertFalse(result["ok"])
        self.assertIn("not valid JSON", result["assertions"][0]["message"])

    def test_redirects_disabled_by_default(self):
        result = self.run_http(url="/redirect")
        self.assertEqual(result["status"], 302)
        self.assertFalse(result["ok"])
        self.assertEqual(_Handler.visited, ["/redirect"])

    def test_same_origin_redirect_allowed_when_enabled(self):
        result = self.run_http(url="/redirect", follow_redirects=True)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["status"], 201)
        self.assertEqual(_Handler.visited, ["/redirect", "/created"])

    def test_cross_origin_redirect_never_forwards_credentials(self):
        result = self.run_http(url="/cross", follow_redirects=True, auth={"type": "bearer"})
        self.assertFalse(result["ok"])
        self.assertIn("Cross-origin redirect blocked", result["error"])
        self.assertEqual(result["status"], 302)
        self.assertEqual(_Handler.visited, ["/cross"])

    def test_response_read_is_bounded(self):
        result = self.run_http(url="/large")
        self.assertTrue(result["truncated"])
        self.assertEqual(result["size_bytes"], MAX_RESPONSE_BYTES)
        self.assertEqual(len(result["body"]), MAX_RESPONSE_BYTES)
        self.assertFalse(result["ok"])

    def test_timeout(self):
        result = self.run_http(url="/slow", timeout=1)
        self.assertFalse(result["ok"])
        self.assertIn("timed out", result["error"])
        self.assertLess(result["elapsed_ms"], 1800)

    def test_slow_stream_cannot_restart_entire_timeout_per_chunk(self):
        result = self.run_http(url="/drip", timeout=1)
        self.assertFalse(result["ok"])
        self.assertIn("timed out", result["error"])
        self.assertLess(result["elapsed_ms"], 1700)

    def test_bad_timeout_and_non_http_url_fail_before_network(self):
        for value in (0, 301, "bad"):
            with self.subTest(timeout=value):
                self.assertFalse(self.run_http(timeout=value)["ok"])
        result = self.run_http(url="file:///tmp/test")
        self.assertIn("Only HTTP and HTTPS", result["error"])
        self.assertEqual(_Handler.visited, [])

    def test_header_newline_rejected(self):
        result = self.run_http(headers=[{"key": "X-Test", "value": "safe\r\nAuthorization: bad"}])
        self.assertFalse(result["ok"])
        self.assertIn("header", result["error"])
        self.assertEqual(_Handler.visited, [])

    def test_embedded_url_credentials_are_rejected_without_exposure(self):
        url = self.base.replace("http://", "http://user:secret@") + "/echo"
        result = self.run_http(url=url)
        self.assertFalse(result["ok"])
        self.assertIn("Authentication settings", result["error"])
        self.assertNotIn("secret", result["error"])
        self.assertNotIn("secret", result["request"]["url"])
        self.assertEqual(_Handler.visited, [])

    def test_sequence_chains_extracted_variables(self):
        requests = [
            self.request(id="create", url="/created", extract=[{"name": "asset_id", "path": "$.id"}]),
            self.request(id="read", url="/items/{{asset_id}}", params=[{"key": "name", "value": "{{name}}"}]),
        ]
        sequence = {"steps": [{"request_id": "create"}, {"request_id": "read", "variables": {"name": "step"}}]}
        result = run_sequence(sequence, requests, self.environment, {"name": "runtime", "private": "do not return"})
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["variables"], {"asset_id": 42})
        self.assertEqual(result["steps"][1]["response"]["json"]["query"], {"name": ["step"]})

    def test_sequence_stop_on_failure_and_continue_mode(self):
        requests = [self.request(id="bad", url="/missing"), self.request(id="good")]
        sequence = {"steps": [{"request_id": "bad"}, {"request_id": "good"}]}
        result = run_sequence(sequence, requests, self.environment)
        self.assertFalse(result["ok"])
        self.assertEqual(len(result["steps"]), 1)
        sequence["stop_on_failure"] = False
        result = run_sequence(sequence, requests, self.environment)
        self.assertFalse(result["ok"])
        self.assertEqual(len(result["steps"]), 2)
        self.assertTrue(result["steps"][1]["response"]["ok"])

    def test_sequence_missing_request_and_cancel(self):
        result = run_sequence({"steps": [{"request_id": "unknown"}]}, [], self.environment)
        self.assertFalse(result["ok"])
        self.assertIn("missing request", result["error"])
        event = threading.Event()
        event.set()
        result = run_sequence({"steps": [{"request_id": "test"}]}, [self.request()], self.environment, cancel_event=event)
        self.assertTrue(result["cancelled"])
        self.assertEqual(result["steps"], [])

    def test_sequence_required_extraction_failure_stops_execution(self):
        requests = [self.request(id="bad", url="/created", extract=[{"name": "x", "path": "$.missing"}]), self.request(id="good")]
        result = run_sequence({"steps": [{"request_id": "bad"}, {"request_id": "good"}]}, requests, self.environment)
        self.assertFalse(result["ok"])
        self.assertEqual(len(result["steps"]), 1)

    def test_sequence_progress_snapshots_are_independent(self):
        requests = [
            self.request(id="create", url="/created", extract=[{"name": "id", "path": "$.id"}]),
            self.request(id="read", url="/items/{{id}}"),
        ]
        snapshots = []
        result = run_sequence({"steps": [{"request_id": "create"}, {"request_id": "read"}]}, requests, self.environment, progress_callback=snapshots.append)
        self.assertTrue(result["ok"])
        self.assertEqual([len(snapshot["steps"]) for snapshot in snapshots], [1, 2])
        self.assertEqual(snapshots[0]["variables"], {"id": 42})
        snapshots[0]["variables"]["id"] = 99
        snapshots[1]["steps"][0]["response"]["json"]["id"] = 100
        self.assertEqual(result["variables"], {"id": 42})
        self.assertEqual(result["steps"][0]["response"]["json"]["id"], 42)

    def test_sequence_cancel_after_progress_retains_completed_step(self):
        event = threading.Event()
        snapshots = []

        def cancel_after_first(snapshot):
            snapshots.append(snapshot)
            event.set()

        result = run_sequence({"steps": [{"request_id": "test"}, {"request_id": "test"}]}, [self.request()], self.environment, cancel_event=event, progress_callback=cancel_after_first)
        self.assertTrue(result["cancelled"])
        self.assertEqual(len(result["steps"]), 1)
        self.assertEqual(len(snapshots), 1)
        self.assertEqual(len(snapshots[0]["steps"]), 1)

    def test_sequence_progress_observer_failure_is_isolated(self):
        def broken_observer(_):
            raise RuntimeError("Display failed")

        result = run_sequence({"steps": [{"request_id": "test"}]}, [self.request()], self.environment, progress_callback=broken_observer)
        self.assertTrue(result["ok"])
        self.assertIsNone(result["error"])


if __name__ == "__main__":
    unittest.main()
