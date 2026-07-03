# Brief: small statistics module

Create a Python module `stats.py` (workspace root, stdlib only, no third-party imports)
with three pure functions:

- `mean(values: list[float]) -> float` — arithmetic mean. Raise `ValueError("empty input")` on empty list.
- `median(values: list[float]) -> float` — median (average of two middle values for even length). Raise `ValueError("empty input")` on empty list.
- `stdev(values: list[float]) -> float` — population standard deviation. Raise `ValueError("need at least 2 values")` if fewer than 2 values.

Also create `tests/test_stats.py` using pytest covering: normal cases for all three
functions, the even/odd median split, and every error case with `pytest.raises`.

All tests must pass with `python3 -m pytest tests/test_stats.py -q`.
