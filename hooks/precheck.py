#!/usr/bin/env python3
"""LocalCrew intake pre-check — Claude Code UserPromptSubmit hook.

Fires before Claude sees the user's prompt. If the prompt matches
delegable-work signals (bulk mechanical coding, map-reduce extraction,
research briefs), inject ONE advisory line into Claude's context so the
delegation decision test runs BEFORE any planning momentum builds.

Contract (Claude Code UserPromptSubmit):
  stdin  — JSON: {"prompt": ..., "session_id": ..., "cwd": ...}
  stdout — plain text is injected as context (exit 0)
  exit 2 — would BLOCK the prompt: this script must never do that.

Fail-safe: any error, missing config, bad stdin => exit 0, no stdout, no
stderr. A broken pre-check must never block or pollute a prompt. Because
that hides breakage, `--selftest` runs canned prompts and prints verdicts.

Advisory-only by design: heuristics (and the optional 8B escalation) will
have false positives — the director always decides.

Note: the escalation rubric below duplicates the three-part decision test
kept in the operator's delegation rule; if that rule changes, update RUBRIC.

Register in ~/.claude/settings.json under hooks.UserPromptSubmit as
  {"type": "command", "command": "python3 <repo>/hooks/precheck.py"}
"""

import json
import os
import re
import sys

DEFAULTS = {
    "enabled": True,
    "min_prompt_chars": 40,
    "cjk_min_prompt_chars": 10,
    "scan_chars": 2000,
    "llm_escalation": False,
    "llm_timeout": 5,
    "advisory_hint": (
        "Before planning or writing code, run the LocalCrew delegation "
        "decision test (machine-checkable acceptance + mechanical/bounded "
        "synthesis + voluminous) and record the outcome."
    ),
}

# --- signal patterns ---------------------------------------------------
# Fire iff (TASK and BULK) or STRONG. Conjunctive on purpose: an advisory
# that fires on every "rename this variable" trains the director to ignore it.
# Japanese patterns carry no \b (word boundaries don't apply to CJK).

BULK = [
    ("all-files", r"\ball\s+(?:the\s+)?(?:\w+\s+)?(?:files|modules|tests|functions|classes|configs|scripts|docs|documents|pages|entries|records)\b"),
    ("every-file", r"\b(?:every|each)\s+(?:\w+\s+)?(?:file|module|test|function|class|config|script|doc|document|page|entry|record)s?\b"),
    ("across-files", r"\bacross\b[^.\n]{0,40}\b(?:files|modules|codebase|repo|project)\b"),
    ("throughout", r"\bthroughout\b"),
    ("bulk", r"\bbulk\b"),
    ("ja-bulk", r"全ファイル|すべての|全ての|各ファイル|一括"),
]

TASK = [
    ("refactor", r"\brefactor\w*"),
    ("rename", r"\brenam(?:e|ing)\b"),
    ("extract", r"\bextract\w*"),
    ("classify", r"\bclassif\w*"),
    ("convert", r"\bconvert\w*"),
    ("migrate", r"\bmigrat\w*"),
    ("gen-tests", r"\b(?:generate|write|add)\s+(?:unit\s+)?tests?\b"),
    ("ja-task", r"リファクタ|テスト(?:を)?(?:生成|作成|追加)|変換|移行|抽出|分類"),
]

STRONG = [
    ("research-brief", r"\bresearch\b[^.\n]{0,80}\b(?:summar\w*|brief|report)\b"),
    ("boilerplate", r"\bboilerplate\b"),
    ("test-suite", r"\btest\s+suite\b"),
    ("map-reduce", r"\bmap-?reduce\b"),
]

NEGATION = re.compile(r"(?:don'?t|do\s+not|no\s+need(?:\s+to)?|without|never)[\s\w]{0,16}$", re.I)

CJK = re.compile(r"[぀-ヿ一-鿿ｦ-ﾟ]")

RUBRIC = (
    "You are a strict classifier. Given a user prompt for a coding assistant, "
    "answer whether the task is DELEGABLE to a small local LLM crew. Delegable "
    "means ALL THREE hold: (1) acceptance is machine-checkable (tests, syntax, "
    "structural checks); (2) the work is mechanical or a bounded synthesis - no "
    "open-ended design judgment, no debugging, no exploration; (3) it is "
    "voluminous - a plan would be much shorter than the output. "
    'Reply ONLY with JSON: {"delegable": true|false}'
)


def load_config():
    root = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
    cfg = {}
    for name in ("config.json", "config.local.json"):
        path = os.path.join(root, name)
        if os.path.isfile(path):
            try:
                with open(path, encoding="utf-8") as f:
                    loaded = json.load(f)
                for key, val in loaded.items():
                    if isinstance(val, dict) and isinstance(cfg.get(key), dict):
                        cfg[key].update(val)
                    else:
                        cfg[key] = val
            except (OSError, ValueError):
                continue
    pre = dict(DEFAULTS)
    pre.update(cfg.get("precheck", {}))
    return cfg, pre


def negated(text, start):
    return bool(NEGATION.search(text[max(0, start - 24):start]))


def classify(prompt, pre):
    """Return (fire: bool, matched signal names)."""
    text = prompt.strip()
    if not text or text.startswith("/"):
        return False, []
    min_chars = pre["cjk_min_prompt_chars"] if CJK.search(text) else pre["min_prompt_chars"]
    if len(text) < int(min_chars):
        return False, []
    text = text[: int(pre["scan_chars"])].lower()

    def hits(patterns, guard_negation=False):
        found = []
        for name, pat in patterns:
            m = re.search(pat, text)
            if m and not (guard_negation and negated(text, m.start())):
                found.append(name)
        return found

    strong = hits(STRONG, guard_negation=True)
    if strong:
        return True, strong
    task = hits(TASK, guard_negation=True)
    bulk = hits(BULK)
    if task and bulk:
        return True, task + bulk
    return False, []


def escalate(cfg, pre, prompt):
    """Ask the intern model yes/no. Returns True/False, or None on ANY failure
    (caller fails OPEN to the heuristic advisory)."""
    import urllib.request

    intern = cfg.get("intern", {})
    endpoint = intern.get("endpoint")
    if not endpoint:
        return None
    body = json.dumps({
        "model": intern.get("model", "qwen3.5:9b"),
        "messages": [
            {"role": "system", "content": RUBRIC},
            {"role": "user", "content": prompt[:2000]},
        ],
        "stream": False,
        "think": False,
        "format": "json",
        "options": {"temperature": 0.0, "num_predict": 64},
    }).encode("utf-8")
    try:
        req = urllib.request.Request(endpoint, data=body,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=float(pre["llm_timeout"])) as resp:
            data = json.loads(resp.read(65536).decode("utf-8", "replace"))
        verdict = json.loads(data["message"]["content"])
        return bool(verdict["delegable"])
    except Exception:
        return None


def selftest():
    _, pre = load_config()
    cases = [
        "refactor all the files under src/ to use pathlib instead of os.path please",
        "please generate tests for every module in the utils package",
        "全ファイルをリファクタして",
        "research kv-cache quantization approaches and summarize with sources",
        "what do you think about this design? should we use a queue here or not?",
        "don't refactor anything yet, just tell me what all the files under src do",
        "rename this variable to something clearer",
        "/compact",
        "convert every YAML config in configs/ to JSON",
    ]
    for c in cases:
        fire, names = classify(c, pre)
        print(f"{'FIRE ' if fire else 'skip '} {str(names or '-'):<40} {c[:60]!r}")
    print(f"config: enabled={pre['enabled']} llm_escalation={pre['llm_escalation']} "
          f"hint={pre['advisory_hint'][:60]!r}...")


def main():
    if "--selftest" in sys.argv:
        selftest()
        return
    payload = json.load(sys.stdin)
    prompt = payload.get("prompt", "")
    if not isinstance(prompt, str):
        return
    cfg, pre = load_config()
    if not pre.get("enabled", True):
        return
    fire, names = classify(prompt, pre)
    if not fire:
        return
    if pre.get("llm_escalation"):
        verdict = escalate(cfg, pre, prompt)
        if verdict is False:  # only an explicit "no" suppresses; None fails open
            return
    print("LocalCrew pre-check: this prompt matches delegable-work signals "
          f"({', '.join(names)}). {pre['advisory_hint']}")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass  # fail-safe: never block or pollute a prompt (see --selftest)
    sys.exit(0)
