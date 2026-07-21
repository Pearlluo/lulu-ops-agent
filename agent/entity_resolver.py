"""
entity_resolver.py — the Search Layer: 实体归一 (entity resolution) for Lulu.

The boss never says `get_project_hours(project_id=123)`. He says "Acmegroup 上周谁干活了?"
80% of the difficulty is understanding WHAT he means — so before any SQL:

    'Acmegroup'  -> normalise -> 'acmegroup' == norm('Acme Group')        (exact-after-normalise)
    'MG'          -> entity_aliases.yaml                                      (alias / abbreviation)
    'Acme Grup'  -> rapidfuzz >= 90                                          (typo tolerance)
    'Acme'       -> several candidates 70..90 -> ASK, never guess            (clarification)

Vocabulary comes from the ACTUAL Gold dim tables (through the safety chain) per type:
    site / project / client / supplier / company / person

API:
    resolve(term, types=None)  -> {"status": exact|alias|fuzzy|ambiguous|none,
                                   "match": {type, value, score} | None,
                                   "candidates": [{type, value, score}, ...]}
    suggest(term, limit=5)     -> top candidates regardless of threshold (for 0-row rescue)
"""

import re
from pathlib import Path

import yaml

AGENT_DIR = Path(__file__).resolve().parent
ALIASES_PATH = AGENT_DIR / "entity_aliases.yaml"

AUTO_THRESHOLD = 90       # >= : trust automatically
SUGGEST_THRESHOLD = 70    # 70..90 : offer as candidates, ask the user

try:
    from rapidfuzz import fuzz
    def _score(a, b):
        return max(fuzz.ratio(a, b), fuzz.token_set_ratio(a, b))
except ImportError:                                   # stdlib fallback
    from difflib import SequenceMatcher
    def _score(a, b):
        return SequenceMatcher(None, a, b).ratio() * 100


def _norm(s):
    """'Acme Group' / 'acmegroup' / 'ACME-GROUP' -> 'acmegroup'; '&' == 'and'"""
    s = str(s or "").lower().replace("&", " and ")
    return re.sub(r"[^a-z0-9一-鿿]", "", s)


# ---------------------------------------------------------------- vocabulary
_vocab = None          # {type: {norm_value: display_value}}


def _load_vocab(force=False):
    """NOTE: reads Gold parquet directly — this builds the NAME DICTIONARY only (every column
    here is in agent_registry allowed_fields); the validator's LIMIT clamp would truncate the
    person list (2k names) and silently break resolution. No user data is returned this way."""
    global _vocab
    if _vocab is not None and not force:
        return _vocab
    import duckdb
    con = duckdb.connect()
    gold = AGENT_DIR.parent / "gold"
    queries = {
        "site": [("site_assignment", "site_name"), ("weekly_timesheet", "site_name")],
        "project": [("project_job_summary", "project_name"), ("weekly_timesheet", "project_name")],
        "client": [("project_job_summary", "client_name"), ("project_bridge", "client_name"),
                   ("project_bridge", "client_code")],
        "supplier": [("supplier_summary", "supplier_name")],
        "company": [("employee_profile", "company_name")],
    }
    vocab = {t: {} for t in list(queries) + ["person"]}
    for etype, srcs in queries.items():
        for table, col in srcs:
            p = gold / f"{table}.parquet"
            if not p.exists():
                continue
            try:
                for (v,) in con.execute(
                        f"SELECT DISTINCT {col} FROM '{p}' WHERE {col} IS NOT NULL").fetchall():
                    v = str(v).strip()
                    if len(_norm(v)) >= 3:
                        vocab[etype].setdefault(_norm(v), v)
            except Exception:
                pass
    try:
        p = gold / "employee_profile.parquet"
        for fn, ln in con.execute(
                f"SELECT DISTINCT first_name, last_name FROM '{p}' "
                "WHERE first_name IS NOT NULL AND last_name IS NOT NULL").fetchall():
            full = f"{fn} {ln}".strip()
            for v in (full, str(ln)):                  # full name + surname
                if len(_norm(v)) >= 3:
                    vocab["person"].setdefault(_norm(v), full)
    except Exception:
        pass
    _vocab = vocab
    return vocab


def _load_aliases():
    if not ALIASES_PATH.exists():
        return {}
    data = yaml.safe_load(ALIASES_PATH.read_text(encoding="utf-8")) or {}
    return {_norm(k): v for k, v in (data.get("aliases") or {}).items()}


# ---------------------------------------------------------------- core API
def resolve(term, types=None):
    """Resolve one term to a canonical Gold entity. Never guesses below AUTO_THRESHOLD."""
    n = _norm(term)
    if not n or len(n) < 2:
        return {"status": "none", "match": None, "candidates": []}
    vocab = _load_vocab()
    types = types or list(vocab.keys())

    # 1. exact after normalisation ('Acmegroup' == 'Acme Group')
    for t in types:
        if n in vocab[t]:
            return {"status": "exact", "match": {"type": t, "value": vocab[t][n], "score": 100},
                    "candidates": []}

    # 2. alias / abbreviation ('MG' -> 'Acme Group')
    ali = _load_aliases().get(n)
    if ali:
        target = resolve(ali["value"] if isinstance(ali, dict) else ali, types)
        if target["match"]:
            m = dict(target["match"], score=100)
            return {"status": "alias", "match": m, "candidates": []}

    # 3. fuzzy across the whole vocabulary
    scored = []
    for t in types:
        for nv, display in vocab[t].items():
            s = _score(n, nv)
            if s >= SUGGEST_THRESHOLD:
                scored.append({"type": t, "value": display, "score": round(s, 1)})
    scored.sort(key=lambda c: -c["score"])
    # collapse by normalised VALUE: 'Acme Group' the site/client/company is ONE entity,
    # keep the highest-priority type for it
    TYPE_PRIORITY = ["site", "client", "supplier", "project", "company", "person"]
    by_value = {}
    for c in scored:
        k = _norm(c["value"])
        cur = by_value.get(k)
        if cur is None or c["score"] > cur["score"] or \
                (c["score"] == cur["score"]
                 and TYPE_PRIORITY.index(c["type"]) < TYPE_PRIORITY.index(cur["type"])):
            by_value[k] = c
    candidates = sorted(by_value.values(), key=lambda c: -c["score"])

    if not candidates:
        return {"status": "none", "match": None, "candidates": []}
    top = candidates[0]
    runners = [c for c in candidates[1:4] if c["score"] >= AUTO_THRESHOLD]
    if top["score"] >= AUTO_THRESHOLD and not runners:
        return {"status": "fuzzy", "match": top, "candidates": candidates[1:4]}
    if top["score"] >= AUTO_THRESHOLD and runners:
        return {"status": "ambiguous", "match": None, "candidates": [top] + runners}
    return {"status": "ambiguous" if len(candidates) > 1 else "none",
            "match": None, "candidates": candidates[:4]}


def suggest(term, limit=5, types=None):
    """Top candidates with no threshold gate — for rescuing 0-row answers."""
    n = _norm(term)
    if not n:
        return []
    vocab = _load_vocab()
    scored = []
    for t in (types or vocab.keys()):
        for nv, display in vocab[t].items():
            scored.append({"type": t, "value": display, "score": round(_score(n, nv), 1)})
    scored.sort(key=lambda c: -c["score"])
    out, seen = [], set()
    for c in scored:
        if c["value"] not in seen and c["score"] >= 55:
            seen.add(c["value"])
            out.append(c)
        if len(out) >= limit:
            break
    return out


def resolve_in_question(question):
    """Scan a free-text question for resolvable entity mentions.
    Catches: site=X patterns, latin tokens/phrases unknown to the planner.
    Returns the best {'type','value','score','raw'} or None."""
    cands = []
    m = re.search(r"(site|project|client|supplier|company)\s*[=:：]\s*([A-Za-z0-9 _.&-]+)", question, re.I)
    if m:
        etype, raw = m.group(1).lower(), m.group(2).strip()
        r = resolve(raw, types=[etype])
        if r["match"]:
            return dict(r["match"], raw=raw)
        sug = r["candidates"] or suggest(raw, types=[etype])
        if sug and sug[0]["score"] >= AUTO_THRESHOLD:
            return dict(sug[0], raw=raw)
        # explicit filter that we can't confidently resolve: keep the RAW value so the
        # tool filters (0 rows) and the zero-row rescue can offer candidates — never
        # silently drop the user's filter and return everything
        return {"type": etype, "value": raw, "score": 0, "raw": raw}

    # latin words / phrases (1-3 tokens), longest first so 'Acme Group' beats 'Acme'
    words = re.findall(r"[A-Za-z][A-Za-z0-9&.-]{2,}", question)
    stop = {"the", "and", "for", "what", "who", "show", "timesheet", "roster", "site", "week",
            "last", "this", "next", "month", "all", "about", "hours", "project", "client",
            "list", "give", "how", "many", "worker", "workers", "automation", "github"}
    phrases = []
    for i in range(len(words)):
        for ln in (3, 2, 1):
            if i + ln <= len(words):
                p = " ".join(words[i:i + ln])
                if words[i].lower() not in stop:
                    phrases.append(p)
    seen = set()
    for p in sorted(phrases, key=len, reverse=True):
        if p.lower() in seen or len(_norm(p)) < 4:
            continue
        seen.add(p.lower())
        r = resolve(p)
        if r["match"] and r["match"]["score"] >= AUTO_THRESHOLD:
            cands.append(dict(r["match"], raw=p))

    # short ALL-CAPS abbreviations ('MG', 'T&H') — alias map only, to avoid false positives
    if not cands:
        for m in re.finditer(r"(?<![A-Za-z0-9])([A-Z][A-Z&]{1,3})(?![A-Za-z0-9])", question):
            r = resolve(m.group(1))
            if r["match"] and r["status"] == "alias":
                cands.append(dict(r["match"], raw=m.group(1)))
                break
    if not cands:
        return None
    cands.sort(key=lambda c: (-len(_norm(c["raw"])), -c["score"]))   # longest mention wins
    return cands[0]


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    for t in ["Acmegroup", "acme group", "MG", "Acme Grup", "Carter", "westlake", "NWM", "Transport and Hire"]:
        r = resolve(t)
        print(f"{t!r:22} -> {r['status']:10}", r["match"] or r["candidates"][:3])
