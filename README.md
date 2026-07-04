# LocalCrew

Delegate well-specified coding work to **local LLMs**, with quality gates at every level.

```
director (you + Claude Code)   — writes the brief, reviews the plan, approves the report
manager  (Qwen3.5-122B, vLLM)  — expands the brief into a concrete step plan; audits results
intern   (qwen3:8b, Ollama)    — executes one step at a time, emitting full files
```

The economics: a plan detailed enough for an 8B executor is verbose but *cheap to review*.
Errors get caught at plan time (costs a paragraph) instead of debug time (costs a session).
The intern can't push back on ambiguity, so the manager prompt forces self-contained steps
with exact signatures, concrete input→output examples, and machine-checkable acceptance
criteria. The manager audits conformance ("did step N happen as written"); the director
audits the decomposition ("is the plan sound").

**When NOT to use this:** if the plan would be as long as the code, skip the intern and
write the code directly. The hierarchy pays only for mechanical, voluminous work
(bulk refactors, test generation, applying a pattern across files).

Not code-only: any task that emits **files** with **machine-checkable acceptance**
qualifies — data files, markdown briefings, config. Use structural checks
(`grep -c "## Section" out.md`, `python3 -c "import json; json.load(open('x.json'))"`)
for non-code outputs. The looser the checks, the more weight falls on audits and
director review.

## Executor tiers

Each step carries `"executor": "intern" | "manager"` (default intern). The decision
ladder: **intern** for mechanical, well-specified steps; **manager** for steps needing
real judgment (multi-constraint synthesis, conflicting sources, subtle logic) that are
still too voluminous to keep upstairs; **the director writes it directly** when the
plan would be as long as the output. Manager-executed steps skip the manager audit
(self-review is near-worthless) — the report flags them **"director must review"**
instead, so the human/Claude gate replaces the model gate.

## Web research (SearXNG, harness-run)

The models stay tool-less; the **harness** runs searches. Steps may declare

```json
"web_queries": [{"query": "kv cache quantization fp8", "fetch_top": 2}]
```

Before the executor runs, the harness queries SearXNG (`searxng.endpoint` in config;
delete/empty to disable), injects top results (title/URL/snippet) and — for
`fetch_top` — full page text (HTML-stripped, truncated) into the executor's prompt.
Results count against the context budget; the raw evidence is saved to
`.crew/web/step_<N>.json`; the audit gets the source list so citation claims are
checkable. Search failure fails the step *before* the LLM call — never silently
research-less. Fetches of result URLs are guarded (http/https only, no
loopback/private hosts, bounded reads, text content-types, capped redirects).

Patterns that work well:
- **Research brief**: web_queries + a synthesis step (usually `executor: manager`)
  producing a structured .md with a required `## Sources` section, checked via `grep -c`.
- **Map-reduce over files**: N steps each reading a chunk via `context_files` and
  emitting `findings/<n>.json`, plus a final aggregation step. Bulk extraction and
  classification over a *known* file set is the crew's sweet spot; interactive
  "find where/why X" exploration is not — that needs tools the executors don't have.

## Requirements

- Python 3 (stdlib only — no pip installs)
- vLLM serving an OpenAI-compatible endpoint (default `localhost:8000`)
- Ollama (default `localhost:11434`)

Endpoints, models, timeouts, token budgets and the check-binary allowlist live in
`config.json`.

## Usage

```bash
python3 crew.py stats                                 # usage + success rates (global ledger)
python3 crew.py health                                # both endpoints answering?

python3 crew.py plan --brief brief.md --workspace ws/ # manager writes ws/plan.json
# -> DIRECTOR REVIEWS ws/plan.json (this gate decides everything)

python3 crew.py run --workspace ws/                   # execute all steps
python3 crew.py run --workspace ws/ --resume          # continue after a failed step
python3 crew.py run --workspace ws/ --step 2          # one step only
python3 crew.py run --workspace ws/ --dry-run         # show what would run
python3 crew.py audit --workspace ws/ --step 2        # re-check + re-audit after manual fixes
```

A run is **fail-fast**: any step that exhausts its retries or fails the manager audit
stops the run and writes a report for the director.

## Skills (shared with Claude Code)

Skills use **Claude Code's native format** — `<dir>/<name>/SKILL.md` with `name:`/
`description:` frontmatter — so one file serves the whole hierarchy: Claude Code
discovers workspace `.claude/skills` natively, and the crew reads the same files.

- Search dirs: `skills.dirs` in config (default `["{workspace}/.claude/skills"]`).
  `~/.claude/skills` is deliberately NOT a default — it's full of Claude-Code
  operational skills that would mislead an 8B intern. Opt in only if crew-safe.
- `crew.py skills --workspace ws` lists the discovered catalog.
- At `plan` time the manager sees the catalog (names + descriptions) and may attach
  `"skills": [...]` per step — default zero, max `skills.max_per_step` (2), only when
  the step's acceptance depends on it. Attached skills show in the plan summary with
  sizes so the director can veto over-attachment.
- At `run` time the full skill bodies are injected into the intern's prompt and the
  manager's audit prompt; they share the context char budget with context files.

**Content convention (not machine-enforced):** crew-visible skills must be
model-agnostic conventions/checklists — output formats, invariants, style rules.
No Claude Code tool names, slash commands, or MCP references: an 8B intern will
dutifully "follow" instructions it cannot execute.

## Learning loop: skills proposed from failures

When a run has trouble (any step retried or non-DONE), the manager makes ONE extra
call over the failure evidence (all intern attempts + retry feedback from this run's
`run_log.jsonl`, check tails, audit issues) and returns at most one skill proposal —
or null, the expected common case (step-specific problems, harness-absorbed defects,
and catalog duplicates must NOT become skills). Toggle: `skills.auto_propose`.

Proposals are **staged** in `.crew/skill_proposals/<name>/SKILL.md` — never written
to `.claude/skills` directly. One `cat` shows rationale + description + body
(rationale lives in frontmatter, so it's inert to discovery and prompt injection).
The director decides:

```bash
python3 crew.py propose-skill --workspace ws --step N   # on-demand proposal
python3 crew.py approve-skill --workspace ws --name n   # promote to .claude/skills
python3 crew.py approve-skill --workspace ws --name n --attach 1   # ...and attach to
        # step 1 of plan.json, re-syncing the resume hash — then: run --resume
python3 crew.py reject-skill  --workspace ws --name n   # delete the staging dir
```

`run_report.md` lists pending proposals with paste-ready commands. The tool never
self-approves; a proposal-call failure never changes the run's exit status.

## Per-step execution loop

1. Gather `context_files` (missing file or blown char budget fails the step *before* any
   LLM call — a missing context file means a dependency broke).
2. Intern emits `{"files":[{"path","content"}], "notes"}` (Ollama `format: json`).
3. Output validated *before* writing: paths must be within the step's `target_files`,
   contents non-empty; violations are fed back as a retry.
4. Pre-existing targets are snapshotted to `.crew/backup/step_<N>/`, then files written
   (sandboxed to the workspace).
5. Acceptance checks run (`shlex.split` + `shell=False`, cwd=workspace, timeout,
   allowlisted binaries). Failures feed stderr back to the intern (max 2 retries).
6. Manager audits the result against the step spec; a `fail` verdict stops the run.
   An unparseable audit is treated as a fail (fail-safe).

## Usage ledger

Every `run` appends one JSONL record (workspace, task, per-step status/attempts/executor,
outcome, skill proposal) to `run.ledger` — default `~/.localcrew/ledger.jsonl`, empty
string disables. `crew.py stats [--last N]` aggregates run/step success rates and a
per-executor breakdown. Ledger writes never fail a run.

## Workspace artifacts

```
ws/plan.json               the reviewed plan (sha256 recorded in state + report)
ws/.crew/run_state.json    per-step status — powers --resume
ws/.crew/run_log.jsonl     every LLM call: role, tag, duration, full response
ws/.crew/run_report.md     director-facing summary (written even on abort)
ws/.crew/backup/step_N/    pre-write snapshots of target files
```

## Safety model

- File writes are restricted to the workspace **and** to each step's declared
  `target_files`.
- Checks run without a shell; pipes/`;`/redirects are inert and rejected up front.
  `bash` is allowed only as `bash -n <file>`. No absolute or `..` path arguments.
- Honest caveat: `python3 -c` and pytest *are* arbitrary code execution — the trust
  anchor is the director's plan review plus the plan sha256 tying the reviewed plan to
  the executed run, not the command validator.

## Known LLM quirks baked in (do not "simplify" these away)

- vLLM needs `chat_template_kwargs: {"enable_thinking": false}` or reasoning burns the
  whole token budget and `content` comes back null.
- Ollama must be called via the **native** `/api/chat` with `"think": false` — the
  OpenAI-compat endpoint at `:11434/v1` ignores thinking switches.
- Null/empty `content` is always treated as failure, never as an empty-but-OK answer.
