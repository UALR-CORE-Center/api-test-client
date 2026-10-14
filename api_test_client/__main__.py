"""Launch the browser application or run a saved sequence from a terminal."""

import argparse
import json
from pathlib import Path
import re
import sys

from .engine import run_sequence
from .config import paths_collide, prepare_workspace
from .server import select_environment, serve
from .workspace import WorkspaceStore, atomic_write, redact_report


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] not in {"serve", "run", "-h", "--help"}:
        argv.insert(0, "serve")
    parser = argparse.ArgumentParser(description="A lightweight local API client and sequence runner.")
    commands = parser.add_subparsers(dest="command", required=True)
    browser = commands.add_parser("serve", help="Open the local browser interface (default)")
    browser.add_argument("--workspace", type=Path, help="Writable workspace path; importing a config replaces its contents")
    browser.add_argument("--config", type=Path, help="Load reusable tests from a JSON configuration before startup")
    browser.add_argument("--private-config", type=Path, help="Overlay local environment settings and variables (requires --config)")
    browser.add_argument("--port", type=int, default=8765)
    browser.add_argument("--no-browser", action="store_true")
    runner = commands.add_parser("run", help="Run a saved sequence and emit a JSON report")
    runner.add_argument("--workspace", type=Path, help="Writable workspace path; importing a config replaces its contents")
    runner.add_argument("--config", type=Path, help="Load reusable tests from a JSON configuration before running")
    runner.add_argument("--private-config", type=Path, help="Overlay local environment settings and variables (requires --config)")
    runner.add_argument("--sequence", required=True, help="Sequence id or unique name")
    runner.add_argument("--environment", help="Environment id or unique name; default: active environment")
    runner.add_argument("--var", action="append", default=[], metavar="KEY=VALUE", help="Override a variable; repeatable")
    runner.add_argument("--output", type=Path, help="Also save the report to a JSON file")
    runner.add_argument("--show-secrets", action="store_true", help="Include credential values in the report (otherwise redacted)")
    args = parser.parse_args(argv)
    try:
        if args.command == "serve" and not 0 <= args.port <= 65535:
            raise ValueError("Port must be between 0 and 65535 (0 selects an available port).")
        args.workspace, source_paths = prepare_workspace(args.config, args.private_config, args.workspace)
        if args.command == "serve":
            serve(args.workspace, args.port, not args.no_browser)
            return 0
        if not args.workspace.expanduser().exists():
            raise ValueError("Workspace does not exist. Start the browser app and save a sequence first.")
        workspace = WorkspaceStore(args.workspace).load()
        candidates = [s for s in workspace["sequences"] if s["id"] == args.sequence]
        if not candidates:
            candidates = [s for s in workspace["sequences"] if s["name"] == args.sequence]
        if len(candidates) != 1:
            raise ValueError("Choose an existing sequence by its unique name or id.")
        environment = select_environment(workspace, args.environment)
        variables = {}
        for assignment in args.var:
            key, separator, value = assignment.partition("=")
            if not separator or not key.strip():
                raise ValueError("Each --var must use KEY=VALUE.")
            variables[key.strip()] = value
        if args.output and any(paths_collide(args.output, source) for source in (args.workspace, *source_paths)):
            raise ValueError("Report output must not overwrite the workspace, configuration, or body source files.")
        result = run_sequence(candidates[0], workspace["requests"], environment,
                              variables=variables, globals=workspace["variables"])
        if not args.show_secrets:
            # Include runtime secrets in the redaction context as well.
            context = {**workspace, "runtime": [{"key": k, "value": v, "secret": bool(re.search(r'token|secret|password|api[-_]?key', k, re.I))} for k, v in variables.items()]}
            result = redact_report(result, context)
        rendered = json.dumps(result, indent=2, ensure_ascii=False) + "\n"
        if args.output:
            atomic_write(args.output.expanduser(), rendered)
        print(rendered, end="")
        return 0 if result["ok"] else 1
    except (OSError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\nCancelled.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
