"""
Tool analytics for Lulu — records every ask() so we learn which tools earn their keep.

Per call we log: question, tool.function, ok, row_count, confidence, validator errors,
duration, role, whether it was part of a meta-plan — plus two quality signals filled in
retroactively from the NEXT user turn:
  followed_up : the next question hit the same tool/domain (user needed more — answer was thin)
  corrected   : the next question contained correction phrases (answer was wrong/missed intent)

Storage: append-only JSONL at agent/logs/tool_usage.jsonl (survives restarts).
report(last_n=100) aggregates the recent window per tool.function.
"""

import json
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

AGENT_DIR = Path(__file__).resolve().parent
LOG_PATH = AGENT_DIR / "logs" / "tool_usage.jsonl"

CORRECTION_PHRASES = ["wrong", "incorrect", "not what i", "i meant", "that's not", "no,",
                      "不对", "不是这个", "我是说", "错了"]


class UsageLogger:
    def __init__(self, path=LOG_PATH):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.records = []
        if self.path.exists():
            for line in self.path.read_text(encoding="utf-8").splitlines()[-1000:]:
                try:
                    self.records.append(json.loads(line))
                except Exception:
                    pass

    # ---------- write ----------
    def log(self, question, tool, function, ok, row_count, confidence,
            validator_errors=None, duration_ms=0, role="default", meta=False, user=None):
        rec = {"ts": datetime.now(timezone.utc).isoformat(), "question": question,
               "tool": tool, "function": function, "ok": bool(ok), "row_count": int(row_count or 0),
               "confidence": confidence, "validator_errors": validator_errors or [],
               "duration_ms": int(duration_ms), "role": role, "meta": bool(meta),
               # engines set .current_user once per ask() so nested helpers don't need threading
               "user": user if user is not None else getattr(self, "current_user", ""),
               "followed_up": False, "corrected": False}
        self.records.append(rec)
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        return len(self.records) - 1

    def _rewrite(self):
        with open(self.path, "w", encoding="utf-8") as fh:
            for r in self.records:
                fh.write(json.dumps(r, ensure_ascii=False) + "\n")

    # ---------- retro quality signals (called when the NEXT question arrives) ----------
    def observe_next_question(self, question, routed_tool=None):
        """Heuristics: corrections name the previous answer; follow-ups re-hit the same tool."""
        if not self.records:
            return
        prev = self.records[-1]
        ql = question.lower()
        if any(p in ql for p in CORRECTION_PHRASES):
            prev["corrected"] = True
            self._rewrite()
        elif routed_tool and routed_tool == prev.get("tool"):
            prev["followed_up"] = True
            self._rewrite()

    def mark_corrected(self):
        if self.records:
            self.records[-1]["corrected"] = True
            self._rewrite()

    # ---------- analytics ----------
    def report(self, last_n=100):
        window = self.records[-last_n:]
        agg = defaultdict(lambda: {"uses": 0, "ok": 0, "zero_rows": 0, "followed_up": 0,
                                   "corrected": 0, "high_conf": 0})
        for r in window:
            k = f"{r['tool']}.{r['function']}" if r.get("tool") else "(clarification)"
            a = agg[k]
            a["uses"] += 1
            a["ok"] += r["ok"]
            a["zero_rows"] += (r["ok"] and r["row_count"] == 0)
            a["followed_up"] += r.get("followed_up", False)
            a["corrected"] += r.get("corrected", False)
            a["high_conf"] += (r.get("confidence") == "High")
        out = []
        for k, a in agg.items():
            u = a["uses"]
            out.append({"tool_function": k, "uses": u,
                        "success_rate": round(a["ok"] / u, 2),
                        "zero_row_rate": round(a["zero_rows"] / u, 2),
                        "followup_rate": round(a["followed_up"] / u, 2),
                        "correction_rate": round(a["corrected"] / u, 2),
                        "high_confidence_rate": round(a["high_conf"] / u, 2)})
        out.sort(key=lambda x: -x["uses"])
        return {"window": len(window), "total_logged": len(self.records), "tools": out}

    def print_report(self, last_n=100):
        rep = self.report(last_n)
        print(f"\n=== TOOL ANALYTICS (last {rep['window']} of {rep['total_logged']} calls) ===")
        print(f"{'tool.function':42s} {'uses':>5s} {'ok%':>5s} {'0row%':>6s} {'foll%':>6s} {'corr%':>6s}")
        for t in rep["tools"]:
            print(f"{t['tool_function']:42s} {t['uses']:5d} {t['success_rate']*100:4.0f}% "
                  f"{t['zero_row_rate']*100:5.0f}% {t['followup_rate']*100:5.0f}% {t['correction_rate']*100:5.0f}%")
        return rep
