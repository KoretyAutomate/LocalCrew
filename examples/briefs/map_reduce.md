# Brief: extract/classify <WHAT> from <KNOWN FILE SET>

Map-reduce over a fixed, enumerated input set. Executors have no tools: every
input must be a declared context_file; every output a declared target_file.

## Map steps (one per input or small batch)

For each input file, read it (via context_files) and emit
`findings/<n>.json` with EXACTLY this shape:

    {"source": "<input path>", "items": [{"<field>": "...", "<field2>": "..."}]}

Classification rubric (make it decidable, no judgment calls):
- <label A>: <criteria>
- <label B>: <criteria>
- unknown: anything not clearly A or B — never guess.

Inputs (exhaustive): `<data/a.md>`, `<data/b.md>`, ...

## Reduce step

Read all `findings/*.json` (context_files), emit `<summary.md|combined.json>`
with <structure: sections/keys>. Use `executor: manager` if the merge needs
any reconciliation judgment.

## Acceptance

Per map step:

    python3 -c "import json; d=json.load(open('findings/<n>.json')); assert d['source']"

Reduce:

    python3 -c "import json; json.load(open('combined.json'))"
    grep -c "## <required section>" summary.md    # expect >= 1
