"""Quote hours tool — reads saved quotes from the Online Shutdown Quote tool.

Data source: blob container (default online-quote-data, same storage
account as lulu-data so the existing BLOB_CONNECTION_STRING covers it),
blob quotes.json — the quote app's saved-quote index. Logic is a direct port
of the Hours Remaining tracker's quote_source.py: pick the newest version
WITH manning data; quoted hours per role row = qty x sum over manning days
of that day's shift-type hours.

Read-only, live (no lake dependency). Rates in the payload are NEVER output —
only hours, shifts, dates and job header fields.
"""
import json
import os

from ._base import ToolResult

QUOTES_BLOB = "quotes.json"


def _normalise_job_id(text):
    """'26046' -> 'SH-26046'; 'sh-26046' -> 'SH-26046'."""
    t = str(text or "").strip().upper()
    if t.isdigit():
        t = f"SH-{t}"
    return t


def _has_manning(entry):
    manning = (entry.get("payload") or {}).get("manning") or {}
    return any(str(v).strip().upper() not in ("", "OFF")
               for row in manning.values() for v in (row or {}).values())


def _quoted_lines(payload):
    """Per quote role row: hours already multiplied by qty (Hours Remaining parity)."""
    shift_hours, shift_type = {}, {}
    for s in payload.get("shift_types") or []:
        desc = str(s.get("desc") or "").strip()
        if not desc:
            continue
        try:
            shift_hours[desc] = float(s.get("hrs") or 0)
        except (TypeError, ValueError):
            shift_hours[desc] = 0.0
        shift_type[desc] = "NS" if str(s.get("type") or "").upper() == "NS" else "DS"

    manning = payload.get("manning") or {}
    out = []
    for r in list(payload.get("staff_roles") or []) + list(payload.get("new_roles") or []):
        rid = str(r.get("id"))
        try:
            qty = float(r.get("qty") or 0)
        except (TypeError, ValueError):
            qty = 0.0
        ds_shifts = ns_shifts = 0
        ds_hours = ns_hours = 0.0
        for _day, desc in (manning.get(rid) or {}).items():
            desc = str(desc or "").strip()
            if not desc or desc.upper() == "OFF" or desc not in shift_hours:
                continue
            if shift_type.get(desc) == "NS":
                ns_shifts += 1
                ns_hours += shift_hours[desc]
            else:
                ds_shifts += 1
                ds_hours += shift_hours[desc]
        out.append({"role": str(r.get("role") or "").strip(), "qty": qty,
                    "ds_shifts": int(ds_shifts * qty), "ns_shifts": int(ns_shifts * qty),
                    "ds_hours": round(ds_hours * qty, 1), "ns_hours": round(ns_hours * qty, 1),
                    "total_hours": round((ds_hours + ns_hours) * qty, 1)})
    return out


class QuoteTool:
    name = "quote"

    def _load_index(self):
        from azure.storage.blob import BlobServiceClient
        conn = None
        for k in ("BLOB_CONNECTION_STRING", "AZURE_STORAGE_CONNECTION_STRING"):
            conn = (os.getenv(k) or "").strip()
            if conn:
                break
        if not conn:
            raise RuntimeError("no blob connection string configured")
        svc = BlobServiceClient.from_connection_string(conn)
        cc = svc.get_container_client(os.getenv("LULU_QUOTE_CONTAINER",
                                                "online-quote-data"))
        return json.loads(cc.get_blob_client(QUOTES_BLOB).download_blob()
                          .readall().decode("utf-8"))

    def get_quote_hours(self, job_code, user_role="default"):
        res = ToolResult(tool=self.name, function="get_quote_hours",
                         args={"job_code": job_code})
        jid = _normalise_job_id(job_code)
        res.args["job_code"] = jid
        try:
            index = self._load_index()
            entry = index.get(jid)
            if entry is None:
                for k, v in index.items():
                    if str(k).strip().upper() == jid:
                        entry = v
                        break
            if entry is None:
                res.caveats.append(f"No saved quote for {jid}. Jobs with saved quotes: "
                                   f"{', '.join(sorted(str(k) for k in index))}.")
                return res

            records = entry if isinstance(entry, list) else [entry]
            records = sorted(records, key=lambda e: str(e.get("uploaded_at") or ""))
            manned = [r for r in records if _has_manning(r)]
            picked = (manned or records)[-1]
            payload = picked.get("payload") or {}

            lines = [ln for ln in _quoted_lines(payload) if ln["total_hours"] > 0]
            lines.sort(key=lambda ln: -ln["total_hours"])
            total = round(sum(ln["total_hours"] for ln in lines), 1)
            ds = round(sum(ln["ds_hours"] for ln in lines), 1)
            ns = round(sum(ln["ns_hours"] for ln in lines), 1)

            di = payload.get("data_input") or {}
            dates = sorted(payload.get("manning_dates") or [])
            res.ok = True
            res.data, res.row_count = lines, len(lines)
            res.confidence = "High"
            res.summary = (f"QUOTE (Online Shutdown Quote tool) for {jid} "
                           f"'{picked.get('job_title', '')}' ({picked.get('client_business_name', '')}): "
                           f"{total:,.1f} quoted hours — DS {ds:,.1f} / NS {ns:,.1f} across "
                           f"{len(lines)} roles, manning {dates[0] if dates else di.get('start_date', '?')} "
                           f"→ {dates[-1] if dates else di.get('end_date', '?')}. "
                           f"Version uploaded {picked.get('uploaded_at', '?')} "
                           f"({len(records)} saved version(s)).")
            res.caveats.append("Quoted hours = manning grid x shift lengths from the saved "
                               "quote (live from the quote tool's storage). Compare against "
                               "get_weekly_timesheet actuals / get_roster_summary scheduled; "
                               "rates are never exposed by this tool.")
            if not manned:
                res.caveats.append("No saved version has manning data — hours computed from "
                                   "the newest version anyway; treat as indicative only.")
        except Exception as e:
            res.caveats.append(f"Quote source error: {type(e).__name__}: {e}")
        return res
