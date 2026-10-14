"""Startup imports keep reusable tests separate from private local settings."""

import copy
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch

from api_test_client.config import load_config, load_config_sources, prepare_workspace
from api_test_client.workspace import WorkspaceStore, export_workspace, new_workspace


class StartupConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.config = self.directory / "tests.json"
        self.private = self.directory / "settings.private.json"
        self.live = self.directory / "live.workspace.json"
        self.workspace = new_workspace()
        self.workspace["variables"] = {"message": "public", "limit": 5, "enabled": False}
        self.workspace["environments"][0]["variables"] = {"region": "public", "unchanged": "keep"}
        self.write(self.config, self.workspace)

    def write(self, path, value):
        path.write_text(json.dumps(value), encoding="utf-8")

    def test_public_shorthand_and_row_defaults_are_normalized(self):
        value = load_config(self.config)
        self.assertEqual(value["variables"], [
            {"key": "message", "value": "public", "enabled": True, "secret": False},
            {"key": "limit", "value": "5", "enabled": True, "secret": False},
            {"key": "enabled", "value": "false", "enabled": True, "secret": False},
        ])
        self.assertEqual(value["environments"][0]["variables"][0]["key"], "region")

    def test_private_settings_merge_by_id_and_key_without_replacing_tests(self):
        self.workspace["environments"][0]["auth"] = {"type": "api_key", "header": "X-Test", "value": "{{api_key}}"}
        self.write(self.config, self.workspace)
        self.write(self.private, {
            "variables": {"message": "private-message", "new": [1, False, None]},
            "environments": [
                {"id": "local-demo", "base_url": "https://private.example.invalid",
                 "api_key": "local-only-secret", "auth": {"header": "X-Service-Key"},
                 "variables": [{"key": "region", "value": "private-region"},
                               {"key": "count", "value": 2, "secret": False}]},
                {"id": "second", "name": "Second", "base_url": "http://localhost:9090", "variables": {"flag": True}},
            ],
            "active_environment_id": "second",
        })
        before = (self.config.read_bytes(), self.private.read_bytes())
        value = load_config(self.config, self.private)
        self.assertEqual(value["active_environment_id"], "second")
        environment = value["environments"][0]
        self.assertEqual(environment["base_url"], "https://private.example.invalid")
        self.assertEqual(environment["api_key"], "local-only-secret")
        self.assertEqual(environment["auth"], {"type": "api_key", "header": "X-Service-Key", "value": "{{api_key}}"})
        self.assertEqual(environment["variables"], [
            {"key": "region", "value": "private-region", "enabled": True, "secret": True},
            {"key": "unchanged", "value": "keep", "enabled": True, "secret": False},
            {"key": "count", "value": "2", "enabled": True, "secret": False},
        ])
        self.assertEqual(value["variables"][0], {"key": "message", "value": "private-message", "enabled": True, "secret": True})
        self.assertEqual(value["variables"][-1]["value"], "[1,false,null]")
        self.assertEqual(value["requests"], load_config(self.config)["requests"])
        self.assertEqual(value["sequences"], self.workspace["sequences"])
        self.assertEqual(before, (self.config.read_bytes(), self.private.read_bytes()))
        exported = json.dumps(export_workspace(value))
        for secret in ("local-only-secret", "private-message", "private-region"):
            self.assertNotIn(secret, exported)

    def test_private_configuration_cannot_change_test_definitions(self):
        for field in ("requests", "sequences", "body_file", "arbitrary"):
            self.write(self.private, {field: []})
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, "may override only"):
                load_config(self.config, self.private)

    def test_body_files_are_relative_to_config_and_templates_stay_unresolved(self):
        bodies = self.directory / "payloads"
        bodies.mkdir()
        body = bodies / "request.json"
        payload = '{\n  "message": "{{message}}", "nested": [true, 3]\n}\n'
        body.write_text(payload, encoding="utf-8")
        request = self.workspace["requests"][1]
        request.pop("body")
        request.pop("body_type")
        request["body_file"] = "payloads/request.json"
        self.write(self.config, self.workspace)
        value, sources = load_config_sources(self.config)
        self.assertEqual(value["requests"][1]["body"], payload)
        self.assertEqual(value["requests"][1]["body_type"], "json")
        self.assertNotIn("body_file", value["requests"][1])
        self.assertEqual(sources, (self.config.resolve(), body.resolve()))

    def test_body_and_body_file_are_ambiguous_even_if_body_is_empty(self):
        self.workspace["requests"][0]["body_file"] = "payload.json"
        self.write(self.config, self.workspace)
        with self.assertRaisesRegex(ValueError, "both body and body_file"):
            load_config(self.config)

    def test_missing_or_invalid_body_fails_before_replacing_live_workspace(self):
        self.live.write_text("existing-workspace", encoding="utf-8")
        request = self.workspace["requests"][0]
        request.pop("body")
        request["body_file"] = "payload.json"
        self.write(self.config, self.workspace)
        body = self.directory / "payload.json"
        for content in (None, b"\xff\xfe", b"x" * (4 * 1024 * 1024 + 1)):
            if content is not None:
                body.write_bytes(content)
            with self.subTest(content="missing" if content is None else len(content)), self.assertRaises(ValueError):
                prepare_workspace(self.config, workspace_path=self.live)
            self.assertEqual(self.live.read_text(encoding="utf-8"), "existing-workspace")

    def test_bad_configs_never_modify_existing_live_workspace(self):
        self.live.write_text("existing-workspace", encoding="utf-8")
        invalid = ["not-json", json.dumps([]), json.dumps({"version": 2}), json.dumps({"version": True})]
        for raw in invalid:
            self.config.write_text(raw, encoding="utf-8")
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                prepare_workspace(self.config, workspace_path=self.live)
            self.assertEqual(self.live.read_text(encoding="utf-8"), "existing-workspace")
        self.write(self.config, self.workspace)
        for overlay in ({"requests": []}, {"environments": [{"id": "local-demo", "api_key": 42}]},
                        {"active_environment_id": "missing"}, {"version": 2}):
            self.write(self.private, overlay)
            with self.subTest(overlay=overlay), self.assertRaises(ValueError):
                prepare_workspace(self.config, self.private, self.live)
            self.assertEqual(self.live.read_text(encoding="utf-8"), "existing-workspace")

    def test_default_import_workspace_is_dedicated_and_reset_on_restart(self):
        ordinary = self.directory / ".api-test-client" / "workspace.json"
        ordinary.parent.mkdir()
        ordinary.write_text("ordinary-workspace", encoding="utf-8")
        with patch("api_test_client.config.Path.home", return_value=self.directory):
            destination, sources = prepare_workspace(self.config)
            self.assertEqual(destination.parent, ordinary.parent / "imports")
            self.assertEqual(sources, (self.config.resolve(),))
            modified = WorkspaceStore(destination).load()
            modified["requests"][0]["name"] = "Frontend edit"
            WorkspaceStore(destination).save(modified)
            restarted, _ = prepare_workspace(self.config)
        self.assertEqual(restarted, destination)
        self.assertEqual(WorkspaceStore(destination).load()["requests"][0]["name"], self.workspace["requests"][0]["name"])
        self.assertEqual(ordinary.read_text(encoding="utf-8"), "ordinary-workspace")

    def test_explicit_workspace_and_private_only_rejection(self):
        destination, _ = prepare_workspace(self.config, workspace_path=self.live)
        self.assertEqual(destination, self.live)
        self.assertEqual(WorkspaceStore(destination).load()["requests"][0]["id"], "demo-health")
        with self.assertRaisesRegex(ValueError, "requires --config"):
            prepare_workspace(private_path=self.private)
        with patch("api_test_client.config.default_path", return_value=self.live):
            self.assertEqual(prepare_workspace(), (self.live, ()))

    def test_workspace_cannot_overwrite_config_private_or_body_sources(self):
        body = self.directory / "payload.json"
        body.write_text('{"message":"{{message}}"}', encoding="utf-8")
        request = self.workspace["requests"][0]
        request.pop("body")
        request["body_file"] = body.name
        self.write(self.config, self.workspace)
        self.write(self.private, {"variables": {"message": "secret"}})
        for destination in (self.config, self.private, body):
            before = destination.read_bytes()
            with self.subTest(destination=destination.name), self.assertRaisesRegex(ValueError, "must not overwrite"):
                prepare_workspace(self.config, self.private, destination)
            self.assertEqual(destination.read_bytes(), before)

    @unittest.skipUnless(os.name == "posix", "POSIX source aliases and permissions")
    def test_source_aliases_are_protected_and_writable_copy_is_private(self):
        for link in ("symlink", "hardlink"):
            alias = self.directory / f"{link}.json"
            if link == "symlink":
                alias.symlink_to(self.config)
            else:
                os.link(self.config, alias)
            with self.subTest(link=link), self.assertRaisesRegex(ValueError, "must not overwrite"):
                prepare_workspace(self.config, workspace_path=alias)
        prepare_workspace(self.config, workspace_path=self.live)
        self.assertEqual(stat.S_IMODE(self.live.stat().st_mode), 0o600)

    def test_malformed_private_json_does_not_echo_credentials(self):
        self.private.write_text('{"api_key": "sensitive-never-print" missing}', encoding="utf-8")
        with self.assertRaises(ValueError) as caught:
            load_config(self.config, self.private)
        self.assertIn("Invalid JSON", str(caught.exception))
        self.assertNotIn("sensitive-never-print", str(caught.exception))

    def test_empty_workspace_and_unknown_active_environment(self):
        self.write(self.config, {"version": 1})
        self.assertEqual(load_config(self.config)["active_environment_id"], None)
        self.write(self.private, {"active_environment_id": []})
        with self.assertRaisesRegex(ValueError, "identify a configured environment"):
            load_config(self.config, self.private)

    def test_invalid_rows_and_duplicates_are_rejected(self):
        for variables in ("invalid", [{"key": "a", "secret": "true"}],
                          [{"key": "a"}, {"key": "a"}]):
            workspace = copy.deepcopy(self.workspace)
            workspace["variables"] = variables
            self.write(self.config, workspace)
            with self.subTest(variables=variables), self.assertRaises(ValueError):
                load_config(self.config)
        self.write(self.config, self.workspace)
        self.write(self.private, {"environments": [{"id": "x"}, {"id": "x"}]})
        with self.assertRaisesRegex(ValueError, "ids must be unique"):
            load_config(self.config, self.private)

    def test_wrong_nested_types_and_nonfinite_numbers_fail_cleanly(self):
        for field in ("method", "body_type"):
            workspace = copy.deepcopy(self.workspace)
            workspace["requests"][0][field] = {"malformed": "sensitive-never-print"}
            self.write(self.config, workspace)
            with self.subTest(field=field), self.assertRaises(ValueError) as caught:
                load_config(self.config)
            self.assertNotIn("sensitive-never-print", str(caught.exception))
        for numeric in ("NaN", "Infinity", "1e999"):
            self.config.write_text('{"version":1,"variables":{"number":' + numeric + '}}', encoding="utf-8")
            with self.subTest(numeric=numeric), self.assertRaisesRegex(ValueError, "Non-finite"):
                load_config(self.config)


if __name__ == "__main__":
    unittest.main()
