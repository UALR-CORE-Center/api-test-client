"""CLI contract checks without executing any network requests."""

from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from api_test_client.__main__ import main
from api_test_client.workspace import WorkspaceStore, new_workspace


class CommandLineTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.workspace_path = Path(self.temporary.name) / "workspace.json"
        self.workspace = new_workspace()
        self.workspace["environments"][0]["api_key"] = "stored-secret"
        WorkspaceStore(self.workspace_path).save(self.workspace)
        self.arguments = ["run", "--workspace", str(self.workspace_path),
                          "--sequence", "demo-sequence"]

    def invoke(self, arguments, result=None):
        output = io.StringIO()
        errors = io.StringIO()
        report = {"ok": True, "steps": [], "variables": {}} if result is None else result
        with patch("api_test_client.__main__.run_sequence", return_value=report) as run:
            with redirect_stdout(output), redirect_stderr(errors):
                code = main(arguments)
        return code, output.getvalue(), errors.getvalue(), run

    def test_unique_names_and_variables_are_forwarded(self):
        arguments = ["run", "--workspace", str(self.workspace_path),
                     "--sequence", "Health → fetch item", "--environment", "Local demo",
                     "--var", "asset_id=abc=123", "--var", "message=hello"]
        code, output, errors, run = self.invoke(arguments)
        self.assertEqual(code, 0)
        self.assertTrue(json.loads(output)["ok"])
        self.assertEqual(errors, "")
        positional, keywords = run.call_args
        self.assertEqual(positional[0]["id"], "demo-sequence")
        self.assertEqual(positional[2]["id"], "local-demo")
        self.assertEqual(keywords["variables"], {"asset_id": "abc=123", "message": "hello"})

    def test_failed_run_returns_one(self):
        code, output, errors, _ = self.invoke(self.arguments, {"ok": False, "steps": [], "variables": {}})
        self.assertEqual(code, 1)
        self.assertFalse(json.loads(output)["ok"])
        self.assertEqual(errors, "")

    def test_reports_redact_credentials_by_default_in_stdout_and_file(self):
        report = {"ok": True, "variables": {"access_token": "runtime-secret"},
                  "steps": [{"response": {"body": json.dumps({"token": "returned-secret",
                                                            "message": "stored-secret"})}}]}
        report_path = Path(self.temporary.name) / "reports" / "result.json"
        code, output, errors, _ = self.invoke(
            self.arguments + ["--var", "access_token=runtime-secret", "--output", str(report_path)], report)
        self.assertEqual(code, 0)
        self.assertEqual(errors, "")
        self.assertEqual(report_path.read_text(encoding="utf-8"), output)
        for secret in ("stored-secret", "runtime-secret", "returned-secret"):
            self.assertNotIn(secret, output)
        self.assertIn("[REDACTED]", output)

    def test_show_secrets_applies_to_stdout_and_file(self):
        report = {"ok": True, "steps": [], "variables": {"api_key": "stored-secret"}}
        report_path = Path(self.temporary.name) / "result.json"
        code, output, errors, _ = self.invoke(
            self.arguments + ["--show-secrets", "--output", str(report_path)], report)
        self.assertEqual(code, 0)
        self.assertEqual(errors, "")
        self.assertIn("stored-secret", output)
        self.assertEqual(report_path.read_text(encoding="utf-8"), output)

    def test_invalid_variable_prevents_execution(self):
        for assignment in ("missing_equals", "=empty", "  =empty"):
            with self.subTest(assignment=assignment):
                code, output, errors, run = self.invoke(self.arguments + ["--var", assignment])
                self.assertEqual(code, 2)
                self.assertEqual(output, "")
                self.assertIn("KEY=VALUE", errors)
                run.assert_not_called()

    def test_missing_workspace_prevents_execution(self):
        code, output, errors, run = self.invoke(
            ["run", "--workspace", str(Path(self.temporary.name) / "missing.json"),
             "--sequence", "demo-sequence"])
        self.assertEqual(code, 2)
        self.assertEqual(output, "")
        self.assertIn("does not exist", errors)
        run.assert_not_called()

    def test_report_cannot_overwrite_workspace_or_execute_requests(self):
        before = self.workspace_path.read_bytes()
        code, output, errors, run = self.invoke(
            self.arguments + ["--output", str(self.workspace_path)])
        self.assertEqual(code, 2)
        self.assertEqual(output, "")
        self.assertIn("overwrite", errors)
        self.assertEqual(self.workspace_path.read_bytes(), before)
        run.assert_not_called()

    def test_duplicate_sequence_names_require_an_id(self):
        duplicate = {**self.workspace["sequences"][0], "id": "second-sequence"}
        self.workspace["sequences"].append(duplicate)
        WorkspaceStore(self.workspace_path).save(self.workspace)
        arguments = ["run", "--workspace", str(self.workspace_path),
                     "--sequence", "Health → fetch item"]
        code, output, errors, run = self.invoke(arguments)
        self.assertEqual(code, 2)
        self.assertEqual(output, "")
        self.assertIn("unique name or id", errors)
        run.assert_not_called()

    def test_server_is_the_default_command(self):
        with patch("api_test_client.__main__.serve") as serve:
            self.assertEqual(main(["--port", "0", "--no-browser", "--workspace", str(self.workspace_path)]), 0)
        serve.assert_called_once_with(self.workspace_path, 0, False)


if __name__ == "__main__":
    unittest.main()
