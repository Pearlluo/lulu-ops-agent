"""cockpit/action_log.py — append-only audit trail for the Repair Control Layer.

Every action event (dry-run, approval request, approve/reject, execute, result) is appended
as one JSON line to logs/cockpit_action_log.jsonl. This is the single source of truth for an
issue's repair status — issue_status() replays the events to derive the current state.

Append-only. No production data. No DuckDB.
"""
import json
from datetime import datetime, timezone
from pathlib import Path

# logs/ lives at data/agent/logs (cockpit/ -> data/agent -> logs)
LOG_DIR = Path(__file__).resolve().parent.parent / "logs"
LOG_PATH = LOG_DIR / "cockpit_action_log.jsonl"

# event status values an entry can carry
#   dry_run        a preview was run (no write)
#   pending_approval   approval requested, awaiting Approve/Reject
#   approved       approver said yes (execute now allowed)
#   rejected       approver said no -> issue goes to manual_review
#   executed       the action ran (see `result`: resolved / failed / partial)
#   manual         logged as a manual task (manual_only actions)


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def log_event(issue_id, action_key, status, actor="Admin", mode=None,
              result=None, detail="", writes=False, extra=None):
    """Append one audit event. Returns the written record (never raises on disk error)."""
    rec = {"ts": _now(), "issue_id": issue_id, "action_key": action_key,
           "status": status, "actor": actor, "mode": mode, "result": result,
           "writes": bool(writes), "detail": str(detail)[:2000]}
    if extra:
        rec.update(extra)
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass
    return rec


def read_events(limit=50, issue_id=None):
    """Most-recent-first events, optionally filtered to one issue."""
    if not LOG_PATH.exists():
        return []
    out = []
    try:
        for line in LOG_PATH.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except Exception:
                continue
            if issue_id and d.get("issue_id") != issue_id:
                continue
            out.append(d)
    except Exception:
        return []
    out.reverse()
    return out[:limit]


def issue_status(issue_id):
    """Derive an issue's repair state from its event history (None if never touched)."""
    evs = read_events(limit=1000, issue_id=issue_id)
    if not evs:
        return None
    # evs is newest-first; the latest meaningful state wins
    for e in evs:
        s = e.get("status")
        if s == "executed":
            r = e.get("result")
            return {"resolved": "resolved", "failed": "failed",
                    "partial": "partial"}.get(r, "executed")
        if s in ("rejected", "approved", "pending_approval", "manual",
                 "snoozed", "risk_accepted"):
            return s
    # only dry-runs so far
    return "previewed"


if __name__ == "__main__":
    print("log path:", LOG_PATH)
    for e in read_events(limit=10):
        print(e)
