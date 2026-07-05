# Brief: research brief on <TOPIC>

Web research via harness-run SearXNG; synthesis with cited sources. The
executor sees only what the harness injects — queries must stand alone.

## Question

<One paragraph: the exact question, the audience, what decision this informs.>

## Steps

Single synthesis step (usually `executor: manager` — synthesis is judgment),
with web_queries like:

    "web_queries": [
      {"query": "<specific query 1>", "fetch_top": 2},
      {"query": "<specific query 2 — different angle>", "fetch_top": 1},
      {"query": "<counter-evidence angle>", "fetch_top": 0}
    ]

fetch_top only where snippets won't suffice (config caps: per-step query count
and total fetch_top). Target file: `<brief_out.md>`.

## Required output structure

    # <Title>
    ## Findings        (numbered, each with inline [n] source refs)
    ## Uncertainties   (what the sources disagree on or don't cover)
    ## Sources         (numbered list: title — URL, only URLs from provided results)

## Acceptance

    grep -c "## Sources" <brief_out.md>       # expect 1
    grep -c "## Uncertainties" <brief_out.md> # expect 1

DIRECTOR gate (not a step): verify every cited URL appears in
`.crew/web/step_<N>.json` evidence before trusting the brief. Manager-executed
steps get no model audit — read the output in full.
