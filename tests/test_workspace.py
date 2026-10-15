"""Persistence and sharing checks that protect a user's saved credentials."""

import copy
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from urllib.parse import parse_qs, urlencode, urlsplit
from unittest.mock import patch

from api_test_client.workspace import (
    WorkspaceStore,
    atomic_write,
    export_workspace,
    new_workspace,
    redact_report,
    validate_workspace,
)


class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "private" / "workspace.json"
        self.store = WorkspaceStore(self.path)

    def test_new_workspace_is_runnable_and_save_survives_reopening(self):
        workspace = self.store.load()
        self.assertEqual(workspace["version"], 1)
        self.assertEqual(workspace["active_environment_id"], "local-demo")
        workspace["environments"].append({
            "id": "staging", "name": "Staging", "base_url": "https://example.invalid",
            "api_key": "saved-credential", "variables": [], "auth": {"type": "bearer"},
        })
        workspace["active_environment_id"] = "staging"
        self.store.save(workspace)
        reopened = WorkspaceStore(self.path).load()
        self.assertEqual(reopened["active_environment_id"], "staging")
        self.assertEqual(reopened["environments"][1]["api_key"], "saved-credential")

    def test_invalid_import_never_overwrites_existing_workspace(self):
        previous = self.path.read_bytes()
        cases = []
        candidate = self.store.load()
        candidate["version"] = 2
        cases.append(candidate)
        candidate = self.store.load()
        candidate["requests"].append(copy.deepcopy(candidate["requests"][0]))
        cases.append(candidate)
        candidate = self.store.load()
        candidate["sequences"][0]["steps"][0]["request_id"] = "deleted-request"
        cases.append(candidate)
        candidate = self.store.load()
        candidate["environments"][0]["variables"][0]["secret"] = "yes"
        cases.append(candidate)
        candidate = self.store.load()
        candidate["requests"][0]["timeout"] = 0
        cases.append(candidate)
        for candidate in cases:
            with self.subTest(candidate=candidate):
                with self.assertRaises(ValueError):
                    self.store.save(candidate)
                self.assertEqual(self.path.read_bytes(), previous)
                self.assertEqual(len(self.store.load()["requests"]), 3)

    def test_failed_atomic_replace_preserves_old_file_and_cleans_temporary(self):
        previous = self.path.read_bytes()
        with patch("api_test_client.workspace.os.replace", side_effect=OSError("disk failure")):
            with self.assertRaises(OSError):
                atomic_write(self.path, "replacement")
        self.assertEqual(self.path.read_bytes(), previous)
        self.assertEqual(list(self.path.parent.iterdir()), [self.path])

    def test_required_steps_round_trip_and_reject_non_boolean_values(self):
        workspace = self.store.load()
        step = workspace["sequences"][0]["steps"][0]
        step["required"] = True
        self.store.save(workspace)
        self.assertTrue(self.store.load()["sequences"][0]["steps"][0]["required"])
        for invalid in ("true", 1, None, [], {}):
            with self.subTest(invalid=invalid):
                step["required"] = invalid
                with self.assertRaisesRegex(ValueError, "Step required must be a boolean"):
                    validate_workspace(workspace)

    def test_corrupt_workspace_is_reported_without_replacement(self):
        self.path.write_text("not JSON", encoding="utf-8")
        with self.assertRaises(ValueError):
            WorkspaceStore(self.path)
        self.assertEqual(self.path.read_text(encoding="utf-8"), "not JSON")

    @unittest.skipUnless(os.name == "posix", "POSIX permissions")
    def test_private_directory_and_file_permissions(self):
        self.assertEqual(stat.S_IMODE(self.path.parent.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        self.path.chmod(0o644)
        self.store.save(self.store.load())
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)

    def test_validation_does_not_mutate_caller_and_repairs_stale_environment(self):
        original = {"version": 1, "environments": [{"id": "new", "name": "New"}],
                    "active_environment_id": "deleted"}
        snapshot = copy.deepcopy(original)
        validated = validate_workspace(original)
        self.assertEqual(original, snapshot)
        self.assertEqual(validated["active_environment_id"], "new")
        self.assertEqual(validated["requests"], [])

    def credential_workspace(self):
        workspace = new_workspace()
        workspace["variables"] = [
            {"key": "account", "value": "ordinary-public-value", "secret": False},
            {"key": "client_credential", "value": "private-variable-value", "secret": True},
        ]
        environment = workspace["environments"][0]
        environment["api_key"] = "environment-key-value"
        environment["auth"] = {"type": "api_key", "header": "X-Custom-Credential", "value": "custom-header-value"}
        workspace["requests"][0].update({
            "url": "https://example.invalid/health?session=custom-query-value&display=public",
            "headers": [{"key": "X-Custom-Credential", "value": "custom-header-value"},
                        {"key": "Accept", "value": "application/json"}],
            "params": [{"key": "session", "value": "custom-query-value"}],
            "auth": {"type": "api_key", "location": "query", "header": "session", "value": "custom-query-value"},
        })
        workspace["requests"][1]["auth"] = {"type": "basic", "username": "public-user", "password": "private-password-value"}
        workspace["requests"][2]["auth"] = {"type": "bearer", "token": "request-bearer-value"}
        return workspace

    def test_export_removes_credentials_and_keeps_useful_configuration(self):
        workspace = self.credential_workspace()
        snapshot = copy.deepcopy(workspace)
        exported = export_workspace(workspace)
        rendered = json.dumps(exported)
        for value in ("environment-key-value", "custom-header-value", "custom-query-value",
                      "private-password-value", "request-bearer-value", "private-variable-value"):
            self.assertNotIn(value, rendered)
        self.assertIn("ordinary-public-value", rendered)
        self.assertEqual(exported["requests"][0]["headers"][1]["value"], "application/json")
        self.assertEqual(exported["requests"][1]["auth"]["username"], "public-user")
        self.assertEqual(parse_qs(urlsplit(exported["requests"][0]["url"]).query, keep_blank_values=True),
                         {"session": [""], "display": ["public"]})
        self.assertEqual(workspace, snapshot)
        self.assertEqual(validate_workspace(exported)["version"], 1)

    def test_export_preserves_templates_for_secret_rows(self):
        workspace = self.credential_workspace()
        workspace["requests"][0]["body"] = '{"public":"{{account}}","key":"{{client_credential}}"}'
        self.assertEqual(export_workspace(workspace)["requests"][0]["body"],
                         workspace["requests"][0]["body"])

    def test_auth_variable_reference_is_a_credential_without_secret_flag(self):
        workspace = new_workspace()
        workspace["variables"] = [{"key": "service_key", "value": "referenced-credential", "secret": False}]
        workspace["environments"][0]["auth"] = {
            "type": "api_key", "header": "X-Service", "value": "{{service_key}}",
        }
        workspace["requests"][0]["url"] = "{{base_url}}/health?trace=referenced-credential"
        exported = export_workspace(workspace)
        self.assertNotIn("referenced-credential", json.dumps(exported))
        self.assertIn("{{base_url}}", exported["requests"][0]["url"])
        report = {"body": "echoed referenced-credential", "request": {"headers": {"X-Service": "referenced-credential"}}}
        self.assertNotIn("referenced-credential", json.dumps(redact_report(report, workspace)))

    def test_export_keeps_url_query_variable_placeholders_runnable(self):
        workspace = new_workspace()
        workspace["requests"][0]["url"] = "{{base_url}}/search?q={{search_term}}&page=1"
        exported = export_workspace(workspace)
        self.assertIn("q={{search_term}}", exported["requests"][0]["url"])
        self.assertIn("page=1", exported["requests"][0]["url"])

    def test_report_redacts_url_encoded_custom_query_credential(self):
        workspace = new_workspace()
        credential = "a+b/c="
        workspace["environments"][0]["auth"] = {
            "type": "api_key", "location": "query", "header": "client", "value": credential,
        }
        query = urlencode({"client": credential, "display": "public"})
        report = {"url": "https://example.invalid/health?" + query, "body": "plain response"}
        redacted = redact_report(report, workspace)
        self.assertNotIn("a%2Bb%2Fc%3D", redacted["url"])
        self.assertIn("display=public", redacted["url"])

    def test_report_redacts_known_secrets_and_nested_response_tokens(self):
        workspace = self.credential_workspace()
        payload = {"id": "public-id", "auth": {"access_token": "unknown-response-token"},
                   "children": [{"password": "unknown-response-password"}], "ready": True}
        report = {"ok": True, "steps": [{"response": {
            "url": "https://example.invalid/health?session=custom-query-value",
            "headers": {"Authorization": "Bearer request-bearer-value", "Content-Type": "application/json"},
            "body": json.dumps(payload), "json": payload,
            "request": {"body": "custom-header-value private-variable-value private-password-value"},
        }}]}
        snapshot = copy.deepcopy(report)
        redacted = redact_report(report, workspace)
        rendered = json.dumps(redacted)
        for value in ("custom-query-value", "custom-header-value", "private-variable-value",
                      "private-password-value", "request-bearer-value", "unknown-response-token",
                      "unknown-response-password"):
            self.assertNotIn(value, rendered)
        self.assertTrue(redacted["ok"])
        response = redacted["steps"][0]["response"]
        self.assertEqual(json.loads(response["body"])["auth"]["access_token"], "[REDACTED]")
        self.assertEqual(response["json"]["id"], "public-id")
        self.assertEqual(response["headers"]["Content-Type"], "application/json")
        self.assertEqual(report, snapshot)

    def test_report_redacts_json_escaped_scalar_credentials(self):
        workspace = new_workspace()
        credential = 'private-"quoted"-backslash\\-newline\n-Ω'
        workspace["variables"] = [{"key": "credential", "value": credential, "secret": True}]
        for ensure_ascii in (False, True):
            with self.subTest(ensure_ascii=ensure_ascii):
                body = json.dumps(credential, ensure_ascii=ensure_ascii)
                report = {"body": body, "json": credential,
                          "headers": {"X-Echo": body}, "debug": "echo " + body}
                redacted = redact_report(report, workspace)
                self.assertEqual(json.loads(redacted["body"]), "[REDACTED]")
                self.assertEqual(redacted["json"], "[REDACTED]")
                self.assertEqual(redacted["headers"]["X-Echo"], '"[REDACTED]"')
                self.assertEqual(redacted["debug"], 'echo "[REDACTED]"')
                self.assertEqual(report["body"], body)

    def test_export_removes_literal_json_escaped_credentials_but_keeps_templates(self):
        workspace = new_workspace()
        credential = 'private-"quoted"-backslash\\-newline\n-Ω'
        workspace["environments"][0]["api_key"] = credential
        workspace["requests"][0]["body"] = json.dumps({"credential": credential, "public": "ordinary"})
        workspace["requests"][1]["body"] = json.dumps(credential, ensure_ascii=False)
        workspace["requests"][2]["body"] = '{"credential": {{api_key | json}}, "name": "{{name}}"}'
        exported = export_workspace(workspace)
        self.assertEqual(json.loads(exported["requests"][0]["body"]), {"credential": "", "public": "ordinary"})
        self.assertEqual(json.loads(exported["requests"][1]["body"]), "")
        self.assertEqual(exported["requests"][2]["body"], workspace["requests"][2]["body"])
        self.assertEqual(workspace["environments"][0]["api_key"], credential)


if __name__ == "__main__":
    unittest.main()
