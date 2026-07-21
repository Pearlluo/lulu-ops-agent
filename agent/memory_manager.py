"""
Lulu's three-tier memory — what separates a Memory Agent from a RAG agent.

    Tier 1  Data Memory          = Gold (already built; refreshed nightly)
    Tier 2  Business Memory      = memory/company_memory.yaml   (site rules, supplier flags,
                                    definitions, dated facts — knowledge Admin TELLS Lulu)
    Tier 3  Conversation Memory  = memory/conversation_memory.yaml (per-user preferences,
                                    focus topics, recent questions)

Flow per utterance:
    capture(text)  -> classify: chit-chat? question? BUSINESS KNOWLEDGE?
                      knowledge -> persist into company_memory (survives restarts)
    recall(question) -> matched rules/flags/definitions/facts for the planner to use
    workers_meeting_tickets(tickets) -> memory-driven Gold query: who holds ALL the
                      required (non-expired) certs for a remembered site rule.
                      Runs through the SAME safety chain (validator -> DuckDB -> Gold).

Deterministic (no LLM key needed). An LLM extractor can be layered on later via the gateway.
"""

import re
from pathlib import Path

import yaml
from lulu_time import perth_today as _today


class _D:                      # keep existing str(date.today()) call sites working, Perth-pinned
    @staticmethod
    def today():
        return _today()


date = _D

AGENT_DIR = Path(__file__).resolve().parent
MEM_DIR = AGENT_DIR / "memory"
COMPANY_PATH = MEM_DIR / "company_memory.yaml"
CONVO_PATH = MEM_DIR / "conversation_memory.yaml"

# ticket shorthand -> ILIKE keyword that matches real Gold competency names
TICKET_SYNONYMS = {
    "wah": "height", "working at heights": "height", "heights": "height",
    "voc": "voc", "verification of competency": "voc",
    "driver licence": "driver", "driver license": "driver", "drivers licence": "driver",
    "driver_licence": "driver", "licence": "driver",
    "confined space": "confined", "confined_space": "confined",
    "gas test": "gas test", "gas_test": "gas test",
}

CHITCHAT = re.compile(r"^(hi|hello|hey|thanks?|thank you|ok|okay|cool|great|good|nice|bye|你好|谢谢|好的|嗯|哈喽)\W*$", re.I)
QUESTION = re.compile(r"[?？]|^(who|what|which|how|when|where|is |are |do |does |can |show|list|give|谁|什么|哪|多少|怎么|是否|有没有)", re.I)
SITE_RULE = re.compile(r"(?P<site>[A-Za-z][A-Za-z0-9 \-_]{1,24}?)\s*(?:要求|的要求是|requires?|needs?)\s*[:：]?\s*(?P<list>.+)", re.I)
SUPPLIER_FLAG = re.compile(r"(?P<sup>[A-Za-z一-鿿][\w\- ]{1,30}?)\s*(?:经常出问题|老出问题|有问题|不靠谱|风险很高|is (?:a )?(?:problem|risky|high[- ]risk)|keeps? (?:causing|having) (?:issues|problems))", re.I)
DEFINITION = re.compile(r"(?P<term>[A-Za-z一-鿿][\w ]{1,30}?)\s*(?:的定义是|的意思是|意思是|means|is defined as)\s+(?P<def>.{3,200})", re.I)
PREFERENCE = re.compile(r"(?:我喜欢|我希望|每周给我|每天给我|I (?:like|prefer|want)|remind me|给我.*报告)", re.I)


def _load(path, default):
    if path.exists():
        try:
            return yaml.safe_load(path.read_text(encoding="utf-8")) or default
        except Exception:
            return default
    return default


def _norm_ticket(t):
    return re.sub(r"\s+", " ", t.strip().strip(".,;、")).lower()


class MemoryManager:
    def __init__(self):
        MEM_DIR.mkdir(exist_ok=True)
        self.company = _load(COMPANY_PATH, {"site_rules": {}, "suppliers": {"high_risk": []},
                                            "definitions": {}, "facts": []})
        self.convo = _load(CONVO_PATH, {"users": {}})
        self._qt = None

    # ---------------- persistence ----------------
    def _save_company(self):
        COMPANY_PATH.write_text(yaml.safe_dump(self.company, allow_unicode=True, sort_keys=False),
                                encoding="utf-8")

    def _save_convo(self):
        CONVO_PATH.write_text(yaml.safe_dump(self.convo, allow_unicode=True, sort_keys=False),
                              encoding="utf-8")

    # ---------------- capture: learn from what the user says ----------------
    def capture(self, text, user="admin"):
        """Classify an utterance. Returns (kind, learned) where kind in
        chitchat|question|site_rule|supplier_flag|definition|preference|fact|none."""
        t = text.strip()
        if not t or CHITCHAT.match(t):
            return "chitchat", None
        if QUESTION.search(t) and not SITE_RULE.search(t):
            return "question", None

        m = SITE_RULE.search(t)
        if m:
            site = m.group("site").strip().lower().replace(" ", "_")
            tickets = [_norm_ticket(x) for x in re.split(r"[,，、+\n]| and ", m.group("list")) if _norm_ticket(x)]
            if tickets:
                self.company["site_rules"][site] = {"required_tickets": tickets,
                                                    "learned": str(date.today()), "source": user}
                self._save_company()
                return "site_rule", {site: tickets}

        m = SUPPLIER_FLAG.search(t)
        if m:
            sup = m.group("sup").strip()
            flags = self.company.setdefault("suppliers", {}).setdefault("high_risk", [])
            entry = {"name": sup, "noted": str(date.today()), "by": user, "note": t[:120]}
            if not any(f.get("name", "").lower() == sup.lower() for f in flags if isinstance(f, dict)):
                flags.append(entry)
                self._save_company()
            return "supplier_flag", entry

        m = DEFINITION.search(t)
        if m and len(m.group("term")) < 31:
            term = m.group("term").strip().lower()
            self.company["definitions"][term] = {"meaning": m.group("def").strip(),
                                                 "learned": str(date.today())}
            self._save_company()
            return "definition", {term: m.group("def").strip()}

        if PREFERENCE.search(t):
            u = self.convo["users"].setdefault(user, {"likes": [], "focuses": {}, "recent_questions": []})
            if t[:120] not in u["likes"]:
                u["likes"].append(t[:120])
                self._save_convo()
            return "preference", t[:120]

        if re.search(r"要求|规则|必须|记住|policy|rule|remember|always|never", t, re.I):
            self.company["facts"].append({"fact": t[:300], "learned": str(date.today()), "source": user})
            self.company["facts"] = self.company["facts"][-100:]
            self._save_company()
            return "fact", t[:300]

        return "none", None

    # ---------------- recall: surface relevant memory for a question ----------------
    def recall(self, question):
        q = question.lower()
        hit = {"site_rules": {}, "supplier_flags": [], "definitions": {}, "facts": []}
        for site, rule in self.company.get("site_rules", {}).items():
            if site.replace("_", " ") in q or site in q:
                hit["site_rules"][site] = rule
        for f in self.company.get("suppliers", {}).get("high_risk", []):
            name = f.get("name", "") if isinstance(f, dict) else str(f)
            if name and (name.lower() in q or "supplier" in q or "供应商" in q or "risk" in q or "风险" in q):
                hit["supplier_flags"].append(f)
        for term, d in self.company.get("definitions", {}).items():
            if term in q:
                hit["definitions"][term] = d
        for f in self.company.get("facts", [])[-20:]:
            words = [w for w in re.findall(r"[a-z一-鿿]{3,}", f.get("fact", "").lower())][:6]
            if any(w in q for w in words):
                hit["facts"].append(f)
        hit["any"] = bool(hit["site_rules"] or hit["supplier_flags"] or hit["definitions"] or hit["facts"])
        return hit

    def render_context(self, question):
        """Compact memory block to hand to a planner (LLM or deterministic)."""
        r = self.recall(question)
        if not r["any"]:
            return ""
        lines = ["[Business memory — learned in past conversations]"]
        for s, rule in r["site_rules"].items():
            lines.append(f"- Site rule: {s.upper()} requires {', '.join(rule['required_tickets'])} (learned {rule['learned']})")
        for f in r["supplier_flags"]:
            lines.append(f"- Supplier flag: {f.get('name')} flagged high-risk on {f.get('noted')} ({f.get('note','')[:60]})")
        for t, d in r["definitions"].items():
            lines.append(f"- Definition: '{t}' = {d['meaning']}")
        for f in r["facts"]:
            lines.append(f"- Fact ({f['learned']}): {f['fact'][:100]}")
        return "\n".join(lines)

    # ---------------- memory -> Gold composition (same safety chain) ----------------
    def _query_tool(self):
        if self._qt is None:
            from query_tool import QueryTool
            self._qt = QueryTool()
        return self._qt

    @staticmethod
    def _kw(ticket):
        return TICKET_SYNONYMS.get(ticket, ticket.replace("_", " "))

    def workers_meeting_tickets(self, tickets, user_role="default"):
        """Workers holding a VALID (non-expired) cert for EVERY required ticket."""
        kws = [self._kw(t).replace("'", "''") for t in tickets]
        cases = " ".join(f"WHEN competency_name ILIKE '%{k}%' THEN '{k}'" for k in kws)
        sql = ("SELECT first_name, last_name, "
               f"COUNT(DISTINCT CASE {cases} END) AS matched "
               "FROM training_compliance WHERE is_expired = false "
               f"GROUP BY first_name, last_name HAVING matched = {len(kws)} ORDER BY last_name")
        r = self._query_tool().run(sql, user_role)
        rows = [dict(zip(r.cols, row)) for row in r.rows] if r.ok else []
        return rows, (r.sql if r.ok else "; ".join(r.errors))

    # ---------------- conversation memory ----------------
    def observe(self, user, question, domain):
        u = self.convo["users"].setdefault(user, {"likes": [], "focuses": {}, "recent_questions": []})
        if domain:
            key = domain.split("/")[0].strip().lower() or "other"
            u["focuses"][key] = u["focuses"].get(key, 0) + 1
        u["recent_questions"] = (u["recent_questions"] + [question[:100]])[-50:]
        self._save_convo()

    def user_profile(self, user="admin"):
        u = self.convo["users"].get(user, {})
        focuses = sorted(u.get("focuses", {}).items(), key=lambda x: -x[1])[:5]
        return {"likes": u.get("likes", []), "top_focuses": focuses,
                "questions_asked": len(u.get("recent_questions", []))}
