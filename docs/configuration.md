# Configuration files

Load reusable requests and sequences with `--config`, then add your local URL,
credentials, and variables with `--private-config`. Neither file is changed by
the application. Loading files never sends requests automatically.

## Start with the included example

From the repository root, copy the private template into `private-config/`
and start the web app.

**Windows**

```powershell
Copy-Item examples/basic.private.example.json private-config/basic.private.json
py -m api_test_client --config examples/basic.tests.json --private-config private-config/basic.private.json --workspace private-config/workspace.local.json
```

**macOS / Linux**

```sh
cp examples/basic.private.example.json private-config/basic.private.json
python3 -m api_test_client --config examples/basic.tests.json --private-config private-config/basic.private.json --workspace private-config/workspace.local.json
```

Choose **Basic checks** in the Sequences view and click Run. It checks the local
demo API's health and echoes a JSON payload from a file. No API key is needed.
For a different server port, also change `base_url` in your private file.

## Keep local test kits together

The repository includes `private-config/` for your own test definitions,
credentials, payloads, workspaces, and reports. Everything inside it is ignored
by Git except the instruction README that keeps the folder in fresh clones.
Extract downloaded test kits directly into this folder, retaining their
`payloads/` directories. The loader resolves payload paths relative to the
configuration file, so moving a complete kit preserves its references.

For a kit containing `tests.json` and `settings.private.json`, run:

```powershell
py -m api_test_client --config private-config/tests.json --private-config private-config/settings.private.json --workspace private-config/workspace.local.json
```

Use the actual filenames supplied by your kit. You can also keep independent
kits in subfolders, such as `private-config/project-a/`; they are ignored too.

## A complete startup configuration

The format is the version 1 workspace format, with two conveniences: variables
can be an object, and a request can load its body from a file. For example, save
this as `tests.json`:

```json
{
  "version": 1,
  "active_environment_id": "local-demo",
  "variables": {
    "run_id": "{{$uuid}}"
  },
  "environments": [
    {
      "id": "local-demo",
      "name": "Local demo",
      "base_url": "http://127.0.0.1:8765",
      "api_key": "",
      "auth": {"type": "none"},
      "variables": {"message": "Hello"}
    }
  ],
  "requests": [
    {
      "id": "echo",
      "name": "Echo JSON",
      "method": "POST",
      "url": "/demo/echo",
      "params": [],
      "headers": [],
      "auth": {"type": "inherit"},
      "timeout": 30,
      "follow_redirects": false,
      "body_type": "json",
      "body_file": "payloads/echo.json",
      "assertions": [
        {"type": "status", "expected": 200},
        {"type": "json_equals", "path": "$.received.run_id", "expected": "{{run_id}}"}
      ],
      "extract": [{"name": "initial_response", "path": "$"}]
    }
  ],
  "sequences": [
    {
      "id": "echo-check",
      "name": "Echo check",
      "stop_on_failure": true,
      "steps": [{"request_id": "echo", "variables": {}}]
    }
  ]
}
```

Save the payload as `payloads/echo.json`, beside `tests.json`:

```json
{
  "message": "{{message}}",
  "run_id": "{{run_id}}"
}
```

`body_file` paths resolve relative to the startup configuration, regardless of
the terminal's current directory. Payload files must use UTF-8. They can contain
variable placeholders; substitution happens when you send a request. Omitted
`body_type` defaults to `json` for `body_file`. Use `text` or `form` explicitly
for other bodies. You may supply either `body_file` or an inline string `body`,
but not both. JSON bodies must produce valid JSON after substitution.

Environments, requests, and sequences each need unique IDs within their
collection. Steps use `request_id` to identify requests. Query parameters and
headers are row arrays, for example
`[{"key": "Accept", "value": "application/json", "enabled": true}]`.
Each input file and the resulting workspace have a 4 MiB limit.

## Private settings

Save settings as `settings.private.json` and pass it with `--private-config`:

```json
{
  "active_environment_id": "local-demo",
  "environments": [
    {
      "id": "local-demo",
      "base_url": "http://127.0.0.1:8765",
      "api_key": "",
      "variables": [
        {"key": "message", "value": "My local message", "secret": false}
      ]
    }
  ]
}
```

For your API, replace the URL and API key in this private file. Set the
environment's authentication object to suit its contract:

| Authentication | Environment `auth` value |
| --- | --- |
| No authentication | `{"type": "none"}` |
| Bearer token | `{"type": "bearer", "token": "{{api_key}}"}` |
| API key header | `{"type": "api_key", "header": "X-API-Key", "value": "{{api_key}}", "location": "header"}` |
| API key query parameter | `{"type": "api_key", "header": "api_key", "value": "{{api_key}}", "location": "query"}` |
| Basic | `{"type": "basic", "username": "{{username}}", "password": "{{password}}"}` |

The private overlay accepts only `variables`, `environments`,
`active_environment_id`, and optional `"version": 1`. It cannot replace
requests or sequences. Environments merge by `id`; variable rows merge by
`key`, and authentication fields merge by field name. Omitted settings retain
their public values. The overlay can also add environments.

Global and environment variables accept either `{"name": "value"}` or row
arrays with `key`, `value`, `enabled`, and `secret`. Variable values are stored
as strings. Private variables default to `secret: true`; set `secret: false`
for nonsecret values you want to keep in exports. Public variables default to
`secret: false`. Authentication secrets are removed from exports regardless.

The contents of `private-config/` (except its instruction README),
`*.private.json`, and the repository's `local-tests/` directory are ignored by
Git. Credentials also exist in the writable workspace copy, in plaintext; keep
both private settings and workspace copies out of version control.

## Generated values and response comparisons

`{{$uuid}}`, `{{$timestamp}}`, and `{{$timestamp_minus_1h}}` are generated once
per standalone request or whole sequence. Timestamps use UTC ISO 8601 strings.
Every step in a sequence sees the same values; the next run generates new
ones. These names are reserved and cannot be overridden, but ordinary variables
can alias them, such as `"run_id": "{{$uuid}}"` or
`"scanned_at": "{{$timestamp}}"`.

Use `"expected": "{{run_id}}"` to compare a string. Template expectations
remain strings, so use literal numbers and booleans to check those JSON types.
To compare against an extracted value while retaining its JSON type, use
`expected_variable` instead of `expected`:

```json
{"type": "json_equals", "path": "$", "expected_variable": "initial_response"}
```

Here, an earlier step extracted the complete response using
`{"name": "initial_response", "path": "$"}`. Extractors also support field and
array paths such as `$.items[0].id`. `expected_variable` names the variable
directly; it does not use braces.

Additional numeric and length checks:

```json
[
  {"type": "json_greater_than", "path": "$.count", "expected": 0},
  {"type": "json_length_equals", "path": "$.items", "expected": 2}
]
```

`json_greater_than` requires numeric values. `json_length_equals` accepts an
array, object, or string and a nonnegative integer expectation. See the
[README](../README.md#requests-and-response-checks) for all assertion types.

## Resolve a name before running dependent requests

Add a lookup request before requests that need an ID. Use your API's exact
filter and check its total match count, rather than accepting the first result
from a partial search. This example assumes an API that accepts an exact-name
filter at `/assets/search` and returns `{"count": 1, "results": [{"id": 42}]}`.
Adapt the route, filter, and response paths to your API.

```json
{
  "id": "resolve-asset",
  "name": "Resolve asset name",
  "method": "POST",
  "url": "/assets/search",
  "body_type": "json",
  "body": "{\"name\": {{asset_name | json}}, \"match\": \"exact\", \"limit\": 2}",
  "assertions": [
    {"name": "Lookup accepted", "type": "status", "expected": 200},
    {"name": "Exactly one matching asset", "type": "json_equals", "path": "$.count", "expected": 1},
    {"type": "json_length_equals", "path": "$.results", "expected": 1},
    {"type": "json_greater_than", "path": "$.results[0].id", "expected": 0}
  ],
  "extract": [{"name": "asset_id", "path": "$.results[0].id"}]
}
```

Use the unquoted `{{asset_name | json}}` placeholder in inline bodies or
`body_file` templates. It serializes the resolved value as JSON, safely escaping
quotes, backslashes, and other characters in a name. Strings stay strings;
native numbers, booleans, objects, arrays, and null keep their types. Aliases
inside strings expand before serialization. Plain `{{asset_name}}` still inserts
raw text. Do not add quotes around a `| json` placeholder.

Put `asset_name` in your private environment variables and place the lookup
first in the sequence:

```json
{
  "id": "asset-checks",
  "name": "Look up asset and run checks",
  "stop_on_failure": false,
  "steps": [
    {"request_id": "resolve-asset", "required": true},
    {"request_id": "check-asset"}
  ]
}
```

Here, `check-asset` is a separately saved request using `{{asset_id}}`, for
example in `/assets/{{asset_id}}`. A required step stops the sequence if its
request, assertions, or extraction fails, even with `stop_on_failure: false`.
Failed steps never publish extracted variables to later requests. IDs are
resolved anew for each run; lookups do not rewrite your private file.

If lookups require different credentials, set their request-level `auth` to a
separate private variable. Other requests can continue to inherit the active
environment's authentication. Use client version **0.3.0** or newer for required
steps and the JSON interpolation modifier.

## Terminal runs and reload behavior

For frontend runs, the **Sequence run** panel displays failure details for each
step. Select **Inspect response** for the complete response and **Download run
log** to save a JSON report with recognized credentials masked. The first failed
step is selected automatically when a run finishes. Download a report before
reloading or closing the page. Restarting the server clears its run history.

Start without opening a browser:

```sh
python3 -m api_test_client serve --config examples/basic.tests.json --private-config private-config/basic.private.json --workspace private-config/workspace.local.json --no-browser
```

For this demo, leave that server running and use a second terminal to execute:

```sh
python3 -m api_test_client run --config examples/basic.tests.json --private-config private-config/basic.private.json --workspace private-config/workspace.local.json --sequence basic-checks --output private-config/basic.report.json
```

On Windows, replace `python3` with `py`. A real external API needs only the
`run` command; the local web app does not need to run. `--environment` selects
an environment by ID or unique name; repeat `--var NAME=VALUE` to supply runtime
overrides. Reports redact known secret values by default. Exit codes are 0 for
passing checks, 1 for failed checks, and 2 for invalid inputs.

Each explicit `--config` startup or terminal run reloads the configuration,
private overlay, and body files. It replaces the derived workspace at
`~/.api-test-client/imports/<configuration-path-hash>.workspace.json`; the hash
uses the resolved configuration path. Pass `--workspace ./workspace.local.json`
to choose a different writable copy. `--private-config` requires `--config`.

Browser saves affect only the copy. To retain browser edits, export the
workspace and review it before using it as a new source configuration. To apply
file edits to the web app, restart with the same flags. Restarting with
`--config` replaces earlier edits in the copy. Starting without `--config`
preserves normal workspace behavior, including `API_TEST_CLIENT_WORKSPACE`.
Workspace and report destinations cannot overwrite configuration, private, or
payload source files.
