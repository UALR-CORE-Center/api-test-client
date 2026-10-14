# Contributing

API Test Client aims to make everyday API testing straightforward. Improvements
should keep the application easy to run locally and preserve its lack of runtime
dependencies.

## Local development

Use Python 3.11 or newer. Clone the repository, then run from its root:

```sh
python -m api_test_client --no-browser
```

Open the local URL printed in the terminal. Changes to the static interface are
available after refreshing the browser; restart the server after changing Python
code.

Run the test suite before submitting a change:

```sh
python -m unittest discover -s tests -v
```

An optional editable installation exposes the `api-test-client` command:

```sh
python -m pip install -e .
api-test-client --help
```

## Pull requests

- Explain the problem and the resulting behavior.
- Add focused tests for changes to HTTP execution, variables, authentication,
  assertions, sequences, persistence, or server security.
- Keep platform differences in mind: CI runs on Windows and Linux.
- Update the README when a command or user-facing behavior changes.
- Use synthetic credentials and a local mock server in examples and tests.

Please keep real workspaces, request bodies, response data, API keys, and tokens
out of commits and public issues. Review exported files before attaching them;
arbitrary body content can contain sensitive data.

Contributions are licensed under the repository's [MIT license](LICENSE).
