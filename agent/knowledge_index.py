"""
knowledge_index.py — build + search the company knowledge vector index (RAG).

Complements the structured Gold tools: the tools answer "what IS" (rows,
counts, dates); this answers "how does it WORK / what exists / why" from
prose knowledge — system docs, automation registry business cards, business
definitions, company memory rules.

Design (see docs/vectorization_plan.md):
  * One system/automation = one document (never split a system card).
  * Markdown docs split by ## section, small sections merged (~<=1800 chars).
  * Embeddings: OpenAI text-embedding-3-small (1536 dims), stored WITH the
    chunk text in data/agent/knowledge/knowledge_index.parquet — committed to
    git and baked into the image, so runtime needs no source docs and no
    rebuild. ~tens of KB per 50 chunks.
  * search(): embeds the query and ranks by cosine. If no OPENAI_API_KEY is
    available (CI, offline dev) it degrades to rapidfuzz keyword scoring over
    the same chunks — worse ranking, same contract.

Rebuild after editing docs/registries:   python knowledge_index.py build
Try a query:                             python knowledge_index.py search "how does the quote tool work"
"""
import json
import os
import re
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

AGENT_DIR = Path(__file__).resolve().parent
REPO_ROOT = AGENT_DIR.parents[1]
INDEX_PATH = AGENT_DIR / "knowledge" / "knowledge_index.parquet"

EMBED_MODEL = "text-embedding-3-small"
MAX_CHUNK_CHARS = 1800


# ---------------------------------------------------------------- sources

def _md_chunks(path: Path, source: str):
    """Split a markdown file on ## headings; merge sections below the size cap."""
    text = path.read_text(encoding="utf-8", errors="replace")
    parts = re.split(r"(?m)^## ", text)
    chunks, buf, title = [], "", None
    for i, part in enumerate(parts):
        seg = part if i == 0 else "## " + part
        seg_title = part.splitlines()[0].strip() if i > 0 else path.stem
        if len(buf) + len(seg) <= MAX_CHUNK_CHARS:
            buf += ("\n" if buf else "") + seg
            title = title or seg_title
        else:
            if buf.strip():
                chunks.append((title or path.stem, buf.strip()))
            buf, title = seg, seg_title
        while len(buf) > MAX_CHUNK_CHARS * 2:          # very long single section
            chunks.append((title or path.stem, buf[: MAX_CHUNK_CHARS * 2]))
            buf = buf[MAX_CHUNK_CHARS * 2:]
    if buf.strip():
        chunks.append((title or path.stem, buf.strip()))
    return [{"source": source, "title": t, "text": c} for t, c in chunks]


def _automation_docs():
    """One document per automation project — business card + extracted logic."""
    reg = yaml.safe_load(open(AGENT_DIR / "automation_registry.yaml", encoding="utf-8"))
    docs = []
    for key, e in (reg.get("automations") or {}).items():
        b = e.get("business", {})
        lines = [
            f"System: {e.get('display_name', key)} (repo {e.get('repo', '?')})",
            f"Category: {b.get('category', '')}",
            f"Description: {b.get('description', '')}",
            f"Stack: {', '.join(b.get('stack', []))}",
            f"Azure services: {', '.join(b.get('azure_services', []))}",
        ]
        logic = e.get("logic") or {}
        for section, rules in logic.items() if isinstance(logic, dict) else []:
            if isinstance(rules, list):
                lines.append(f"{section}: " + "; ".join(str(r) for r in rules[:12]))
            else:
                lines.append(f"{section}: {rules}")
        docs.append({
            "source": "automation_registry",
            "title": e.get("display_name", key),
            "text": "\n".join(l for l in lines if l.split(': ', 1)[-1].strip()),
        })
    return docs


def _power_automate_docs():
    """One document per Power Automate cloud flow (business logic records)."""
    reg = yaml.safe_load(open(AGENT_DIR / "automation_registry.yaml", encoding="utf-8"))
    docs = []
    for key, e in (reg.get("power_automate_flows") or {}).items():
        b = e.get("business", {})
        logic = e.get("logic", {})
        lines = [
            f"Power Automate flow: {e.get('display_name', key)} (state: {e.get('state', '?')})",
            f"Schedule: {e.get('schedule', '')}",
            f"Description: {b.get('description', '')}",
            "Steps: " + "; ".join(str(s) for s in (logic.get("steps") or [])),
            f"Notes: {logic.get('notes', '')}",
        ]
        docs.append({
            "source": "power_automate",
            "title": e.get("display_name", key),
            "text": "\n".join(l for l in lines if l.split(': ', 1)[-1].strip()),
        })
    return docs


def _business_definition_docs():
    defs = yaml.safe_load(open(AGENT_DIR / "business_definitions.yaml", encoding="utf-8"))
    docs = []
    for group, terms in (defs or {}).items():
        if not isinstance(terms, dict):
            continue
        lines = [f"Business definitions — {group}:"]
        for name, t in terms.items():
            if isinstance(t, dict):
                phrases = " / ".join(t.get("phrases", [])[:8])
                meaning = t.get("predicate") or t.get("meaning") or t.get("description") or ""
                lines.append(f"- {name}: {phrases} => {meaning}")
            else:
                lines.append(f"- {name}: {t}")
        docs.append({"source": "business_definitions", "title": group, "text": "\n".join(lines)})
    return docs


def _company_memory_docs():
    p = AGENT_DIR / "memory" / "company_memory.yaml"
    if not p.exists():
        return []
    mem = yaml.safe_load(open(p, encoding="utf-8")) or {}
    docs = []
    for section, items in mem.items() if isinstance(mem, dict) else []:
        text = yaml.safe_dump({section: items}, allow_unicode=True, sort_keys=False)
        for i in range(0, len(text), MAX_CHUNK_CHARS):
            docs.append({"source": "company_memory", "title": section,
                         "text": text[i:i + MAX_CHUNK_CHARS]})
    return docs


def gather_documents():
    docs = []
    for rel, source in [
        ("README.md", "readme"),
        ("docs/ARCHITECTURE.md", "architecture"),
        ("docs/knowledge_map.md", "knowledge_map"),
        ("docs/tool_catalog.md", "tool_catalog"),
        ("docs/azure_estate.md", "azure_estate"),
    ]:
        p = REPO_ROOT / rel
        if p.exists():
            docs += _md_chunks(p, source)
    docs += _automation_docs()
    docs += _power_automate_docs()
    docs += _business_definition_docs()
    docs += _company_memory_docs()
    return docs


# ---------------------------------------------------------------- embeddings

def _openai_key():
    key = os.getenv("OPENAI_API_KEY")
    if key:
        return key
    envfile = AGENT_DIR.parent / "Raw Data" / "API" / "credential" / ".env"
    if envfile.exists():
        from dotenv import dotenv_values
        return dotenv_values(envfile).get("OPENAI_API_KEY")
    return None


def _embed(texts, key):
    from openai import OpenAI
    client = OpenAI(api_key=key)
    out = []
    for i in range(0, len(texts), 96):
        resp = client.embeddings.create(model=EMBED_MODEL, input=texts[i:i + 96])
        out += [d.embedding for d in resp.data]
    return out


# ---------------------------------------------------------------- build / search

def build_index():
    key = _openai_key()
    if not key:
        raise RuntimeError("OPENAI_API_KEY needed to build the index")
    docs = gather_documents()
    vecs = _embed([d["text"] for d in docs], key)
    df = pd.DataFrame(docs)
    df["embedding"] = [json.dumps([round(x, 6) for x in v]) for v in vecs]
    INDEX_PATH.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(INDEX_PATH, index=False)
    return len(df)


_index_cache = None


def _load_index():
    global _index_cache
    if _index_cache is None:
        if not INDEX_PATH.exists():
            return None
        df = pd.read_parquet(INDEX_PATH)
        mat = np.array([json.loads(e) for e in df["embedding"]], dtype=np.float32)
        mat /= np.linalg.norm(mat, axis=1, keepdims=True)
        _index_cache = (df, mat)
    return _index_cache


def search(query, top_k=5, mode="auto"):
    """Return [{source, title, text, score}]. mode: auto | vector | keyword."""
    loaded = _load_index()
    if loaded is None:
        return []
    df, mat = loaded
    key = _openai_key() if mode in ("auto", "vector") else None
    if key and mode != "keyword":
        try:
            q = np.array(_embed([query], key)[0], dtype=np.float32)
            q /= np.linalg.norm(q)
            scores = mat @ q
        except Exception:
            scores = _keyword_scores(query, df)
    else:
        scores = _keyword_scores(query, df)
    order = np.argsort(scores)[::-1][:top_k]
    return [{"source": df.iloc[i]["source"], "title": df.iloc[i]["title"],
             "text": df.iloc[i]["text"], "score": round(float(scores[i]), 4)}
            for i in order if scores[i] > 0]


def _keyword_scores(query, df):
    from rapidfuzz import fuzz
    return np.array([fuzz.token_set_ratio(query.lower(), t.lower()) / 100.0
                     for t in df["text"]], dtype=np.float32)


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "build"
    if cmd == "build":
        n = build_index()
        print(f"indexed {n} chunks -> {INDEX_PATH}")
    elif cmd == "search":
        for hit in search(" ".join(sys.argv[2:]) or "timesheet automation"):
            print(f"[{hit['score']:.3f}] {hit['source']} :: {hit['title']}")
            print("   " + hit["text"][:160].replace("\n", " ") + "…")
