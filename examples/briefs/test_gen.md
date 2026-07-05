# Brief: test generation for <MODULE(S)>

Write pytest tests for existing, working code. The code is the spec — do not
change any source file; emit test files only.

## Targets

For each unit under test, list (the plan cannot be more precise than this):

- `<package/module.py>` — function `<name>(<exact signature>)`:
  <2-3 sentences: what it does, key branches, error behavior>.
  Dependencies to fake: <none | sqlite tmp db | HTTP (inject fake) | ...>.
  Suggested cases: <happy path, edge X, error Y>.

## Test conventions (copy from an existing test file)

- Test files go in `<tests/>`, named `test_<module>.py`.
- Imports: `<e.g. from erareport.discovery.read_api import fetch_financials>`.
- Fixtures available in conftest: <list, or "none — tests are self-contained">.
- Mock style: <unittest.mock.patch / dependency injection / fake objects>.
  NOTE: mock-heavy steps sit at the intern's edge — mark them `executor: manager`.

## Acceptance

Each step's check must be runnable from the workspace root with bare binaries
(no absolute paths), e.g.:

    pytest tests/test_<module>.py -q

The full suite must still pass at the end: `pytest tests/ -q` as a final check.
If tests need a specific interpreter env, the DIRECTOR runs the crew with that
env's bin on PATH (crew_run env_path_prepend) — do not encode env paths here.

## Out of scope

Do not test <rendering/templates/network calls/...>. Do not modify source
files, conftest, or existing tests.
