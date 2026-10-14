"""Keep the documented startup example runnable without external services."""

from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from api_test_client.__main__ import main
from api_test_client.config import load_config
from api_test_client.engine import run_sequence
from api_test_client.server import ClientServer


EXAMPLES = Path(__file__).resolve().parents[1] / "examples"


class StartupExampleTests(unittest.TestCase):
    def test_example_payload_and_private_overlay_load(self):
        config = load_config(EXAMPLES / "basic.tests.json", EXAMPLES / "basic.private.example.json")
        environment = config["environments"][0]
        self.assertEqual(environment["id"], "local-demo")
        message = next(row for row in environment["variables"] if row["key"] == "message")
        self.assertEqual(message["value"], "Hello from my private configuration")
        self.assertFalse(message["secret"])
        echo = next(request for request in config["requests"] if request["id"] == "echo")
        self.assertIn("{{run_id}}", echo["body"])
        self.assertNotIn("body_file", echo)
        self.assertEqual(config["sequences"][0]["id"], "basic-checks")

    def test_startup_does_not_execute_tests_or_change_source_files(self):
        sources = [EXAMPLES / "basic.tests.json", EXAMPLES / "basic.private.example.json",
                   EXAMPLES / "payloads" / "echo.json"]
        before = {path: path.read_bytes() for path in sources}
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory) / "workspace.json"
            with patch("api_test_client.__main__.serve") as serve:
                with patch("api_test_client.__main__.run_sequence") as run:
                    self.assertEqual(main([
                        "--config", str(sources[0]), "--private-config", str(sources[1]),
                        "--workspace", str(workspace), "--no-browser",
                    ]), 0)
            serve.assert_called_once()
            run.assert_not_called()
            self.assertTrue(workspace.exists())
        self.assertEqual(before, {path: path.read_bytes() for path in sources})

    def test_example_sequence_passes_against_local_demo(self):
        config = load_config(EXAMPLES / "basic.tests.json", EXAMPLES / "basic.private.example.json")
        with tempfile.TemporaryDirectory() as directory:
            server = ClientServer(("127.0.0.1", 0), Path(directory) / "workspace.json")
            worker = threading.Thread(target=server.serve_forever, daemon=True)
            worker.start()
            try:
                environment = config["environments"][0]
                environment["base_url"] = f"http://127.0.0.1:{server.server_port}"
                result = run_sequence(config["sequences"][0], config["requests"], environment,
                                      globals=config["variables"])
                self.assertTrue(result["ok"], result)
                self.assertEqual(len(result["steps"]), 2)
                echoed = result["steps"][1]["response"]["json"]["received"]
                self.assertEqual(echoed["message"], "Hello from my private configuration")
                self.assertEqual(echoed["run_id"], result["run_variables"]["$uuid"])
            finally:
                server.shutdown()
                server.server_close()
                worker.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
