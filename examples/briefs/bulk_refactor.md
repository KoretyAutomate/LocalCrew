# Brief: apply <PATTERN> across <SCOPE>

Mechanical transformation, one pattern, many sites. No behavior changes.

## The pattern

Before:

    <exact code shape to find>

After:

    <exact replacement shape>

Rules: <what varies per site, what must stay untouched, imports to add/remove>.

## Sites (exhaustive — the executor must not search)

- `<path/file1.py>` — <N occurrences: function foo, line-area hints>
- `<path/file2.py>` — ...

## Acceptance

Per step:

    python3 -c "import ast; ast.parse(open('<file>').read())"
    grep -c "<new pattern marker>" <file>     # expect <N>
    grep -c "<old pattern marker>" <file>     # expect 0  (use `test` if grep-c-0 exits nonzero)

Final step: `pytest tests/ -q` (or the project's suite) must pass unchanged.

## Out of scope

Anything not listed under Sites. No formatting churn on untouched lines.
