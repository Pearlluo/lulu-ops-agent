"""
conversation_trace_logger.py — persist EVERY Ask Lulu interaction as JSONL.

Chat is the best bug finder: each question/answer becomes a trace record that the
Bug Inbox classifies and regression_from_chat promotes into tests. One record per
conversation turn (tool_usage_logger stays per-tool-call; this is per-ANSWER).

File: logs/conversation_traces.jsonl
  {trace_id, ts, engine, question, user, user_role,
   detected_intent, selected_tool, function, args, resolved_terms,
   sql, result_rows, answer, confidence, caveats, validator_errors,
   needs_clarification, is_meta, tools_called, memory_used,
   latency_ms, planner_model, answer_model, tokens, cost}

Feedback events append as {type: "feedback", trace_id, user_feedback, correction_flag}
and are merged on read — so the file is append-only and safe under concurrent writers.
"""

import json
import time
import uuid
from pathlib import Path

AGENT_DIR = Path(__file__).resolve().parent
TRACE_PATH = AGENT_DIR / "logs" / "conversation_traces.jsonl"


class TraceLogger:
    def __init__(self, path=TRACE_PATH):
        self.path = Path(path)
        self.path.parent.mkdir(exist_ok=True)

    # ---------------- write ----------------
    def _append(self, rec):
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")

    def log(self, **fields):
        rec = {"type": "trace", "trace_id": uuid.uuid4().hex[:12],
               "ts": time.strftime("%Y-%m-%dT%H:%M:%S")}
        rec.update(fields)
        self._append(rec)
        return rec["trace_id"]

    def log_feedback(self, trace_id, user_feedback=None, correction_flag=False):
        """Attach feedback to an earlier trace ('wrong tool', thumbs-down text, etc.)."""
        self._append({"type": "feedback", "trace_id": trace_id, "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
                      "user_feedback": user_feedback, "correction_flag": bool(correction_flag)})

    # ---------------- engine adapters ----------------
    def log_deterministic(self, resp, t0, user="admin", user_role="default", resolved_terms=None,
                          conversation_id=None, turn=None):
        """resp = lulu_agent.AgentResponse"""
        return self.log(
            conversation_id=conversation_id, turn=turn,
            engine="deterministic", question=resp.question, user=user, user_role=user_role,
            detected_intent=resp.domain, selected_tool=resp.tool, function=resp.function,
            args=resp.args, resolved_terms=resolved_terms or {},
            sql=resp.sql, result_rows=getattr(resp, "row_count", len(resp.data)),
            answer=resp.answer, confidence=resp.confidence, caveats=resp.caveats,
            validator_errors=resp.validator_errors,
            needs_clarification=resp.answer.startswith("CLARIFICATION NEEDED"),
            is_meta=resp.is_meta,
            tools_called=[{"tool": s.get("tool"), "function": s.get("function"),
                           "ok": s.get("ok"), "rows": s.get("rows")} for s in resp.step_results],
            memory_used=resp.memory_used, latency_ms=round((time.time() - t0) * 1000, 1),
            planner_model="deterministic", answer_model="deterministic", tokens={}, cost=None)

    def log_gateway(self, run, user="admin", conversation_id=None, turn=None):
        """run = llm_agent_runner.GatewayRunResult"""
        first = run.tools_called[0] if run.tools_called else {}
        return self.log(
            conversation_id=conversation_id, turn=turn,
            engine="gateway", question=run.question, user=user, user_role=run.role,
            detected_intent="", selected_tool=first.get("name", ""), function=first.get("name", ""),
            args=first.get("args", {}), resolved_terms={},
            sql="", result_rows=sum(t.get("rows", 0) for t in run.tools_called),
            answer=run.final_answer, confidence=run.confidence, caveats=run.caveats,
            validator_errors=[t["summary"] for t in run.tools_called if t.get("blocked")],
            needs_clarification=False, is_meta=len(run.tools_called) > 1,
            tools_called=[{k: v for k, v in t.items() if k != "data"} for t in run.tools_called],
            memory_used=[],
            latency_ms=round(run.duration_s * 1000, 1),
            planner_model=run.planner_model, answer_model=run.answer_model,
            tokens=getattr(run, "tokens", {}) or {}, cost=None)

    # ---------------- read ----------------
    def read(self, last_n=None):
        """All trace records (most recent last), with feedback events merged in."""
        if not self.path.exists():
            return []
        traces, feedback = [], {}
        for line in open(self.path, encoding="utf-8"):
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("type") == "feedback":
                feedback[rec["trace_id"]] = rec
            else:
                rec.setdefault("user_feedback", None)
                rec.setdefault("correction_flag", False)
                traces.append(rec)
        for t in traces:
            fb = feedback.get(t["trace_id"])
            if fb:
                t["user_feedback"] = fb.get("user_feedback")
                t["correction_flag"] = fb.get("correction_flag", False)
        return traces[-last_n:] if last_n else traces

    def find(self, trace_id):
        for t in self.read():
            if t["trace_id"] == trace_id or t["trace_id"].startswith(trace_id):
                return t
        return None

    # ---------------- conversation persistence / resume ----------------
    def conversations(self, limit=20, exclude_test=True, user=None):
        """Recent conversations: [{conversation_id, title, turns, last_ts}], newest first.
        Built by grouping traces on conversation_id — nothing extra is stored.
        user=<email> scopes the list to that person's own conversations."""
        groups = {}
        for t in self.read():
            cid = t.get("conversation_id")
            if not cid or (exclude_test and "test" in str(cid).lower()):
                continue
            if user and t.get("user") != user:
                continue
            g = groups.setdefault(cid, {"conversation_id": cid, "title": t.get("question", "")[:60],
                                        "turns": 0, "last_ts": ""})
            g["turns"] += 1
            g["last_ts"] = max(g["last_ts"], t.get("ts", ""))
        return sorted(groups.values(), key=lambda g: g["last_ts"], reverse=True)[:limit]

    def load_conversation(self, conversation_id):
        """Rebuild a conversation for resuming: (engine_history, chat_messages).
        engine_history feeds LuluAgent/Gateway `history=`; chat_messages re-renders the UI."""
        turns = sorted((t for t in self.read() if t.get("conversation_id") == conversation_id),
                       key=lambda t: (t.get("turn") or 0, t.get("ts", "")))
        engine_history, chat = [], []
        for t in turns:
            engine_history.append({"question": t.get("question", ""), "answer": t.get("answer", ""),
                                   "tool": t.get("selected_tool", ""), "function": t.get("function", ""),
                                   "args": t.get("args") or {}})
            steps = [("Engine", t.get("engine", "")),
                     ("Tool", f"{t.get('selected_tool')}.{t.get('function')}" if t.get("selected_tool") else "—"),
                     ("Args", str(t.get("args") or "—")),
                     ("Rows", str(t.get("result_rows")))]
            chat.append({"q": t.get("question", ""), "answer": t.get("answer", ""),
                         "conf": t.get("confidence", "Low"), "caveats": t.get("caveats") or [],
                         "trace_steps": steps, "sqls": [t.get("sql")] if t.get("sql") else [],
                         "trace_id": t.get("trace_id", ""),
                         "feedback": ("down" if t.get("correction_flag")
                                      else "up" if t.get("user_feedback") else None)})
        return engine_history, chat


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    tl = TraceLogger()
    traces = tl.read()
    print(f"{len(traces)} traces in {tl.path}")
    for t in traces[-10:]:
        flag = "⚠" if t.get("correction_flag") or t.get("needs_clarification") else " "
        print(f"{flag} {t['trace_id']} [{t['engine'][:4]}] {t['question'][:40]!r} "
              f"-> {t.get('selected_tool')}.{t.get('function')} rows={t.get('result_rows')} "
              f"conf={t.get('confidence')} {t.get('latency_ms')}ms")
