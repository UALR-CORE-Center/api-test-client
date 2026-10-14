"""End-to-end local app API and terminal runner checks, without external traffic."""

from contextlib import redirect_stderr, redirect_stdout
from http.client import HTTPConnection
import io
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from api_test_client.__main__ import main
from api_test_client.server import ClientServer
from api_test_client.workspace import new_workspace


class ServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.path = Path(cls.temp.name) / "workspace.json"
        cls.server = ClientServer(("127.0.0.1", 0), cls.path)
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"
        cls.worker = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.worker.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.worker.join(timeout=2)
        cls.temp.cleanup()

    def setUp(self):
        self.workspace = new_workspace(self.base)
        self.server.store.save(self.workspace)
        status, session, _ = self.http("GET", "/api/session", token=False)
        self.assertEqual(status, 200)
        self.token = session["token"]

    def http(self, method, path, payload=None, headers=None, token=True):
        request_headers = {}
        if token:
            request_headers["X-Client-Token"] = self.token
        body = None
        if payload is not None:
            body = json.dumps(payload).encode()
            request_headers["Content-Type"] = "application/json"
        request_headers.update(headers or {})
        connection = HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        try:
            connection.request(method, path, body, request_headers)
            response = connection.getresponse()
            data = response.read()
            value = json.loads(data) if data and "application/json" in response.getheader("Content-Type", "") else data
            return response.status, value, dict(response.getheaders())
        finally:
            connection.close()

    def test_loopback_only_bind(self):
        with self.assertRaises(ValueError):
            ClientServer(("0.0.0.0", 0), self.path)

    def test_session_token_required_for_workspace(self):
        self.assertTrue(self.token)
        for headers in ({}, {"X-Client-Token": "wrong-token"}):
            with self.subTest(headers=headers):
                status, value, _ = self.http("GET", "/api/workspace", headers=headers, token=False)
                self.assertEqual(status, 403)
                self.assertNotIn("environments", value)
        status, value, _ = self.http("GET", "/api/workspace")
        self.assertEqual(status, 200)
        self.assertEqual(value["active_environment_id"], "local-demo")

    def test_browser_origin_checks_apply_even_to_session_endpoint(self):
        blocked = [
            {"Host": "attacker.example"},
            {"Origin": "https://attacker.example"},
            {"Origin": "null"},
            {"Sec-Fetch-Site": "cross-site"},
            {"Host": f"127.0.0.1:{self.server.server_port}", "Origin": f"http://localhost:{self.server.server_port}"},
        ]
        for headers in blocked:
            with self.subTest(headers=headers):
                status, value, _ = self.http("GET", "/api/session", headers=headers, token=False)
                self.assertEqual(status, 403)
                self.assertNotIn("token", value)
        status, _, _ = self.http("GET", "/api/workspace", headers={"Origin": self.base, "Sec-Fetch-Site": "same-origin"})
        self.assertEqual(status, 200)

    def test_cross_origin_mutation_cannot_change_saved_workspace(self):
        before = self.path.read_bytes()
        self.workspace["environments"][0]["name"] = "Tampered"
        status, _, _ = self.http("PUT", "/api/workspace", self.workspace,
                                 headers={"Origin": "https://attacker.example"})
        self.assertEqual(status, 403)
        self.assertEqual(self.path.read_bytes(), before)

    def test_invalid_import_is_rejected_and_previous_workspace_remains(self):
        before = self.path.read_bytes()
        status, value, _ = self.http("POST", "/api/import", {"workspace": {"version": 99}})
        self.assertEqual(status, 400)
        self.assertIn("error", value)
        self.assertEqual(self.path.read_bytes(), before)

    def test_json_payload_is_required(self):
        status, value, _ = self.http("PUT", "/api/workspace", self.workspace,
                                     headers={"Content-Type": "text/plain"})
        self.assertEqual(status, 400)
        self.assertIn("application/json", value["error"])

    def test_workspace_roundtrip_and_export_remove_credentials(self):
        self.workspace["environments"][0]["api_key"] = "private-key-to-export"
        status, saved, _ = self.http("PUT", "/api/workspace", self.workspace)
        self.assertEqual(status, 200)
        self.assertEqual(saved["environments"][0]["api_key"], "private-key-to-export")
        status, exported, headers = self.http("GET", "/api/export")
        self.assertEqual(status, 200)
        self.assertNotIn("private-key-to-export", json.dumps(exported))
        self.assertIn("attachment", headers["Content-Disposition"])
        self.assertEqual(self.server.store.load()["environments"][0]["api_key"], "private-key-to-export")

    def test_static_shell_and_security_headers(self):
        status, html, headers = self.http("GET", "/", token=False)
        self.assertEqual(status, 200)
        self.assertIn(b"API Test Client", html)
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
        self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
        status, data, _ = self.http("HEAD", "/", token=False)
        self.assertEqual(status, 200)
        self.assertEqual(data, b"")

    def test_demo_request_with_runtime_variable(self):
        status, result, _ = self.http("POST", "/api/run", {
            "environment_id": "local-demo", "request": self.workspace["requests"][1],
            "variables": {"message": "runtime payload"},
        })
        self.assertEqual(status, 200)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["status"], 200)
        self.assertEqual(result["json"]["received"], {"message": "runtime payload"})
        self.assertIsInstance(result["assertions"], list)

    def run_sequence(self, sequence=None):
        status, value, _ = self.http("POST", "/api/sequences/run", {
            "sequence": sequence or self.workspace["sequences"][0], "environment_id": "local-demo",
        })
        self.assertEqual(status, 202, value)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            status, job, _ = self.http("GET", "/api/runs/" + value["run_id"])
            self.assertEqual(status, 200)
            if job["state"] == "completed":
                return value["run_id"], job["result"]
            time.sleep(0.01)
        self.fail("Local sequence did not complete within five seconds")

    def test_sequence_polling_chains_response_variable(self):
        _, result = self.run_sequence()
        self.assertTrue(result["ok"], result)
        self.assertFalse(result["cancelled"])
        self.assertEqual(result["variables"], {"demo_id": "demo-123"})
        self.assertEqual(len(result["steps"]), 2)
        self.assertEqual(result["steps"][1]["response"]["json"]["id"], "demo-123")
        for step in result["steps"]:
            self.assertIn("response", step)
            self.assertIsInstance(step["response"]["ok"], bool)

    def test_unknown_runs_and_malformed_runs_return_errors(self):
        status, _, _ = self.http("GET", "/api/runs/does-not-exist")
        self.assertEqual(status, 404)
        for payload in ({"sequence": {"steps": []}}, {"sequence": self.workspace["sequences"][0], "variables": []}):
            with self.subTest(payload=payload):
                status, _, _ = self.http("POST", "/api/sequences/run", payload)
                self.assertEqual(status, 400)

    def test_cancelling_completed_run_is_harmless(self):
        run_id, result = self.run_sequence()
        status, value, _ = self.http("POST", "/api/runs/" + run_id + "/cancel", {})
        self.assertEqual(status, 200)
        self.assertTrue(value["cancelled"])
        status, job, _ = self.http("GET", "/api/runs/" + run_id)
        self.assertEqual(status, 200)
        self.assertEqual(job["result"], result)

    def test_terminal_runner_has_same_result_schema_and_saves_report(self):
        _, browser_result = self.run_sequence()
        output = io.StringIO()
        report_path = Path(self.temp.name) / "report.json"
        with redirect_stdout(output):
            code = main(["run", "--workspace", str(self.path), "--sequence", "demo-sequence",
                         "--environment", "Local demo", "--output", str(report_path)])
        self.assertEqual(code, 0)
        report = json.loads(output.getvalue())
        self.assertEqual(set(report), set(browser_result))
        self.assertEqual(report["variables"], browser_result["variables"])
        self.assertEqual(json.loads(report_path.read_text(encoding="utf-8")), report)

    def test_terminal_report_redacts_runtime_credentials(self):
        self.workspace["requests"][0] = {
            **self.workspace["requests"][1], "id": "echo-key",
            "body": '{"credential":"{{access_token}}"}',
        }
        self.workspace["sequences"][0]["steps"] = [{"request_id": "echo-key"}]
        self.server.store.save(self.workspace)
        output = io.StringIO()
        with redirect_stdout(output):
            code = main(["run", "--workspace", str(self.path), "--sequence", "demo-sequence",
                         "--var", "access_token=runtime-private-value"])
        self.assertEqual(code, 0)
        self.assertNotIn("runtime-private-value", output.getvalue())
        self.assertIn("[REDACTED]", output.getvalue())

    def test_terminal_runner_cannot_overwrite_workspace_with_report(self):
        before = self.path.read_bytes()
        errors = io.StringIO()
        with patch("api_test_client.__main__.run_sequence") as execute, redirect_stderr(errors), redirect_stdout(io.StringIO()):
            code = main(["run", "--workspace", str(self.path), "--sequence", "demo-sequence",
                         "--output", str(self.path)])
            execute.assert_not_called()
        self.assertEqual(code, 2)
        self.assertIn("must not overwrite", errors.getvalue())
        self.assertEqual(self.path.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
