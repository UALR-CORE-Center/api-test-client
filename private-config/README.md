# Private configurations

Keep your local test configurations, environment settings, API keys, request
payloads, writable workspaces, and reports in this folder. Everything here
except this README is ignored by Git, including files in subfolders.

Extract a complete test kit here, preserving its payload directory. Copy its
private settings example to the filename you want to use, then fill in your
URL, API key, and any required variables. The application's `body_file` paths
are relative to the test configuration, so a complete kit can move together.

From the repository root, use your kit's actual filenames:

```powershell
py -m api_test_client --config private-config/tests.json --private-config private-config/settings.private.json --workspace private-config/workspace.local.json
```

Starting the app loads the files. Choose a request or sequence in the browser
to execute it. Browser saves update the writable workspace; restarting with
`--config` reloads your source files.

For the included offline demo:

```powershell
Copy-Item examples/basic.private.example.json private-config/basic.private.json
py -m api_test_client --config examples/basic.tests.json --private-config private-config/basic.private.json --workspace private-config/workspace.local.json
```

On macOS/Linux, use `cp` instead of `Copy-Item` and `python3` instead of `py`.
