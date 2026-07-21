"""
LuluAgent x Claude tool-use runner — real LLM conversations over the business tools.

Flow per question:
  user question -> Claude (system prompt + 39 tool schemas) -> Claude picks tool(s)+args
  -> we execute via tools/* (which route through sql_validator -> query_tool -> DuckDB -> Gold)
  -> tool results (structured JSON) go back to Claude -> loop until Claude answers in text.

Safety chain is untouched:
  * Claude can ONLY pick a registered tool + parameters. There is no SQL tool, no raw-query
    path, no file access. Free-form SQL is impossible by construction.
  * user_role comes from the CALLER, never from Claude — any 'user_role' in Claude's tool
    input is stripped before dispatch.
  * Restricted fields are enforced by sql_validator; finance tools degrade gracefully.

Auth: ANTHROPIC_API_KEY from the environment or credential/.env (API folder).
"""

import json
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

AGENT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(AGENT_DIR))

import anthropic                                     # noqa: E402
import yaml                                          # noqa: E402
from tools import build_tools                        # noqa: E402
from claude_tool_definitions import TOOL_DEFINITIONS, DISPATCH   # noqa: E402
from tool_usage_logger import UsageLogger            # noqa: E402

MODEL = "claude-opus-4-8"
MAX_TOKENS = 16000
MAX_TOOL_TURNS = 8                                   # hard stop for the agentic loop
TOOL_RESULT_MAX_ROWS = 15                            # keep tool payloads token-sane


def _load_api_key():
    key = os.getenv("ANTHROPIC_API_KEY")
    if key:
        return key
    envfile = AGENT_DIR.parent / "Raw Data" / "API" / "credential" / ".env"
    if envfile.exists():
        from dotenv import dotenv_values
        key = dotenv_values(envfile).get("ANTHROPIC_API_KEY")
    if not key:
        raise RuntimeError("ANTHROPIC_API_KEY not found (env var or Raw Data/API/credential/.env)")
    return key


def _build_system_prompt():
    """system_prompt.md + the semantic layer, condensed. Cached across questions."""
    sp = (AGENT_DIR / "system_prompt.md").read_text(encoding="utf-8")
    defs = yaml.safe_load(open(AGENT_DIR / "business_definitions.yaml", encoding="utf-8"))
    sem = ["\n# Semantic layer (fuzzy phrase -> canonical meaning; resolve BEFORE picking tool args)"]
    for name, t in (defs.get("status_terms") or {}).items():
        sem.append(f"- {' / '.join(t.get('phrases', [])[:6])} => {t.get('predicate', '')}")
    sem.append("\n# Tool-routing reminders")
    sem.append("- 'cannot work / 不能上岗' => find_not_eligible_workers (compliance is derived from expired certs)")
    sem.append("- 'expiring soon / 快到期' => find_expiring_tickets (default 30 days)")
    sem.append("- 'is site X ready' => site_compliance_report + find_deployable_workers")
    sem.append("- person named but no id => search_employee first, then the id-based tool")
    sem.append("- Always answer in English (Australian business English), regardless of the question's language.")
    sem.append("- A '[Signed-in user: ...]' line may precede the question. When the question says "
               "'my / me / I' (e.g. 'my hours'), resolve the person by matching that email against "
               "employee_profile.email_work — do NOT ask for an employee ID first.")
    sem.append("- Follow-ups inherit the conversation: if a person, job or date range was established "
               "in an earlier turn, KEEP USING IT ('pull all hours' right after a John Carter hours "
               "query still means John Carter). Only ask who/what when neither the history nor the "
               "signed-in user context offers a candidate.")
    sem.append("- NEVER claim records don't exist without first CALLING a tool to check. "
               "For any data question you MUST call at least one tool before answering.")
    sem.append("- 'Acme Group' / 'acme' is the WHOLE COMPANY, not a project or site filter — "
               "for schedule/roster questions about Acme Group, query the date range with NO project filter.")
    return sp + "\n".join(sem)


@dataclass
class ToolCallRecord:
    name: str
    args: dict
    ok: bool
    row_count: int
    summary: str
    confidence: str
    caveats: list = field(default_factory=list)
    blocked: bool = False
    errors: list = field(default_factory=list)


@dataclass
class AgentRunResult:
    question: str
    role: str
    tools_called: list = field(default_factory=list)     # [ToolCallRecord]
    final_answer: str = ""
    confidence: str = "Low"
    caveats: list = field(default_factory=list)
    clarification: bool = False
    role_gate: bool = False
    turns: int = 0
    duration_s: float = 0.0


class ClaudeLuluAgent:
    def __init__(self):
        self.client = anthropic.Anthropic(api_key=_load_api_key())
        self.tools = build_tools()
        self.system = [{"type": "text", "text": _build_system_prompt(),
                        "cache_control": {"type": "ephemeral"}}]
        self.logger = UsageLogger()

    # ---------------- tool execution (safety chain) ----------------
    def _execute_tool(self, name, args, user_role):
        tool_key = DISPATCH.get(name)
        if tool_key is None:
            return None, f"Unknown tool: {name}"
        args = {k: v for k, v in (args or {}).items() if k != "user_role"}   # Claude never sets role
        fn = getattr(self.tools[tool_key], name)
        result = fn(**args, user_role=user_role)                              # -> ToolResult (validated)
        return result, None

    # ---------------- the agentic loop ----------------
    def ask(self, question, user_role="default") -> AgentRunResult:
        t0 = time.time()
        run = AgentRunResult(question=question, role=user_role)
        from lulu_time import today_context
        messages = [{"role": "user", "content": today_context() + "\n\n" + question}]

        for turn in range(MAX_TOOL_TURNS):
            run.turns = turn + 1
            response = self.client.messages.create(
                model=MODEL,
                max_tokens=MAX_TOKENS,
                thinking={"type": "adaptive"},
                system=self.system,
                tools=TOOL_DEFINITIONS,
                messages=messages,
            )

            tool_uses = [b for b in response.content if b.type == "tool_use"]
            if response.stop_reason != "tool_use" or not tool_uses:
                run.final_answer = next((b.text for b in response.content if b.type == "text"), "")
                break

            messages.append({"role": "assistant", "content": response.content})
            tool_results = []
            for tu in tool_uses:
                result, err = self._execute_tool(tu.name, dict(tu.input), user_role)
                if err:
                    payload = json.dumps({"error": err})
                    rec = ToolCallRecord(tu.name, dict(tu.input), False, 0, err, "Low", errors=[err])
                else:
                    payload = result.to_json(max_rows=TOOL_RESULT_MAX_ROWS)
                    rec = ToolCallRecord(tu.name, dict(tu.input), result.ok, result.row_count,
                                         result.summary, result.confidence, result.caveats,
                                         blocked=bool(result.validator_errors),
                                         errors=result.validator_errors)
                    self.logger.log(question, DISPATCH.get(tu.name, "?"), tu.name, result.ok,
                                    result.row_count, result.confidence, result.validator_errors,
                                    0, user_role, meta=True)
                run.tools_called.append(rec)
                tool_results.append({"type": "tool_result", "tool_use_id": tu.id,
                                     "content": payload, "is_error": not rec.ok})
            messages.append({"role": "user", "content": tool_results})
        else:
            run.final_answer = "(stopped: tool-turn limit reached)"

        # ---- derive run-level signals ----
        run.duration_s = time.time() - t0
        run.role_gate = any(r.blocked or any("requires role" in e for e in r.errors)
                            for r in run.tools_called) or \
            any("require" in r.summary.lower() and "role" in r.summary.lower() for r in run.tools_called)
        run.clarification = not run.tools_called and ("?" in run.final_answer)
        seen = set()
        for r in run.tools_called:
            for c in r.caveats:
                if c not in seen:
                    seen.add(c)
                    run.caveats.append(c)
        confs = [r.confidence for r in run.tools_called if r.ok]
        if run.clarification or not run.tools_called and not run.final_answer:
            run.confidence = "Low"
        elif not confs:
            run.confidence = "Low" if run.role_gate else "Medium"
        elif all(c == "High" for c in confs):
            run.confidence = "High"
        else:
            run.confidence = "Medium"
        return run


if __name__ == "__main__":
    agent = ClaudeLuluAgent()
    q = sys.argv[1] if len(sys.argv) > 1 else "How many certificates are expired?"
    role = sys.argv[2] if len(sys.argv) > 2 else "default"
    r = agent.ask(q, role)
    print("tools:", [(t.name, t.args) for t in r.tools_called])
    print("answer:", r.final_answer)
    print("confidence:", r.confidence)
