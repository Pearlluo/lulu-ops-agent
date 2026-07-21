"""
Answer stage — the second brain. Takes the planner's tool EVIDENCE and produces the
final business answer with the (usually cheaper) answer model.

Plan A: planner=Opus, answer=Opus      -> two_stage 'auto' skips this stage (same brain).
Plan B: planner=Opus, answer=DeepSeek  -> evidence-grounded polish on the cheap model.
Graceful: if the answer provider has no API key or errors, return the planner's draft —
two-stage is an optimisation, never a point of failure.
"""

import json

from llm_provider import get_provider, load_settings

ANSWER_SYSTEM = """You are Lulu, Acme Group's workforce-operations assistant, writing the FINAL answer.

You are given: the user's question, structured EVIDENCE from validated database tools, and a draft.
Rules (non-negotiable):
- Ground every statement in the EVIDENCE. Never invent numbers, names, dates, or records.
- Keep all figures exactly as they appear in the evidence.
- Always answer in English (Australian business English), regardless of the question's language.
- Lead with the direct answer, then brief supporting detail. Business tone, concise.
- If the evidence notes restricted fields/roles or data caveats, keep that note in the answer.
- If the evidence is empty, say no matching records were found — do not speculate."""


class AnswerGenerator:
    def __init__(self):
        self.settings = load_settings()
        try:
            self.provider = get_provider("answer")
        except Exception:
            self.provider = None

    def _active(self, planner_label):
        mode = self.settings.get("two_stage", "auto")
        if mode == "off" or self.provider is None or not self.provider.available():
            return False
        if mode == "on":
            return True
        return self.provider.label() != planner_label      # auto: only if it's a different brain

    def generate(self, question, evidence, draft, planner_label):
        """evidence: list of {tool, args, summary, data_sample}. Returns (final_text, answer_model_used)."""
        if not self._active(planner_label):
            return draft, None
        try:
            payload = json.dumps(evidence, ensure_ascii=False, default=str)[:8000]
            user = (f"QUESTION:\n{question}\n\nEVIDENCE (validated tool results):\n{payload}\n\n"
                    f"DRAFT ANSWER (from the planning model):\n{draft}\n\n"
                    "Write the final answer now.")
            resp = self.provider.chat(ANSWER_SYSTEM, [{"role": "user", "content": user}],
                                      max_tokens=2048,
                                      temperature=self.settings.get("temperature"))
            return (resp.text or draft), self.provider.label()
        except Exception:
            return draft, None        # never fail the run because polishing failed
