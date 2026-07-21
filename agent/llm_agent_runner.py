"""
LuluAgent Gateway runner — the model-agnostic agent loop.

    user -> [router: pick PLANNER model] -> tool loop (canonical messages, any provider)
         -> tools -> sql_validator -> query_tool -> DuckDB -> Gold
         -> [ANSWER model polishes evidence into the final reply]
         -> reply

Swap planner/answer/fallback in admin_settings.json / model_registry.yaml — zero code change.
Business logic (tools, validator, Gold) is untouched and identical for every LLM vendor.
"""

import json
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

AGENT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(AGENT_DIR))

from tools import build_tools                                    # noqa: E402
from claude_tool_definitions import TOOL_DEFINITIONS, DISPATCH   # noqa: E402
from claude_agent_runner import _build_system_prompt             # noqa: E402 (same prompt, any brain)
from llm_provider import get_provider, load_settings             # noqa: E402
from answer_generator import AnswerGenerator                     # noqa: E402
from tool_usage_logger import UsageLogger                        # noqa: E402
from memory_manager import MemoryManager                         # noqa: E402
from conversation_trace_logger import TraceLogger                # noqa: E402

MAX_TOOL_TURNS = 8
TOOL_RESULT_MAX_ROWS = 15


@dataclass
class GatewayRunResult:
    question: str
    role: str
    planner_model: str = ""
    answer_model: str = ""          # empty if two-stage skipped
    fallback_used: bool = False
    tools_called: list = field(default_factory=list)   # [{name,args,ok,rows,summary,confidence,blocked}]
    final_answer: str = ""
    confidence: str = "Low"
    caveats: list = field(default_factory=list)
    role_gate: bool = False
    turns: int = 0
    duration_s: float = 0.0
    tokens: dict = field(default_factory=dict)   # accumulated LLM usage across turns
    trace_id: str = ""                           # conversation trace id (feedback hooks)


class LuluGatewayAgent:
    def __init__(self):
        self.tools = build_tools()
        self.system = _build_system_prompt()
        self.logger = UsageLogger()
        self.answerer = AnswerGenerator()
        self.settings = load_settings()
        self.memory = MemoryManager()
        self.trace = TraceLogger()

    # ---------------- safety chain (identical for every LLM) ----------------
    def _execute(self, name, args, user_role):
        tool_key = DISPATCH.get(name)
        if tool_key is None:
            return None, f"Unknown tool: {name}"
        args = {k: v for k, v in (args or {}).items() if k != "user_role"}
        fn = getattr(self.tools[tool_key], name)
        return fn(**args, user_role=user_role), None

    def _planner(self):
        primary = get_provider("planner")
        if primary.available():
            return primary, False
        fb = get_provider("fallback")
        if fb.available():
            return fb, True
        raise RuntimeError(f"No usable planner: '{primary.label()}' and fallback "
                           f"'{fb.label()}' both lack API keys.")

    # ---------------- the loop ----------------
    def ask(self, question, user_role="default", user="admin",
            history=None, conversation_id=None) -> GatewayRunResult:
        """history: previous turns [{question, answer}] — passed to the LLM as real
        conversation context so follow-ups ('那这周呢?') resolve naturally."""
        t0 = time.time()
        run = GatewayRunResult(question=question, role=user_role)
        self.logger.current_user = user          # stamp every usage-log row with who asked
        self._conversation_id, self._turn = conversation_id, len(history or []) + 1

        # ---- memory: learn from statements (same brain-independent behaviour as V1) ----
        kind, learned = self.memory.capture(question, user=user)
        if kind in ("site_rule", "supplier_flag", "definition", "preference", "fact"):
            run.planner_model = "memory (no LLM call needed)"
            run.final_answer = f"✅ Learned ({kind}): {learned}"
            run.confidence = "High"
            run.duration_s = time.time() - t0
            run.trace_id = self.trace.log_gateway(run, user=user, conversation_id=self._conversation_id, turn=self._turn)
            return run

        planner, run.fallback_used = self._planner()
        run.planner_model = planner.label()
        temperature = self.settings.get("temperature")

        # ---- inject TODAY (pinned Perth) + recalled memory into the user turn (system frozen = cache-safe) ----
        from lulu_time import today_context
        date_ctx = today_context()
        mem_ctx = self.memory.render_context(question)
        # Search Layer pre-pass: resolve entity mentions deterministically and TELL the
        # planner model — keeps it from dropping explicit filters like 'site=Acme Group'
        ent_ctx = ""
        try:
            from entity_resolver import resolve_in_question
            hit = resolve_in_question(question)
            if hit:
                ent_ctx = (f"[Detected entity filter: {hit['type']} = '{hit['value']}'"
                           + (f" (resolved from '{hit['raw']}')" if hit.get("raw") and hit["raw"] != hit["value"] else "")
                           + " — include this as a tool filter argument unless clearly wrong.]")
        except Exception:
            pass
        # who is asking — lets 'my hours' resolve via email_work instead of asking for an ID
        user_ctx = ""
        if user and "@" in str(user):
            user_ctx = (f"[Signed-in user: {user} — for 'my / me / I' questions, identify this person "
                        f"via employee_profile.email_work = '{user}' and use their records.]")
        content = "\n\n".join(x for x in (date_ctx, mem_ctx, ent_ctx, user_ctx, question) if x)
        messages = []
        if history:
            from conversation_context import history_to_messages
            messages.extend(history_to_messages(history))
        messages.append({"role": "user", "content": content})
        evidence = []

        for turn in range(MAX_TOOL_TURNS):
            run.turns = turn + 1
            try:
                resp = planner.chat(self.system, messages, tools=TOOL_DEFINITIONS,
                                    max_tokens=8192, temperature=temperature)
            except Exception as ex:
                fb = get_provider("fallback")
                if not run.fallback_used and fb.available():
                    planner, run.fallback_used = fb, True
                    run.planner_model = f"{run.planner_model} -> {fb.label()} (fallback)"
                    resp = planner.chat(self.system, messages, tools=TOOL_DEFINITIONS,
                                        max_tokens=8192, temperature=temperature)
                else:
                    run.final_answer = f"LLM error: {type(ex).__name__}: {ex}"
                    run.duration_s = time.time() - t0
                    run.trace_id = self.trace.log_gateway(run, user=user, conversation_id=self._conversation_id, turn=self._turn)
                    return run

            for k, v in (resp.usage or {}).items():       # accumulate token usage across turns
                if isinstance(v, (int, float)):
                    run.tokens[k] = run.tokens.get(k, 0) + v

            if resp.stop != "tool_use" or not resp.tool_calls:
                draft = resp.text
                break

            # record assistant turn (canonical + provider-native echo)
            messages.append({"role": "assistant", "content": resp.text,
                             "tool_calls": [{"id": c.id, "name": c.name, "args": c.args}
                                            for c in resp.tool_calls],
                             "_raw": {planner.name: resp.raw}})
            results = []
            for call in resp.tool_calls:
                result, err = self._execute(call.name, call.args, user_role)
                if err:
                    rec = {"name": call.name, "args": call.args, "ok": False, "rows": 0,
                           "summary": err, "confidence": "Low", "blocked": False}
                    payload, is_err = json.dumps({"error": err}), True
                else:
                    rec = {"name": call.name, "args": call.args, "ok": result.ok,
                           "rows": result.row_count, "summary": result.summary,
                           "confidence": result.confidence,
                           "blocked": bool(result.validator_errors),
                           "data": result.data}          # full rows (UI export; stripped in traces)
                    payload, is_err = result.to_json(max_rows=TOOL_RESULT_MAX_ROWS), not result.ok
                    for c in result.caveats:
                        if c not in run.caveats:
                            run.caveats.append(c)
                    evidence.append({"tool": call.name, "args": call.args,
                                     "summary": result.summary,
                                     "data_sample": result.data[:10]})
                    self.logger.log(question, DISPATCH[call.name], call.name, result.ok,
                                    result.row_count, result.confidence,
                                    result.validator_errors, 0, user_role, meta=True)
                run.tools_called.append(rec)
                results.append({"id": call.id, "content": payload, "is_error": is_err})
            messages.append({"role": "tool_results", "results": results})
        else:
            draft = "(stopped: tool-turn limit reached)"

        # ---------------- answer stage (second brain) ----------------
        final, answer_model = self.answerer.generate(question, evidence, draft, planner.label())
        run.final_answer = final
        run.answer_model = answer_model or ""

        # ---------------- run-level signals ----------------
        run.role_gate = any(r["blocked"] for r in run.tools_called) or \
            any("role" in r["summary"].lower() and "requir" in r["summary"].lower()
                for r in run.tools_called)
        confs = [r["confidence"] for r in run.tools_called if r["ok"]]
        if not run.tools_called:
            run.confidence = "Low"
        elif confs and all(c == "High" for c in confs):
            run.confidence = "High"
        else:
            run.confidence = "Medium"
        run.duration_s = time.time() - t0
        run.trace_id = self.trace.log_gateway(run, user=user, conversation_id=self._conversation_id, turn=self._turn)
        return run


if __name__ == "__main__":
    agent = LuluGatewayAgent()
    q = sys.argv[1] if len(sys.argv) > 1 else "How many certificates are expired?"
    r = agent.ask(q, sys.argv[2] if len(sys.argv) > 2 else "default")
    print("planner:", r.planner_model, "| answer:", r.answer_model or "(planner draft)")
    print("tools:", [(t["name"], t["args"]) for t in r.tools_called])
    print("answer:", r.final_answer)
