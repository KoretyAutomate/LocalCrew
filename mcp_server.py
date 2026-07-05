#!/usr/bin/env python3
"""LocalCrew MCP server — thin stdio wrapper around crew.py.

Purpose: salience. Registered as an MCP server, the crew shows up in the
assistant's per-turn tool list instead of being a CLI it must remember.
Every tool is a subprocess call into crew.py in this directory; the server
adds NO logic and NO auto-approval — director gates (plan review,
independent verification) stay with the caller.

Long runs: crew_run starts the run DETACHED and returns immediately with a
log path; poll with crew_run_status. An MCP tool call must never block for
the minutes a run takes.

Transport: MCP stdio — newline-delimited JSON-RPC 2.0, stdlib only.

Register (user scope):
  claude mcp add --scope user localcrew -- python3 /path/to/LocalCrew/mcp_server.py
"""

import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.realpath(__file__))
CREW = os.path.join(HERE, "crew.py")
PROTOCOL_VERSION = "2024-11-05"

TOOLS = [
    {
        "name": "crew_stats",
        "description": "LocalCrew usage ledger: run/step success rates, per-executor breakdown, recent runs. Use to answer 'how is the crew performing?'",
        "inputSchema": {"type": "object", "properties": {
            "last": {"type": "integer", "description": "show last N runs (default 10)"}}},
    },
    {
        "name": "crew_health",
        "description": "Check that the LocalCrew endpoints (vLLM manager, Ollama intern) answer a trivial completion. Run before planning; vLLM is slow to cold-start.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "crew_skills",
        "description": "List the crew-visible skill catalog for a workspace (<ws>/.claude/skills).",
        "inputSchema": {"type": "object", "properties": {
            "workspace": {"type": "string", "description": "absolute path to the project workspace"}},
            "required": ["workspace"]},
    },
    {
        "name": "crew_plan",
        "description": "Have the LocalCrew manager expand a brief file into <workspace>/plan.json. Synchronous (typically 1-2 min). The caller MUST review plan.json before crew_run — this gate decides everything.",
        "inputSchema": {"type": "object", "properties": {
            "brief_path": {"type": "string", "description": "absolute path to the brief .md file"},
            "workspace": {"type": "string", "description": "absolute path to the project workspace"}},
            "required": ["brief_path", "workspace"]},
    },
    {
        "name": "crew_run",
        "description": "Execute the reviewed plan.json. Starts DETACHED and returns immediately with a console-log path — poll crew_run_status; do judgment work meanwhile. Never blocks. Independent verification (run the tests yourself) is still required after completion.",
        "inputSchema": {"type": "object", "properties": {
            "workspace": {"type": "string", "description": "absolute path to the project workspace"},
            "resume": {"type": "boolean", "description": "continue after a failed step"},
            "step": {"type": "integer", "description": "run one step only"},
            "env_path_prepend": {"type": "string", "description": "directory to prepend to PATH so acceptance checks resolve the project's python3/pytest (e.g. a conda env's bin dir). The check allowlist is exact-token; PATH is the sanctioned way to pick an interpreter."}},
            "required": ["workspace"]},
    },
    {
        "name": "crew_run_status",
        "description": "Progress of a crew_run: per-step status from run_state.json, console-log tail, and the run report once written.",
        "inputSchema": {"type": "object", "properties": {
            "workspace": {"type": "string", "description": "absolute path to the project workspace"}},
            "required": ["workspace"]},
    },
]


def crew_cmd(*args):
    return [sys.executable, CREW, *args]


def run_sync(cmd, timeout, env=None):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env)
    except subprocess.TimeoutExpired:
        return f"ERROR: timed out after {timeout}s: {' '.join(cmd)}", True
    out = (p.stdout or "") + (("\n[stderr]\n" + p.stderr) if p.stderr.strip() else "")
    return out.strip() or "(no output)", p.returncode != 0


def tool_crew_stats(args):
    cmd = crew_cmd("stats")
    if args.get("last"):
        cmd += ["--last", str(int(args["last"]))]
    return run_sync(cmd, 60)


def tool_crew_health(args):
    return run_sync(crew_cmd("health"), 180)


def tool_crew_skills(args):
    return run_sync(crew_cmd("skills", "--workspace", args["workspace"]), 60)


def tool_crew_plan(args):
    return run_sync(crew_cmd("plan", "--brief", args["brief_path"],
                             "--workspace", args["workspace"]), 1200)


def tool_crew_run(args):
    ws = args["workspace"]
    if not os.path.isdir(ws):
        return f"ERROR: workspace not found: {ws}", True
    cmd = crew_cmd("run", "--workspace", ws)
    if args.get("resume"):
        cmd.append("--resume")
    if args.get("step") is not None:
        cmd += ["--step", str(int(args["step"]))]
    env = os.environ.copy()
    if args.get("env_path_prepend"):
        env["PATH"] = args["env_path_prepend"] + os.pathsep + env.get("PATH", "")
    crew_dir = os.path.join(ws, ".crew")
    os.makedirs(crew_dir, exist_ok=True)
    log_path = os.path.join(crew_dir, "run_console.log")
    with open(log_path, "ab") as log:
        log.write(f"\n=== crew_run (mcp) {' '.join(cmd)} ===\n".encode())
        proc = subprocess.Popen(cmd, stdout=log, stderr=log,
                                stdin=subprocess.DEVNULL, env=env,
                                start_new_session=True)
    return (f"run started detached (pid {proc.pid}).\n"
            f"console log: {log_path}\n"
            f"poll with crew_run_status; the report lands at "
            f"{os.path.join(crew_dir, 'run_report.md')}"), False


def tool_crew_run_status(args):
    ws = args["workspace"]
    crew_dir = os.path.join(ws, ".crew")
    parts = []
    state_path = os.path.join(crew_dir, "run_state.json")
    if os.path.isfile(state_path):
        try:
            with open(state_path, encoding="utf-8") as f:
                state = json.load(f)
            steps = state.get("steps", {})
            lines = [f"  step {sid}: {s.get('status')} (attempts={s.get('attempts', '?')})"
                     for sid, s in sorted(steps.items(), key=lambda kv: str(kv[0]))]
            parts.append("run_state:\n" + "\n".join(lines))
        except (OSError, ValueError) as e:
            parts.append(f"run_state.json unreadable: {e}")
    else:
        parts.append("no run_state.json (run not started or workspace wrong)")
    log_path = os.path.join(crew_dir, "run_console.log")
    if os.path.isfile(log_path):
        with open(log_path, "rb") as f:
            f.seek(max(0, os.path.getsize(log_path) - 2000))
            tail = f.read().decode("utf-8", "replace")
        parts.append("console tail:\n" + tail)
    report = os.path.join(crew_dir, "run_report.md")
    if os.path.isfile(report):
        parts.append(f"report ready: {report} (read it fully before approving)")
    return "\n\n".join(parts), False


HANDLERS = {
    "crew_stats": tool_crew_stats,
    "crew_health": tool_crew_health,
    "crew_skills": tool_crew_skills,
    "crew_plan": tool_crew_plan,
    "crew_run": tool_crew_run,
    "crew_run_status": tool_crew_run_status,
}


def handle(req):
    method = req.get("method")
    if method == "initialize":
        return {"protocolVersion": req.get("params", {}).get("protocolVersion", PROTOCOL_VERSION),
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "localcrew", "version": "1.5.0"}}
    if method == "tools/list":
        return {"tools": TOOLS}
    if method == "tools/call":
        params = req.get("params", {})
        name = params.get("name")
        fn = HANDLERS.get(name)
        if fn is None:
            return {"content": [{"type": "text", "text": f"unknown tool: {name}"}], "isError": True}
        try:
            text, is_err = fn(params.get("arguments") or {})
        except Exception as e:  # tool errors are results, not protocol errors
            text, is_err = f"ERROR: {type(e).__name__}: {e}", True
        return {"content": [{"type": "text", "text": text}], "isError": is_err}
    if method == "ping":
        return {}
    return None  # unknown -> -32601 for requests, ignored for notifications


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except ValueError:
            continue
        is_notification = "id" not in req
        result = handle(req)
        if is_notification:
            continue
        if result is None:
            resp = {"jsonrpc": "2.0", "id": req["id"],
                    "error": {"code": -32601, "message": f"method not found: {req.get('method')}"}}
        else:
            resp = {"jsonrpc": "2.0", "id": req["id"], "result": result}
        sys.stdout.write(json.dumps(resp) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
