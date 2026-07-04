#!/usr/bin/env python3
"""LocalCrew — delegate well-specified coding work to local LLMs.

Roles:
  manager  Qwen3.5-122B via vLLM   — expands a brief into a concrete plan; audits results
  intern   qwen3:8b via Ollama     — executes one plan step at a time (full-file writes)
  director the human + Claude Code — reviews the plan, approves the run report

Stdlib only. See PLAN.md for the full design.
"""

import argparse
import hashlib
import json
import re
import shlex
import shutil
import subprocess
import sys
import time
import html
import ipaddress
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_CONFIG = Path(__file__).resolve().parent / "config.json"

SHELL_OPERATOR_TOKENS = {"|", "||", "&&", ";", "&", ">", ">>", "<", "<<"}


class CrewError(Exception):
    pass


class LLMError(CrewError):
    pass


# ---------------------------------------------------------------- utilities

def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_config(path):
    with open(path) as f:
        return json.load(f)


def crew_dir(workspace: Path) -> Path:
    d = workspace / ".crew"
    d.mkdir(parents=True, exist_ok=True)
    return d


def log_event(workspace, record):
    """Append one JSONL record to the workspace run log (no-op if workspace is None)."""
    if workspace is None:
        return
    record = {"ts": now_iso(), **record}
    with open(crew_dir(workspace) / "run_log.jsonl", "a") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def extract_json(text):
    """Extract the outermost JSON object from LLM output (strips think blocks / fences)."""
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S)
    text = re.sub(r"```(?:json)?", "", text)
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        raise CrewError("no JSON object found in output")
    return json.loads(text[start : end + 1])


# ---------------------------------------------------------------- web research

def url_violation(url):
    """Guardrail check for fetch_top URLs. Returns a reason string, or None if OK."""
    try:
        p = urllib.parse.urlparse(url)
    except ValueError as e:
        return f"unparseable URL: {e}"
    if p.scheme not in ("http", "https"):
        return f"scheme {p.scheme!r} not allowed"
    host = p.hostname or ""
    if not host:
        return "no host"
    if host == "localhost":
        return "localhost blocked"
    try:
        ip = ipaddress.ip_address(host)
        if ip.is_loopback or ip.is_private or ip.is_link_local:
            return f"private/loopback address {host} blocked"
    except ValueError:
        pass  # a hostname, not an IP literal
    return None


class _GuardedRedirectHandler(urllib.request.HTTPRedirectHandler):
    max_redirections = 5

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        err = url_violation(newurl)
        if err:
            raise CrewError(f"redirect blocked ({err}): {newurl}")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def html_to_text(raw):
    raw = re.sub(r"(?is)<(script|style|noscript)\b.*?</\1>", " ", raw)
    raw = re.sub(r"(?s)<[^>]+>", " ", raw)
    return re.sub(r"\s+", " ", html.unescape(raw)).strip()


def fetch_page_text(url, sx):
    """Fetch one external page with guardrails. Returns text, or raises CrewError."""
    err = url_violation(url)
    if err:
        raise CrewError(err)
    opener = urllib.request.build_opener(_GuardedRedirectHandler)
    req = urllib.request.Request(url, headers={"User-Agent": "LocalCrew/1.3"})
    try:
        with opener.open(req, timeout=sx.get("timeout", 30)) as r:
            ctype = (r.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            if ctype and not (ctype.startswith("text/") or ctype == "application/xhtml+xml"):
                raise CrewError(f"content-type {ctype!r} skipped")
            raw = r.read(sx.get("max_page_bytes", 262144)).decode("utf-8", errors="replace")
    except (urllib.error.URLError, OSError, TimeoutError) as e:
        raise CrewError(str(e)) from e
    text = html_to_text(raw)
    if not text:
        raise CrewError("page produced no text")
    return text[: sx.get("page_chars", 4000)]


def searxng_search(sx, query):
    """One SearXNG query (retry once). Returns [{title,url,snippet}], or raises CrewError."""
    qs = urllib.parse.urlencode({"q": query, "format": "json"})
    url = f"{sx['endpoint']}?{qs}"
    req = urllib.request.Request(url, headers={"User-Agent": "LocalCrew/1.3"})
    last = None
    for attempt in (1, 2):
        try:
            with urllib.request.urlopen(req, timeout=sx.get("timeout", 30)) as r:
                data = json.loads(r.read())
            results = data.get("results") or []
            return [{"title": x.get("title") or "(untitled)",
                     "url": x.get("url") or "",
                     "snippet": (x.get("content") or "").strip()}
                    for x in results[: sx.get("max_results", 5)]]
        except (urllib.error.URLError, OSError, TimeoutError, json.JSONDecodeError) as e:
            last = e
            if attempt == 1:
                time.sleep(2)
    raise CrewError(f"SearXNG query failed after retry: {last}")


def gather_web_context(cfg, workspace, step, cache):
    """Run a step's web_queries via the harness. Returns (context text, sources list).

    Raises CrewError if search itself is unavailable (fail-fast — never silently
    research-less). Page-fetch failures degrade to notes. Saves the raw evidence to
    .crew/web/step_<id>.json.
    """
    queries = step.get("web_queries", []) or []
    if not queries:
        return "", []
    sx = cfg.get("searxng") or {}
    if not searxng_enabled(cfg):
        raise CrewError("step has web_queries but searxng is not configured")
    sections, sources, raw_dump = [], [], []
    for q in queries:
        query = q["query"].strip()
        fetch_top = q.get("fetch_top", 0)
        key = ("q", " ".join(query.lower().split()))
        if key not in cache:
            cache[key] = searxng_search(sx, query)
        results = cache[key]
        block = f'--- WEB SEARCH: "{query}" ---\n'
        if not results:
            block += "(no results for this query — do not invent sources)\n"
        for i, res in enumerate(results):
            block += f"[{i+1}] {res['title']}\n    {res['url']}\n    {res['snippet']}\n"
            sources.append((res["title"], res["url"]))
        fetched = []
        for res in results[:fetch_top]:
            ukey = ("page", res["url"])
            if ukey not in cache:
                try:
                    cache[ukey] = fetch_page_text(res["url"], sx)
                except CrewError as e:
                    cache[ukey] = None
                    log_event(workspace, {"role": "harness", "tag": f"step{step['id']}.webfetch",
                                          "note": f"fetch skipped {res['url']}: {e}"})
            if cache[ukey] is None:
                block += f"\n(fetch failed or blocked: {res['url']})\n"
            else:
                block += f"\n--- PAGE TEXT: {res['url']} ---\n{cache[ukey]}\n"
                fetched.append(res["url"])
        sections.append(block)
        raw_dump.append({"query": query, "fetch_top": fetch_top,
                         "results": results, "fetched": fetched})
    webdir = crew_dir(workspace) / "web"
    webdir.mkdir(exist_ok=True)
    (webdir / f"step_{step['id']}.json").write_text(
        json.dumps(raw_dump, indent=2, ensure_ascii=False))
    return "\n".join(sections), sources


# ---------------------------------------------------------------- skills

FRONTMATTER_RE = re.compile(r"\A---\r?\n(.*?)\r?\n---\r?\n?(.*)\Z", re.S)


def parse_skill_md(path: Path):
    """Parse one SKILL.md. Returns (skill dict, warnings list); skill is None if unusable."""
    warnings = []
    try:
        text = path.read_text(errors="replace")
    except OSError as e:
        return None, [f"{path}: unreadable ({e})"]
    name, description, body = path.parent.name, "", text
    m = FRONTMATTER_RE.match(text)
    if m:
        body = m.group(2)
        for line in m.group(1).splitlines():
            kv = re.match(r"(name|description)\s*:\s*(.*)$", line.strip())
            if not kv:
                continue
            key, val = kv.group(1), kv.group(2).strip().strip("'\"")
            if val in (">", "|") or not val:
                warnings.append(f"{path}: multi-line/empty '{key}:' unsupported, "
                                "keeping first line / fallback")
                continue
            if key == "name":
                name = val
            else:
                description = val
    if not description:
        warnings.append(f"{path}: no description")
    return {"name": name, "description": description, "body": body.strip(),
            "path": str(path)}, warnings


def discover_skills(cfg, workspace: Path):
    """Scan configured skill dirs. Returns (catalog dict name->skill, warnings list).

    Later dirs win on cross-dir name collision; within one dir, first scanned wins.
    """
    scfg = cfg.get("skills", {})
    max_chars = scfg.get("max_skill_chars", 6000)
    catalog, warnings = {}, []
    for d in scfg.get("dirs", []):
        d = d.replace("{workspace}", str(workspace))
        root = Path(d).expanduser()
        if not root.is_dir():
            continue
        seen_here = set()
        for sk_md in sorted(root.glob("*/SKILL.md")):
            skill, w = parse_skill_md(sk_md)
            warnings += w
            if skill is None:
                continue
            if len(skill["body"]) > max_chars:
                warnings.append(f"{sk_md}: skipped, body {len(skill['body'])} chars "
                                f"> max_skill_chars {max_chars}")
                continue
            n = skill["name"]
            if n in seen_here:
                warnings.append(f"{sk_md}: duplicate name {n!r} within {root}, keeping first")
                continue
            if n in catalog:
                warnings.append(f"skill {n!r}: {sk_md} overrides {catalog[n]['path']}")
            seen_here.add(n)
            catalog[n] = skill
    return catalog, warnings


# ---------------------------------------------------------------- LLM clients

def _post(url, body, timeout):
    data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except (urllib.error.URLError, OSError, TimeoutError) as e:
        raise LLMError(f"call to {url} failed: {e}") from e


def call_manager(cfg, messages, max_tokens, workspace=None, tag=""):
    m = cfg["manager"]
    body = {
        "model": m["model"],
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": m.get("temperature", 0.2),
        "chat_template_kwargs": {"enable_thinking": False},
    }
    t0 = time.time()
    resp = _post(m["endpoint"], body, m["timeout"])
    choice = (resp.get("choices") or [{}])[0]
    content = (choice.get("message") or {}).get("content")
    log_event(workspace, {
        "role": "manager", "tag": tag, "duration_s": round(time.time() - t0, 1),
        "finish_reason": choice.get("finish_reason"),
        "request_chars": sum(len(x.get("content", "")) for x in messages),
        "content": content,
    })
    if not content or not content.strip():
        raise LLMError(f"manager returned empty content (tag={tag}, "
                       f"finish_reason={choice.get('finish_reason')})")
    return content


def call_intern(cfg, messages, workspace=None, tag="", force_json=True):
    it = cfg["intern"]
    body = {
        "model": it["model"],
        "messages": messages,
        "think": False,
        "stream": False,
        "options": {
            "num_predict": it.get("max_tokens", 8192),
            "num_ctx": it.get("num_ctx", 24576),
            "temperature": it.get("temperature", 0.1),
        },
    }
    if force_json:
        body["format"] = "json"
    t0 = time.time()
    resp = _post(it["endpoint"], body, it["timeout"])
    content = (resp.get("message") or {}).get("content")
    log_event(workspace, {
        "role": "intern", "tag": tag, "duration_s": round(time.time() - t0, 1),
        "done_reason": resp.get("done_reason"),
        "request_chars": sum(len(x.get("content", "")) for x in messages),
        "content": content,
    })
    if not content or not content.strip():
        raise LLMError(f"intern returned empty content (tag={tag}, "
                       f"done_reason={resp.get('done_reason')})")
    return content


# ---------------------------------------------------------------- validation

def is_safe_relpath(p):
    if not isinstance(p, str) or not p.strip():
        return False
    if p.startswith(("/", "~")):
        return False
    return ".." not in Path(p).parts


def resolve_in_workspace(workspace: Path, rel: str) -> Path:
    if not is_safe_relpath(rel):
        raise CrewError(f"unsafe path: {rel!r}")
    p = (workspace / rel).resolve()
    ws = workspace.resolve()
    if p != ws and ws not in p.parents:
        raise CrewError(f"path escapes workspace: {rel!r}")
    return p


def validate_check_command(cmd, allowed):
    """Return an error string, or None if the command is acceptable."""
    if not isinstance(cmd, str) or not cmd.strip():
        return "empty command"
    try:
        toks = shlex.split(cmd)
    except ValueError as e:
        return f"unparseable command: {e}"
    if not toks:
        return "empty command"
    if toks[0] not in allowed:
        return f"binary {toks[0]!r} not in allowlist {sorted(allowed)}"
    if toks[0] == "bash" and (len(toks) != 3 or toks[1] != "-n"):
        return "bash is allowed only as 'bash -n <file>' (syntax check)"
    for t in toks:
        if t in SHELL_OPERATOR_TOKENS:
            return f"shell operator {t!r} unsupported (checks run without a shell)"
    for t in toks[1:]:
        if t.startswith(("/", "~")):
            return f"absolute path argument {t!r} not allowed"
        if t == ".." or t.startswith("../") or "/../" in t:
            return f"path traversal in argument {t!r}"
    return None


def validate_plan(plan, cfg, skill_catalog=None):
    """Return a list of validation errors (empty list = valid)."""
    errors = []
    allowed = set(cfg["run"]["allowed_check_binaries"])
    skill_catalog = skill_catalog or {}
    max_skills = cfg.get("skills", {}).get("max_per_step", 2)
    if not isinstance(plan.get("task"), str) or not plan["task"].strip():
        errors.append("plan.task must be a non-empty string")
    steps = plan.get("steps")
    if not isinstance(steps, list) or not steps:
        errors.append("plan.steps must be a non-empty list")
        return errors
    seen = set()
    for i, s in enumerate(steps):
        if not isinstance(s, dict):
            errors.append(f"steps[{i}] is not an object")
            continue
        sid = s.get("id")
        tag = f"step {sid if isinstance(sid, int) else i}"
        if not isinstance(sid, int) or sid in seen:
            errors.append(f"{tag}: id must be a unique integer")
        else:
            seen.add(sid)
        for key in ("title", "instructions"):
            if not isinstance(s.get(key), str) or not s[key].strip():
                errors.append(f"{tag}: {key} must be a non-empty string")
        tf = s.get("target_files")
        if not isinstance(tf, list) or not tf:
            errors.append(f"{tag}: target_files must be a non-empty list")
        else:
            for p in tf:
                if not is_safe_relpath(p):
                    errors.append(f"{tag}: unsafe target path {p!r}")
        for p in s.get("context_files", []) or []:
            if not is_safe_relpath(p):
                errors.append(f"{tag}: unsafe context path {p!r}")
        for d in s.get("depends_on", []) or []:
            if not isinstance(d, int) or d not in seen or d == sid:
                errors.append(f"{tag}: depends_on {d!r} must reference an EARLIER step id")
        if s.get("executor", "intern") not in ("intern", "manager"):
            errors.append(f"{tag}: executor must be 'intern' or 'manager'")
        wq = s.get("web_queries", []) or []
        if not isinstance(wq, list):
            errors.append(f"{tag}: web_queries must be a list")
        elif wq:
            sx = cfg.get("searxng") or {}
            if not searxng_enabled(cfg):
                errors.append(f"{tag}: web_queries used but searxng is not configured")
            elif len(wq) > sx.get("max_queries_per_step", 4):
                errors.append(f"{tag}: {len(wq)} web queries, max is "
                              f"{sx.get('max_queries_per_step', 4)}")
            else:
                fetch_total = 0
                for q in wq:
                    if not isinstance(q, dict) or not isinstance(q.get("query"), str) \
                            or not q["query"].strip():
                        errors.append(f"{tag}: each web query needs a non-empty 'query' string")
                        continue
                    ft = q.get("fetch_top", 0)
                    if not isinstance(ft, int) or ft < 0:
                        errors.append(f"{tag}: fetch_top must be an int >= 0")
                    else:
                        fetch_total += ft
                if fetch_total > sx.get("max_fetch_top", 3):
                    errors.append(f"{tag}: total fetch_top {fetch_total} exceeds "
                                  f"max_fetch_top {sx.get('max_fetch_top', 3)}")
        skills = s.get("skills", []) or []
        if not isinstance(skills, list):
            errors.append(f"{tag}: skills must be a list")
        else:
            if len(skills) > max_skills:
                errors.append(f"{tag}: {len(skills)} skills attached, max_per_step is {max_skills}")
            for n in skills:
                if n not in skill_catalog:
                    errors.append(f"{tag}: unknown skill {n!r} "
                                  f"(available: {sorted(skill_catalog) or 'none'})")
        checks = s.get("acceptance_checks")
        if not isinstance(checks, list) or not checks:
            errors.append(f"{tag}: acceptance_checks must be a non-empty list")
        else:
            for c in checks:
                err = validate_check_command(c, allowed)
                if err:
                    errors.append(f"{tag}: bad check {c!r}: {err}")
    return errors


# ---------------------------------------------------------------- prompts

MANAGER_PLAN_SYSTEM = """You are a senior manager writing an execution plan. Steps produce
FILES (code, data, markdown, config). The plan is executed step-by-step by a literal-minded
model that CANNOT ask questions, CANNOT see other steps, and rewrites ENTIRE files from
scratch.

Output ONLY a JSON object (no prose, no markdown) with this exact shape:
{
  "task": "one-line restatement of the task",
  "steps": [
    {
      "id": 1,
      "title": "short title",
      "depends_on": [],
      "executor": "intern",
      "target_files": ["relative/path.py"],
      "context_files": [],
      "skills": [],
      "web_queries": [],
      "instructions": "...",
      "acceptance_checks": ["python3 -c \\"import ast; ast.parse(open('relative/path.py').read())\\""]
    }
  ]
}

HARD REQUIREMENTS for every step:
- SELF-CONTAINED: instructions must be readable in isolation. Never reference other steps
  ("the function from step 2"). Restate names and signatures instead.
- EXACT INTERFACES: full function/class signatures with types, exact relative file paths,
  and at least one concrete input->output example per behavior.
- FULL-FILE SEMANTICS: the executor rewrites whole files. If a step modifies an existing
  file, that file MUST be listed in context_files and instructions must say to reproduce
  all unmentioned content unchanged.
- SMALL STEPS: one file (or one file plus its test file) per step; target under ~150 lines
  of output. Split anything bigger.
- NO PLACEHOLDERS: forbid TODO, stub bodies, or partial implementations.
- target_files is EXHAUSTIVE: the executor may write only those paths. Tests count.
- context_files must list every file the executor needs to see, INCLUDING files created
  by earlier steps that this step imports or modifies.
- acceptance_checks are shell commands run from the workspace root WITHOUT a shell:
  no pipes, no redirects, no ;, no &&. Allowed binaries: {allowlist}.
  Use python3 -c for syntax/import checks and python3 -m pytest <file> -q for tests.
  All paths in checks must be relative.
- skills: DEFAULT IS []. Attach a skill (max {max_skills} per step, by exact name from
  the AVAILABLE SKILLS catalog) ONLY if the step's acceptance depends on conforming to
  it. The full skill text is injected into the executor's prompt and eats its context
  budget — never attach "just in case". If no catalog is shown, always use [].
- executor: "intern" (DEFAULT — a small fast model) or "manager" (a much stronger but
  slow model). Use "manager" ONLY for steps that genuinely exceed a small model:
  multi-constraint synthesis, subtle logic, nuanced prose. Mechanical steps stay intern.
- For non-code outputs use structural checks: `grep -c <required-marker> <file>`,
  `python3 -c "import json; json.load(open('data.json'))"`, `ls <file>`.
{web_section}"""

WEB_PLAN_SECTION = """- web_queries: DEFAULT IS []. Steps MAY request web research: the harness (not you, not
  the executor) queries a search engine BEFORE the executor runs and injects results
  (title/URL/snippet, optionally fetched page text) into the executor's prompt.
  Format: [{"query": "specific search phrase", "fetch_top": 0}] — max {max_q} queries
  per step; fetch_top > 0 fetches the full text of that many top results (use only when
  snippets won't suffice; the SUM of fetch_top across one step must be <= {max_f}).
  Results eat the executor's context budget. Only add queries when the step needs
  EXTERNAL information; instruct the executor to cite the URLs it used in its output.
"""

EXECUTOR_STEP_SYSTEM = """You are a careful worker executing ONE step that produces files
(code, data, markdown, or config). Output ONLY a valid JSON object, no prose, no markdown:
{"files": [{"path": "relative/path", "content": "COMPLETE file content"}], "notes": "one line"}

Rules:
- Write the COMPLETE content of every file you emit (they replace the file entirely).
- Encode newlines as a single \\n inside the JSON string — NEVER double-escape (\\\\n).
- You may ONLY write the target files listed in the step. No other paths.
- No placeholders or TODOs; when writing code, no stub bodies. Follow the instructions exactly.
- If WEB SEARCH RESULTS are provided, base factual claims on them and cite source URLs
  where the instructions ask for citations. Never invent sources.
- Your work must pass the acceptance checks shown to you.
"""

MANAGER_AUDIT_SYSTEM = """You audit an executor's completed files against the step specification.
Output ONLY a JSON object: {"verdict": "pass" or "fail", "issues": ["..."]}

Fail if the work deviates from the spec, is incomplete, contains placeholders, or is
clearly incorrect. If the spec required citations, fail work whose cited URLs are not
among the listed web sources. Minor style differences are NOT a fail. List concrete
issues; an empty issues list is expected for a pass.
"""


def workspace_listing(workspace: Path, max_entries=200):
    lines = []
    if workspace.is_dir():
        for p in sorted(workspace.rglob("*")):
            if ".crew" in p.parts or ".git" in p.parts or "__pycache__" in p.parts:
                continue
            if p.is_file():
                lines.append(f"{p.relative_to(workspace)} ({p.stat().st_size} bytes)")
            if len(lines) >= max_entries:
                lines.append("... (truncated)")
                break
    return "\n".join(lines) or "(empty workspace)"


def searxng_enabled(cfg):
    return bool((cfg.get("searxng") or {}).get("endpoint"))


def build_plan_messages(cfg, brief, listing, skill_catalog):
    allowlist = ", ".join(cfg["run"]["allowed_check_binaries"])
    max_skills = str(cfg.get("skills", {}).get("max_per_step", 2))
    sx = cfg.get("searxng") or {}
    web_section = ""
    if searxng_enabled(cfg):
        web_section = (WEB_PLAN_SECTION
                       .replace("{max_q}", str(sx.get("max_queries_per_step", 4)))
                       .replace("{max_f}", str(sx.get("max_fetch_top", 3))))
    system = (MANAGER_PLAN_SYSTEM
              .replace("{allowlist}", allowlist)
              .replace("{max_skills}", max_skills)
              .replace("{web_section}", web_section))
    user = f"TASK BRIEF from the director:\n\n{brief}\n\n"
    if skill_catalog:
        user += "AVAILABLE SKILLS (name — description):\n" + "\n".join(
            f"- {s['name']} — {s['description'] or '(no description)'}"
            for s in skill_catalog.values()) + "\n\n"
    user += f"CURRENT WORKSPACE FILES:\n{listing}\n\nProduce the plan JSON now."
    return [{"role": "system", "content": system},
            {"role": "user", "content": user}]


def format_step_spec(step):
    return (
        f"STEP {step['id']}: {step['title']}\n\n"
        f"TARGET FILES (you may write ONLY these):\n"
        + "\n".join(f"- {p}" for p in step["target_files"])
        + "\n\nINSTRUCTIONS:\n" + step["instructions"]
        + "\n\nACCEPTANCE CHECKS (run from workspace root; your work must pass all):\n"
        + "\n".join(f"- {c}" for c in step["acceptance_checks"])
    )


def format_skill_sections(skill_parts):
    return "".join(f"\n--- SKILL: {name} (conventions you MUST follow) ---\n{body}\n"
                   for name, body in skill_parts)


def build_step_messages(step, context_parts, feedback, skill_parts=(), web_context=""):
    ctx = ""
    for rel, text in context_parts:
        ctx += f"\n--- CONTEXT FILE: {rel} ---\n{text}\n"
    user = format_step_spec(step)
    if skill_parts:
        user += "\n\nSKILLS:" + format_skill_sections(skill_parts)
    if web_context:
        user += "\n\nWEB SEARCH RESULTS (gathered for you — cite URLs from here only):\n" \
                + web_context
    if ctx:
        user += "\n\nCONTEXT FILES (current contents):" + ctx
    if feedback:
        user += ("\n\nYOUR PREVIOUS ATTEMPT FAILED. Fix it and output the full JSON again.\n"
                 f"FAILURE DETAILS:\n{feedback}")
    return [{"role": "system", "content": EXECUTOR_STEP_SYSTEM},
            {"role": "user", "content": user}]


def build_audit_messages(step, file_parts, check_results, skill_parts=(), web_sources=()):
    body = format_step_spec(step)
    if skill_parts:
        body += "\n\nSKILLS THE WORK MUST CONFORM TO:" + format_skill_sections(skill_parts)
    if web_sources:
        body += "\n\nWEB SOURCES the executor was given (citations must come from these):\n"
        body += "\n".join(f"- {t} — {u}" for t, u in web_sources)
    body += "\n\nFINAL FILE CONTENTS:\n"
    for rel, text in file_parts:
        body += f"\n--- {rel} ---\n{text}\n"
    body += "\nACCEPTANCE CHECK RESULTS:\n"
    for cmd, rc, tail in check_results:
        body += f"- exit {rc}: {cmd}\n{tail}\n"
    body += "\nAudit this work now. Output the verdict JSON."
    return [{"role": "system", "content": MANAGER_AUDIT_SYSTEM},
            {"role": "user", "content": body}]


# ---------------------------------------------------------------- run state

def state_path(workspace):
    return crew_dir(workspace) / "run_state.json"


def load_state(workspace):
    p = state_path(workspace)
    if p.is_file():
        return json.loads(p.read_text())
    return {"plan_sha": None, "steps": {}}


def save_state(workspace, state):
    state_path(workspace).write_text(json.dumps(state, indent=2))


# ---------------------------------------------------------------- execution

def run_checks(workspace, checks, timeout):
    """Run acceptance checks. Returns (all_passed, [(cmd, returncode, output_tail)])."""
    results = []
    all_ok = True
    for cmd in checks:
        toks = shlex.split(cmd)
        try:
            proc = subprocess.run(toks, cwd=workspace, capture_output=True,
                                  text=True, timeout=timeout)
            rc = proc.returncode
            out = (proc.stdout + "\n" + proc.stderr).strip()
        except subprocess.TimeoutExpired:
            rc, out = 124, f"check timed out after {timeout}s"
        except OSError as e:
            rc, out = 127, str(e)
        tail = out[-1500:] if out else "(no output)"
        results.append((cmd, rc, tail))
        if rc != 0:
            all_ok = False
    return all_ok, results


def backup_targets(workspace, step):
    """Snapshot pre-existing target files once per step, before the first write."""
    bdir = crew_dir(workspace) / "backup" / f"step_{step['id']}"
    if bdir.exists():
        return
    for rel in step["target_files"]:
        src = resolve_in_workspace(workspace, rel)
        if src.is_file():
            dst = bdir / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_bytes(src.read_bytes())
    bdir.mkdir(parents=True, exist_ok=True)


def repair_double_escapes(content):
    """Undo one level of double-escaping (small models emit \\\\n inside JSON strings).

    Only fires on the unambiguous signature: literal backslash-n sequences present but
    not a single real newline. Returns (content, repaired_flag).
    """
    if "\n" in content or "\\n" not in content:
        return content, False
    sentinel = "\x00CREW_BS\x00"
    repaired = (content
                .replace("\\\\", sentinel)
                .replace("\\r\\n", "\n").replace("\\n", "\n").replace("\\t", "\t")
                .replace('\\"', '"')
                .replace(sentinel, "\\"))
    return repaired, True


def validate_intern_output(obj, step):
    """Return (files list, None) or (None, feedback string)."""
    files = obj.get("files") if isinstance(obj, dict) else None
    if not isinstance(files, list) or not files:
        return None, 'output JSON must contain a non-empty "files" list'
    targets = set(step["target_files"])
    problems = []
    for i, f in enumerate(files):
        if not isinstance(f, dict):
            problems.append(f"files[{i}] is not an object")
            continue
        path, content = f.get("path"), f.get("content")
        if not isinstance(path, str) or not path.strip():
            problems.append(f"files[{i}].path missing or empty")
        elif path not in targets:
            problems.append(f"files[{i}].path {path!r} is NOT in target_files {sorted(targets)}")
        if not isinstance(content, str):
            problems.append(f"files[{i}].content missing or not a string (path={f.get('path')!r})")
    if problems:
        return None, "invalid output:\n" + "\n".join(f"- {p}" for p in problems)
    return files, None


def execute_step(cfg, workspace, step, no_audit=False, skill_catalog=None, web_cache=None):
    """Run one step end to end. Returns a step record dict (status DONE/FAILED/AUDIT_FAIL)."""
    rcfg = cfg["run"]
    executor = step.get("executor", "intern")
    rec = {"id": step["id"], "title": step["title"], "executor": executor,
           "status": "FAILED", "attempts": 0, "files_written": [], "check_results": [],
           "audit": None, "reason": None, "started_at": now_iso()}

    try:
        budget = rcfg["context_char_budget"]
        skill_parts, total = [], 0
        for n in step.get("skills", []) or []:
            sk = (skill_catalog or {}).get(n)
            if sk is None:
                raise CrewError(f"skill missing from catalog: {n!r} (was it deleted after plan review?)")
            total += len(sk["body"])
            skill_parts.append((n, sk["body"]))
        web_context, web_sources = gather_web_context(
            cfg, workspace, step, web_cache if web_cache is not None else {})
        total += len(web_context)
        context_parts = []
        for rel in step.get("context_files", []) or []:
            p = resolve_in_workspace(workspace, rel)
            if not p.is_file():
                raise CrewError(f"context file missing: {rel} (a dependency step may have failed)")
            text = p.read_text(errors="replace")
            total += len(text)
            context_parts.append((rel, text))
        if total > budget:
            raise CrewError(f"context budget exceeded (skills + web + context files): "
                            f"{total} chars > {budget}")
    except CrewError as e:
        rec["reason"] = str(e)
        return rec

    max_attempts = 1 + rcfg["max_step_retries"]
    transport_left = rcfg.get("max_transport_retries", 2)
    feedback = None
    passed = False
    for attempt in range(1, max_attempts + 1):
        rec["attempts"] = attempt
        if feedback:
            log_event(workspace, {"role": "harness",
                                  "tag": f"step{step['id']}.feedback{attempt - 1}",
                                  "content": feedback})
        messages = build_step_messages(step, context_parts, feedback, skill_parts,
                                       web_context)
        tag = f"step{step['id']}.attempt{attempt}"
        raw = None
        while raw is None:
            # transport failures (timeout, connection) get their own retry budget —
            # the model never saw anything, so burning an acceptance attempt on them
            # only shortens the real feedback loop
            try:
                if executor == "manager":
                    raw = call_manager(cfg, messages,
                                       cfg["manager"].get("execute_max_tokens", 8192),
                                       workspace, tag=tag)
                else:
                    raw = call_intern(cfg, messages, workspace, tag=tag)
            except LLMError as e:
                if transport_left <= 0:
                    rec["reason"] = f"transport failures exhausted retries: {e}"
                    return rec
                transport_left -= 1
                log_event(workspace, {"role": "harness", "tag": f"{tag}.transport_retry",
                                      "note": str(e)})
        try:
            obj = extract_json(raw)
        except (CrewError, json.JSONDecodeError) as e:
            feedback = f"your output was not valid JSON ({e}); output ONLY the JSON object"
            continue
        files, err = validate_intern_output(obj, step)
        if err:
            feedback = err
            continue

        backup_targets(workspace, step)
        written = []
        for f in files:
            dst = resolve_in_workspace(workspace, f["path"])
            dst.parent.mkdir(parents=True, exist_ok=True)
            content, repaired = repair_double_escapes(f["content"])
            if repaired:
                log_event(workspace, {"role": "harness", "tag": f"step{step['id']}.repair",
                                      "note": f"un-double-escaped {f['path']}"})
            dst.write_text(content)
            written.append(f["path"])
        rec["files_written"] = written

        ok, results = run_checks(workspace, step["acceptance_checks"], rcfg["check_timeout"])
        rec["check_results"] = [{"cmd": c, "rc": rc, "tail": t} for c, rc, t in results]
        if ok:
            passed = True
            break
        feedback = "acceptance checks failed:\n" + "\n".join(
            f"$ {c}\n(exit {rc})\n{t}" for c, rc, t in results if rc != 0)

    if not passed:
        rec["reason"] = f"exhausted {max_attempts} attempts; last feedback:\n{feedback}"
        return rec

    if no_audit:
        rec["status"] = "DONE"
        rec["audit"] = {"verdict": "skipped", "issues": []}
        return rec
    if executor == "manager":
        # a manager self-audit of its own fresh output is near-worthless; the
        # director reviews manager-executed steps instead (flagged in the report)
        rec["status"] = "DONE"
        rec["audit"] = {"verdict": "self_skipped", "issues": []}
        return rec

    rec["audit"] = audit_step(cfg, workspace, step,
                              [(c["cmd"], c["rc"], c["tail"]) for c in rec["check_results"]],
                              skill_parts, web_sources)
    rec["status"] = "DONE" if rec["audit"]["verdict"] == "pass" else "AUDIT_FAIL"
    if rec["status"] == "AUDIT_FAIL":
        rec["reason"] = "manager audit failed: " + "; ".join(rec["audit"]["issues"])
    return rec


def audit_step(cfg, workspace, step, check_results, skill_parts=(), web_sources=None):
    """Manager audit of a step's current on-disk result. Fail-safe on any error."""
    if web_sources is None:
        web_sources = []
        wf = crew_dir(workspace) / "web" / f"step_{step['id']}.json"
        if wf.is_file():
            for q in json.loads(wf.read_text()):
                web_sources += [(r["title"], r["url"]) for r in q.get("results", [])]
    budget = cfg["run"]["audit_file_char_budget"]
    file_parts = []
    for rel in step["target_files"]:
        p = resolve_in_workspace(workspace, rel)
        text = p.read_text(errors="replace") if p.is_file() else "(FILE MISSING)"
        if len(text) > budget:
            text = text[:budget] + f"\n... (truncated at {budget} chars)"
        file_parts.append((rel, text))
    try:
        raw = call_manager(cfg, build_audit_messages(step, file_parts, check_results,
                                                     skill_parts, web_sources),
                           cfg["manager"]["audit_max_tokens"], workspace,
                           tag=f"audit.step{step['id']}")
        obj = extract_json(raw)
        verdict = obj.get("verdict")
        issues = obj.get("issues") or []
        if verdict not in ("pass", "fail") or not isinstance(issues, list):
            raise CrewError(f"malformed audit verdict: {obj!r}")
        return {"verdict": verdict, "issues": [str(i) for i in issues]}
    except (LLMError, CrewError, json.JSONDecodeError) as e:
        return {"verdict": "fail", "issues": [f"audit unparseable/unavailable (fail-safe): {e}"]}


# ---------------------------------------------------------------- skill proposals

MANAGER_PROPOSE_SYSTEM = """You review failures from a small code-writing model to decide
whether ONE reusable skill (a written convention) would prevent this class of mistake
in FUTURE, DIFFERENT tasks.

Output ONLY a JSON object:
{"proposal": {"name": "...", "description": "...", "body": "...", "rationale": "..."} }
or
{"proposal": null}

null is the EXPECTED, COMMON answer. Return null when the failure is:
- step-specific (bad or ambiguous instructions — a plan problem, not a convention gap)
- a transport/format defect the harness already handles (JSON escaping, truncation,
  empty responses, timeouts)
- already covered by an existing skill in the catalog shown to you
- not clearly generalizable beyond this exact task

If you do propose:
- name: lowercase slug, [a-z0-9_-], max 64 chars, directory-safe.
- description: single line, states when the skill applies.
- body: a model-agnostic convention/checklist the writer can follow WHILE writing a
  file — concrete rules with a short example. Not "be careful". No references to
  Claude Code tools, slash commands, or MCP. Do NOT restate the executor's JSON output
  contract or this step's instructions.
- rationale: name the observed failure AND a plausibly different future step this
  would help.
"""

SKILL_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


def slugify_skill_name(name):
    """Coerce a proposed name to a directory-safe slug; return None if hopeless."""
    if not isinstance(name, str):
        return None
    s = re.sub(r"[^a-z0-9_-]+", "-", name.strip().lower()).strip("-")[:64]
    return s if SKILL_NAME_RE.match(s) else None


def truncate_middle(text, cap):
    if len(text) <= cap:
        return text
    head, tail = int(cap * 0.6), int(cap * 0.4)
    return text[:head] + f"\n... ({len(text) - head - tail} chars omitted) ...\n" + text[-tail:]


def read_current_run_log(workspace):
    """Records from run_log.jsonl after the LAST run.start marker (all, if no marker)."""
    path = crew_dir(workspace) / "run_log.jsonl"
    if not path.is_file():
        return []
    records = []
    for line in path.read_text().splitlines():
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        if r.get("tag") == "run.start":
            records = []
            continue
        records.append(r)
    return records


def build_propose_messages(cfg, problem_steps, log_records, skill_catalog, state_recs):
    cap = cfg["skills"].get("propose_attempt_chars", 2500)
    body = "FAILURE EVIDENCE from the most recent run:\n"
    for step in problem_steps:
        sid = step["id"]
        body += f"\n=== {format_step_spec(step)}\n"
        rec = state_recs.get(str(sid)) or {}
        body += (f"\nOUTCOME: status={rec.get('status')} attempts={rec.get('attempts')}\n")
        if rec.get("reason"):
            body += f"FINAL FAILURE REASON:\n{rec['reason']}\n"
        if (rec.get("audit") or {}).get("issues"):
            body += "AUDIT ISSUES:\n" + "\n".join(f"- {i}" for i in rec["audit"]["issues"]) + "\n"
        for r in log_records:
            tag = r.get("tag") or ""
            if not tag.startswith(f"step{sid}."):
                continue
            if r.get("role") == "intern":
                body += (f"\n--- executor output ({tag}) ---\n"
                         + truncate_middle(r.get("content") or "(empty)", cap) + "\n")
            elif r.get("role") == "harness" and ".feedback" in tag:
                body += f"\n--- retry feedback ({tag}) ---\n{r.get('content')}\n"
    if skill_catalog:
        body += "\nEXISTING SKILLS (do not duplicate):\n" + "\n".join(
            f"- {s['name']} — {s['description']}" for s in skill_catalog.values()) + "\n"
    body += "\nDecide now: one generalizable skill, or null."
    return [{"role": "system", "content": MANAGER_PROPOSE_SYSTEM},
            {"role": "user", "content": body}]


def stage_proposal(workspace, proposal, max_chars):
    """Validate and write a proposal to staging. Returns (staged path, None) or (None, why)."""
    name = slugify_skill_name(proposal.get("name"))
    if name is None:
        return None, f"unusable proposed name {proposal.get('name')!r}"
    description = next(iter(str(proposal.get("description") or "").strip().splitlines()), "")
    body = str(proposal.get("body") or "").strip()
    rationale = " ".join(str(proposal.get("rationale") or "").split())
    if not body or not description or not rationale:
        return None, "proposal missing description/body/rationale"
    if len(body) > max_chars:
        return None, f"proposal body {len(body)} chars > max_skill_chars {max_chars}"
    d = crew_dir(workspace) / "skill_proposals" / name
    if d.exists():
        print(f"warning: overwriting previously staged proposal {name!r}", file=sys.stderr)
        shutil.rmtree(d)
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\nrationale: {rationale}\n---\n{body}\n")
    return d / "SKILL.md", None


def propose_skill_from_failures(cfg, workspace, problem_steps, state_recs, skill_catalog):
    """One manager call -> at most one staged proposal. Returns (name, rationale) or None.

    Never raises: a proposal failure must not change the run outcome.
    """
    try:
        log_records = read_current_run_log(workspace)
        raw = call_manager(cfg, build_propose_messages(cfg, problem_steps, log_records,
                                                       skill_catalog, state_recs),
                           cfg["skills"].get("propose_max_tokens", 4096),
                           workspace, tag="propose_skill")
        obj = extract_json(raw)
        proposal = obj.get("proposal") if isinstance(obj, dict) else None
        if not isinstance(proposal, dict):
            log_event(workspace, {"role": "harness", "tag": "propose_skill.null",
                                  "note": "manager returned null proposal"})
            return None
        path, err = stage_proposal(workspace, proposal,
                                   cfg["skills"].get("max_skill_chars", 6000))
        if err:
            print(f"skill proposal discarded: {err}", file=sys.stderr)
            return None
        return slugify_skill_name(proposal["name"]), proposal.get("rationale", "")
    except (LLMError, CrewError, json.JSONDecodeError) as e:
        print(f"skill proposal failed (run outcome unaffected): {e}", file=sys.stderr)
        return None


# ---------------------------------------------------------------- reporting

def write_report(workspace, plan, plan_hash, records, aborted, proposal=None):
    lines = [
        "# LocalCrew run report",
        f"- task: {plan['task']}",
        f"- plan sha256: `{plan_hash}`",
        f"- finished: {now_iso()}",
        f"- outcome: {'ABORTED (fail-fast)' if aborted else 'completed'}",
        "",
        "| step | title | status | attempts | files written |",
        "|------|-------|--------|----------|---------------|",
    ]
    for r in records:
        lines.append(f"| {r['id']} | {r['title']} | **{r['status']}** | "
                     f"{r['attempts']} | {', '.join(r['files_written']) or '-'} |")
    manager_steps = [r for r in records if r.get("executor") == "manager"]
    if manager_steps:
        lines += ["", "**Manager-executed steps (no self-audit ran — director must review "
                  "these files):** " + ", ".join(f"step {r['id']}" for r in manager_steps)]
    for r in records:
        if r["status"] == "DONE" and not (r["audit"] or {}).get("issues"):
            continue
        lines += ["", f"## step {r['id']} — {r['status']}"]
        if r.get("reason"):
            lines.append(f"reason: {r['reason']}")
        if r.get("audit") and r["audit"]["issues"]:
            lines.append("audit issues:")
            lines += [f"- {i}" for i in r["audit"]["issues"]]
        for c in r.get("check_results", []):
            if c["rc"] != 0:
                lines += [f"failed check `{c['cmd']}` (exit {c['rc']}):",
                          "```", c["tail"], "```"]
    if proposal:
        name, rationale = proposal
        lines += ["", "## skill proposals (staged, awaiting director review)",
                  f"- **{name}** — {rationale}",
                  f"  - review:  `cat .crew/skill_proposals/{name}/SKILL.md`",
                  f"  - approve: `python3 crew.py approve-skill --workspace . --name {name}`",
                  f"  - reject:  `python3 crew.py reject-skill --workspace . --name {name}`"]
    path = crew_dir(workspace) / "run_report.md"
    path.write_text("\n".join(lines) + "\n")
    return path


# ---------------------------------------------------------------- ledger

def ledger_path(cfg):
    p = cfg.get("run", {}).get("ledger", "")
    return Path(p).expanduser() if p else None


def append_ledger(cfg, record):
    """Append one run record to the global ledger. Never fails the run."""
    path = ledger_path(cfg)
    if path is None:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError as e:
        print(f"warning: ledger write failed: {e}", file=sys.stderr)


def cmd_stats(args, cfg):
    path = ledger_path(cfg)
    if path is None or not path.is_file():
        print(f"no ledger yet ({path or 'ledger disabled in config'})")
        return 0
    runs = []
    for line in path.read_text().splitlines():
        try:
            runs.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    if not runs:
        print("ledger is empty")
        return 0
    completed = sum(1 for r in runs if r["outcome"] == "completed")
    steps = [s for r in runs for s in r["steps"]]
    done = [s for s in steps if s["status"] == "DONE"]
    first_try = [s for s in done if s.get("attempts") == 1]
    print(f"runs: {len(runs)} total, {completed} completed, "
          f"{len(runs) - completed} aborted "
          f"({100 * completed // len(runs)}% run success)")
    if steps:
        print(f"steps: {len(steps)} total, {len(done)} DONE "
              f"({100 * len(done) // len(steps)}%), "
              f"{len(first_try)} first-attempt ({100 * len(first_try) // max(len(done), 1)}% of DONE)")
    by_exec = {}
    for s in steps:
        e = s.get("executor", "intern")
        by_exec.setdefault(e, [0, 0])[1] += 1
        if s["status"] == "DONE":
            by_exec[e][0] += 1
    for e, (d, t) in sorted(by_exec.items()):
        print(f"  {e}: {d}/{t} steps DONE")
    print()
    for r in runs[-args.last:]:
        marks = " ".join(f"{s['id']}:{s['status']}({s['attempts']})" for s in r["steps"])
        flags = " [backfilled]" if r.get("backfilled") else ""
        print(f"{r['ts'][:16]}  {r['outcome']:9}  {Path(r['workspace']).name:20}  "
              f"{marks}{flags}")
        print(f"                 {r['task'][:90]}")
    return 0


# ---------------------------------------------------------------- commands

def cmd_health(args, cfg):
    ok = True
    targets = [
        ("manager", lambda: call_manager(
            cfg, [{"role": "user", "content": "Reply with exactly: OK"}], 16, tag="health")),
        ("intern", lambda: call_intern(
            cfg, [{"role": "user", "content": "Reply with exactly: OK"}],
            tag="health", force_json=False)),
    ]
    if searxng_enabled(cfg):
        def _sx_check():
            if not searxng_search(cfg["searxng"], "test"):
                raise LLMError("searxng returned zero results for a trivial query")
        targets.append(("searxng", _sx_check))
    for name, fn in targets:
        t0 = time.time()
        try:
            fn()
            print(f"{name}: OK ({time.time() - t0:.1f}s)")
        except (LLMError, CrewError) as e:
            print(f"{name}: FAIL — {e}")
            ok = False
    return 0 if ok else 1


def cmd_plan(args, cfg):
    workspace = Path(args.workspace)
    workspace.mkdir(parents=True, exist_ok=True)
    brief = Path(args.brief).read_text()
    listing = workspace_listing(workspace)
    catalog, warnings = discover_skills(cfg, workspace)
    for w in warnings:
        print(f"skill warning: {w}", file=sys.stderr)
    messages = build_plan_messages(cfg, brief, listing, catalog)

    plan, errors = None, ["(not attempted)"]
    for attempt in (1, 2):
        raw = call_manager(cfg, messages, cfg["manager"]["plan_max_tokens"],
                           workspace, tag=f"plan.attempt{attempt}")
        try:
            plan = extract_json(raw)
            errors = validate_plan(plan, cfg, catalog)
        except (CrewError, json.JSONDecodeError) as e:
            plan, errors = None, [f"output was not valid JSON: {e}"]
        if not errors:
            break
        messages = messages[:2] + [
            {"role": "assistant", "content": raw},
            {"role": "user", "content":
                "Your plan was rejected by the validator. Fix ALL of these problems and "
                "output the corrected full plan JSON (nothing else):\n"
                + "\n".join(f"- {e}" for e in errors)},
        ]
    if errors:
        print("PLAN REJECTED after retry. Validator errors:", file=sys.stderr)
        for e in errors:
            print(f"  - {e}", file=sys.stderr)
        rej = crew_dir(workspace) / "rejected_plan.json"
        rej.write_text(json.dumps(plan, indent=2) if plan is not None else "(unparseable)")
        print(f"raw candidate saved to {rej}", file=sys.stderr)
        return 1

    out = Path(args.out) if args.out else workspace / "plan.json"
    out.write_text(json.dumps(plan, indent=2, ensure_ascii=False) + "\n")
    sha = file_sha256(out)
    print(f"PLAN OK -> {out}")
    print(f"plan sha256: {sha}")
    print(f"task: {plan['task']}")
    for s in plan["steps"]:
        print(f"  step {s['id']}: {s['title']} [{s.get('executor', 'intern')}]")
        print(f"    targets: {', '.join(s['target_files'])}")
        if s.get("context_files"):
            print(f"    context: {', '.join(s['context_files'])}")
        if s.get("skills"):
            print("    skills:  " + ", ".join(
                f"{n} ({len(catalog[n]['body'])} chars)" for n in s["skills"]))
        for q in s.get("web_queries", []) or []:
            print(f"    web:     \"{q['query']}\" (fetch_top={q.get('fetch_top', 0)})")
        print(f"    checks:  {len(s['acceptance_checks'])}")
    print("\nDirector: review the full plan.json before running "
          "(instructions quality decides everything).")
    return 0


def cmd_run(args, cfg):
    workspace = Path(args.workspace)
    if not workspace.is_dir():
        print(f"workspace does not exist: {workspace}", file=sys.stderr)
        return 1
    plan_path = Path(args.plan) if args.plan else workspace / "plan.json"
    plan = json.loads(plan_path.read_text())
    catalog, warnings = discover_skills(cfg, workspace)
    for w in warnings:
        print(f"skill warning: {w}", file=sys.stderr)
    errors = validate_plan(plan, cfg, catalog)
    if errors:
        print("plan fails validation; refusing to run:", file=sys.stderr)
        for e in errors:
            print(f"  - {e}", file=sys.stderr)
        return 1
    plan_hash = file_sha256(plan_path)

    steps = sorted(plan["steps"], key=lambda s: s["id"])
    state = load_state(workspace)
    if args.resume:
        if state.get("plan_sha") and state["plan_sha"] != plan_hash:
            print("refusing --resume: plan.json changed since the recorded run "
                  f"(state sha {state['plan_sha'][:12]}… != {plan_hash[:12]}…)", file=sys.stderr)
            return 1
    else:
        state = {"plan_sha": plan_hash, "steps": {}}
    state["plan_sha"] = plan_hash

    def done(sid):
        return state["steps"].get(str(sid), {}).get("status") == "DONE"

    if args.step is not None:
        selected = [s for s in steps if s["id"] == args.step]
        if not selected:
            print(f"no step with id {args.step}", file=sys.stderr)
            return 1
        for d in selected[0].get("depends_on", []) or []:
            if not done(d):
                print(f"WARNING: step {args.step} depends on step {d}, which is not DONE.")
    else:
        selected = [s for s in steps if not (args.resume and done(s["id"]))]

    if args.dry_run:
        print(f"DRY RUN — plan sha256 {plan_hash}")
        for s in selected:
            print(f"step {s['id']}: {s['title']}")
            print(f"  executor: {s.get('executor', 'intern')}")
            print(f"  targets: {', '.join(s['target_files'])}")
            print(f"  context: {', '.join(s.get('context_files') or []) or '-'}")
            print(f"  skills:  {', '.join(s.get('skills') or []) or '-'}")
            for q in s.get("web_queries", []) or []:
                print(f"  web:     \"{q['query']}\" (fetch_top={q.get('fetch_top', 0)})")
            for c in s["acceptance_checks"]:
                print(f"  check: {c}")
        return 0

    log_event(workspace, {"role": "harness", "tag": "run.start", "plan_sha": plan_hash})
    records, aborted = [], False
    web_cache = {}
    for s in selected:
        print(f"[{now_iso()}] step {s['id']}: {s['title']} "
              f"[{s.get('executor', 'intern')}] ...", flush=True)
        rec = execute_step(cfg, workspace, s, no_audit=args.no_audit,
                           skill_catalog=catalog, web_cache=web_cache)
        records.append(rec)
        state["steps"][str(s["id"])] = rec
        save_state(workspace, state)
        print(f"  -> {rec['status']} (attempts={rec['attempts']})", flush=True)
        if rec["status"] != "DONE":
            aborted = True
            break

    proposal = None
    problem_ids = {r["id"] for r in records
                   if (r["attempts"] > 1 or r["status"] != "DONE")
                   and r.get("executor", "intern") == "intern"}
    if problem_ids and cfg.get("skills", {}).get("auto_propose", True):
        problem_steps = [s for s in steps if s["id"] in problem_ids]
        state_recs = {str(r["id"]): r for r in records}
        proposal = propose_skill_from_failures(cfg, workspace, problem_steps,
                                               state_recs, catalog)
        if proposal:
            print(f"skill proposal staged: {proposal[0]} — "
                  f"cat {crew_dir(workspace) / 'skill_proposals' / proposal[0] / 'SKILL.md'}")

    append_ledger(cfg, {
        "ts": now_iso(), "workspace": str(workspace.resolve()),
        "task": plan["task"], "plan_sha": plan_hash,
        "outcome": "aborted" if aborted else "completed",
        "steps": [{"id": r["id"], "status": r["status"], "attempts": r["attempts"],
                   "executor": r.get("executor", "intern")} for r in records],
        "skill_proposal": proposal[0] if proposal else None,
    })
    report = write_report(workspace, plan, plan_hash, records, aborted, proposal)
    print(f"\nreport: {report}")
    print(f"log:    {crew_dir(workspace) / 'run_log.jsonl'}")
    if aborted:
        print("RUN ABORTED — escalate to director (see report).")
    return 1 if aborted else 0


def cmd_audit(args, cfg):
    workspace = Path(args.workspace)
    plan_path = Path(args.plan) if args.plan else workspace / "plan.json"
    plan = json.loads(plan_path.read_text())
    step = next((s for s in plan["steps"] if s["id"] == args.step), None)
    if step is None:
        print(f"no step with id {args.step}", file=sys.stderr)
        return 1
    catalog, _ = discover_skills(cfg, workspace)
    skill_parts = [(n, catalog[n]["body"]) for n in step.get("skills", []) or []
                   if n in catalog]
    ok, results = run_checks(workspace, step["acceptance_checks"], cfg["run"]["check_timeout"])
    for cmd, rc, tail in results:
        print(f"check exit {rc}: {cmd}")
        if rc != 0:
            print(tail)
    audit = audit_step(cfg, workspace, step, results, skill_parts)
    if step.get("executor") == "manager":
        print("note: auditor model == executor model for this step; weigh the verdict accordingly")
    print(f"\naudit verdict: {audit['verdict']}")
    for i in audit["issues"]:
        print(f"  - {i}")
    return 0 if audit["verdict"] == "pass" and ok else 1


def cmd_propose_skill(args, cfg):
    workspace = Path(args.workspace)
    plan_path = Path(args.plan) if args.plan else workspace / "plan.json"
    plan = json.loads(plan_path.read_text())
    step = next((s for s in plan["steps"] if s["id"] == args.step), None)
    if step is None:
        print(f"no step with id {args.step}", file=sys.stderr)
        return 1
    catalog, _ = discover_skills(cfg, workspace)
    state = load_state(workspace)
    proposal = propose_skill_from_failures(cfg, workspace, [step], state["steps"], catalog)
    if proposal is None:
        print("no skill proposed (manager returned null, or proposal was unusable)")
        return 0
    name, rationale = proposal
    print(f"skill proposal staged: {name}")
    print(f"rationale: {rationale}")
    print(f"review:  cat {crew_dir(workspace) / 'skill_proposals' / name / 'SKILL.md'}")
    print(f"approve: python3 crew.py approve-skill --workspace {workspace} --name {name}")
    print(f"reject:  python3 crew.py reject-skill --workspace {workspace} --name {name}")
    return 0


def cmd_approve_skill(args, cfg):
    workspace = Path(args.workspace)
    src_dir = crew_dir(workspace) / "skill_proposals" / args.name
    src = src_dir / "SKILL.md"
    if not src.is_file():
        print(f"no staged proposal named {args.name!r}", file=sys.stderr)
        return 1
    skill, warnings = parse_skill_md(src)
    for w in warnings:
        print(f"warning: {w}", file=sys.stderr)
    max_chars = cfg.get("skills", {}).get("max_skill_chars", 6000)
    if skill is None or not skill["body"] or len(skill["body"]) > max_chars:
        print("staged proposal fails validation; not promoting", file=sys.stderr)
        return 1
    dest = workspace / ".claude" / "skills" / args.name
    if dest.exists():
        if not args.force:
            print(f"live skill {args.name!r} already exists; use --force to replace",
                  file=sys.stderr)
            return 1
        shutil.rmtree(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src_dir), str(dest))
    print(f"approved: {dest / 'SKILL.md'}")
    catalog, _ = discover_skills(cfg, workspace)
    print("now in catalog" if args.name in catalog else
          "WARNING: promoted but not discovered — check skills.dirs config")
    if args.attach:
        return attach_skill_to_plan(cfg, workspace, args, catalog)
    return 0


def attach_skill_to_plan(cfg, workspace, args, catalog):
    """Attach a just-approved skill to plan steps and re-sync the resume hash.

    The plan sha changes by design here — this IS a director-authorized plan edit,
    so run_state's recorded sha is updated too; --resume keeps working.
    """
    if args.name not in catalog:
        print("cannot --attach: skill not discoverable", file=sys.stderr)
        return 1
    plan_path = Path(args.plan) if args.plan else workspace / "plan.json"
    if not plan_path.is_file():
        print(f"cannot --attach: no plan at {plan_path}", file=sys.stderr)
        return 1
    plan = json.loads(plan_path.read_text())
    max_per_step = cfg.get("skills", {}).get("max_per_step", 2)
    by_id = {s["id"]: s for s in plan["steps"]}
    for sid in args.attach:
        step = by_id.get(sid)
        if step is None:
            print(f"cannot --attach: no step with id {sid}", file=sys.stderr)
            return 1
        skills = step.setdefault("skills", [])
        if args.name in skills:
            print(f"step {sid}: already attached")
            continue
        if len(skills) >= max_per_step:
            print(f"cannot --attach to step {sid}: already has {len(skills)} skills "
                  f"(max_per_step={max_per_step})", file=sys.stderr)
            return 1
        skills.append(args.name)
        print(f"step {sid}: attached {args.name}")
    plan_path.write_text(json.dumps(plan, indent=2, ensure_ascii=False) + "\n")
    new_sha = file_sha256(plan_path)
    state_p = state_path(workspace)
    if state_p.is_file():
        state = json.loads(state_p.read_text())
        state["plan_sha"] = new_sha
        state_p.write_text(json.dumps(state, indent=2))
        print(f"plan sha updated to {new_sha[:12]}… (run_state re-synced; --resume will work)")
    else:
        print(f"plan sha now {new_sha[:12]}…")
    print(f"next: python3 crew.py run --workspace {workspace} --resume")
    return 0


def cmd_reject_skill(args, cfg):
    d = crew_dir(Path(args.workspace)) / "skill_proposals" / args.name
    if not d.is_dir():
        print(f"no staged proposal named {args.name!r}", file=sys.stderr)
        return 1
    shutil.rmtree(d)
    print(f"rejected and removed: {args.name}")
    return 0


def cmd_skills(args, cfg):
    workspace = Path(args.workspace) if args.workspace else Path.cwd()
    catalog, warnings = discover_skills(cfg, workspace)
    for w in warnings:
        print(f"warning: {w}", file=sys.stderr)
    if not catalog:
        dirs = [d.replace("{workspace}", str(workspace))
                for d in cfg.get("skills", {}).get("dirs", [])]
        print(f"no skills found (searched: {', '.join(dirs) or 'no dirs configured'})")
        return 0
    for s in catalog.values():
        print(f"{s['name']}  ({len(s['body'])} chars)  [{s['path']}]")
        print(f"    {s['description'] or '(no description)'}")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(prog="crew.py", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default=str(DEFAULT_CONFIG))
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("health", help="verify both LLM endpoints respond")

    p = sub.add_parser("stats", help="usage + success-rate summary from the global ledger")
    p.add_argument("--last", type=int, default=10, help="show the last N runs (default 10)")

    p = sub.add_parser("skills", help="list skills discoverable by the crew")
    p.add_argument("--workspace")

    p = sub.add_parser("plan", help="manager expands a brief into plan.json")
    p.add_argument("--brief", required=True)
    p.add_argument("--workspace", required=True)
    p.add_argument("--out")

    p = sub.add_parser("run", help="execute plan steps (intern) with checks and audits")
    p.add_argument("--plan")
    p.add_argument("--workspace", required=True)
    p.add_argument("--step", type=int)
    p.add_argument("--resume", action="store_true")
    p.add_argument("--no-audit", action="store_true")
    p.add_argument("--dry-run", action="store_true")

    p = sub.add_parser("audit", help="re-run checks + manager audit for one step")
    p.add_argument("--plan")
    p.add_argument("--workspace", required=True)
    p.add_argument("--step", type=int, required=True)

    p = sub.add_parser("propose-skill", help="manager distills a failed step into a staged skill")
    p.add_argument("--plan")
    p.add_argument("--workspace", required=True)
    p.add_argument("--step", type=int, required=True)

    p = sub.add_parser("approve-skill", help="promote a staged proposal to .claude/skills")
    p.add_argument("--workspace", required=True)
    p.add_argument("--name", required=True)
    p.add_argument("--force", action="store_true")
    p.add_argument("--attach", type=int, action="append",
                   help="also attach the skill to this plan step (repeatable); "
                        "updates plan.json and re-syncs run_state for --resume")
    p.add_argument("--plan", help="plan path for --attach (default: <workspace>/plan.json)")

    p = sub.add_parser("reject-skill", help="delete a staged skill proposal")
    p.add_argument("--workspace", required=True)
    p.add_argument("--name", required=True)

    args = ap.parse_args(argv)
    cfg = load_config(args.config)
    try:
        return {"health": cmd_health, "plan": cmd_plan, "run": cmd_run,
                "audit": cmd_audit, "skills": cmd_skills, "stats": cmd_stats,
                "propose-skill": cmd_propose_skill,
                "approve-skill": cmd_approve_skill,
                "reject-skill": cmd_reject_skill}[args.cmd](args, cfg)
    except CrewError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
