"""Live OPMS read tool — real-time hours straight from the OPMS API.

The lake is a nightly 02:00-Perth snapshot; anything entered into OPMS after
that is invisible to the DuckDB tools until the next refresh. This tool fills
that gap for the one question where staleness bites daily: "how many hours has
X worked THIS week / today?"

Read-only, one worker per call. Dedup mirrors the lake's winning-sheet rule
(build_silver_gold._build_opms_hours_map): sum entries within a sheet, then per
(day, site) keep only the most-recently-modified sheet — OPMS can hold
duplicate sheets for the same date/site and summing across them doubles hours.
Gap deductions (weekly automation) are NOT applied here: raw OPMS values only.
"""
import os

from ._base import ToolResult
from .opms_write_tool import AUTH_URL, API_BASE

ENTRIES_PATH = "/timesheets/entries"
PAGE_SIZE = 25                 # OPMS caps /timesheets/entries page_size at 25
MAX_PAGES = 60                 # safety cap ≈ 1500 sheets per call


class OpmsLiveTool:
    name = "opms_live"

    def _token(self):
        import requests
        r = requests.post(AUTH_URL, data="grant_type=client_credentials",
                          headers={"Content-Type": "application/x-www-form-urlencoded"},
                          auth=(os.environ["OPMS_CLIENT_ID"], os.environ["OPMS_CLIENT_SECRET"]),
                          timeout=30)
        r.raise_for_status()
        return r.json()["access_token"]

    def get_live_worker_hours(self, worker_id, date_from=None, date_to=None,
                              user_role="default"):
        res = ToolResult(tool=self.name, function="get_live_worker_hours",
                         args={"worker_id": worker_id, "date_from": date_from,
                               "date_to": date_to})
        try:
            wid = str(int(worker_id))
        except (TypeError, ValueError):
            res.caveats.append("worker_id must be an integer OPMS id — refused.")
            return res

        import datetime as dt
        from lulu_time import perth_today
        today = perth_today()
        if not date_from:
            date_from = (today - dt.timedelta(days=today.weekday())).isoformat()
        if not date_to:
            date_to = today.isoformat()
        try:
            d_from, d_to = dt.date.fromisoformat(date_from), dt.date.fromisoformat(date_to)
        except ValueError:
            res.caveats.append("dates must be YYYY-MM-DD — refused.")
            return res
        res.args.update(date_from=date_from, date_to=date_to)

        try:
            import requests
            headers = {"Authorization": f"Bearer {self._token()}"}
            # modified_since filters on SHEET modification time; a sheet holding
            # hours for a day is only ever touched on/after that day, so the
            # range start is a safe lower bound (same assumption the extractor
            # uses). The endpoint requires a full ISO datetime, not a date.
            params = {"modified_since": f"{date_from}T00:00:00Z", "page_size": PAGE_SIZE}
            contrib = {}          # (day, site) -> {sheet_id: [hours, last_modified]}
            pages = 0
            cursor = None
            while pages < MAX_PAGES:
                if cursor:
                    params["after"] = cursor
                r = requests.get(f"{API_BASE}{ENTRIES_PATH}", headers=headers,
                                 params=params, timeout=60)
                r.raise_for_status()
                data = r.json()
                # response shapes seen from OPMS: {data:[...]}, {timesheets:[...]}, [...]
                if isinstance(data, dict):
                    if isinstance(data.get("data"), list):
                        batch = data["data"]
                    elif isinstance(data.get("timesheets"), list):
                        batch = data["timesheets"]
                    else:
                        batch = []
                else:
                    batch = data or []
                for ts in (batch or []):
                    raw_date = str(ts.get("date") or "")[:10]
                    try:
                        day = dt.date.fromisoformat(raw_date)
                    except ValueError:
                        continue
                    if not (d_from <= day <= d_to):
                        continue
                    sid = str(ts.get("id") or "")
                    site = str(ts.get("site_id") or "")
                    modified = str(ts.get("last_modified_date") or "")
                    for entry in (ts.get("entries") or []):
                        emp = ((entry.get("employee") or {}).get("id"))
                        try:
                            if str(int(float(emp))) != wid:
                                continue
                        except (TypeError, ValueError):
                            continue
                        try:
                            v = float(entry.get("value") or 0)
                        except (TypeError, ValueError):
                            v = 0.0
                        cur = contrib.setdefault((day, site), {}).setdefault(sid, [0.0, modified])
                        cur[0] += v
                pages += 1
                cursor = data.get("next_cursor") if isinstance(data, dict) else None
                if not cursor or not batch:
                    break

            per_day = {}
            for (day, _site), sheets in contrib.items():
                best = max(sheets.values(), key=lambda hv: hv[1])   # winning sheet
                per_day[day] = round(per_day.get(day, 0.0) + best[0], 2)

            rows = [{"work_date": d.isoformat(), "hours": h}
                    for d, h in sorted(per_day.items())]
            total = round(sum(per_day.values()), 2)
            res.ok = True
            res.data, res.row_count = rows, len(rows)
            res.confidence = "High"
            res.summary = (f"LIVE OPMS hours for worker {wid}, {date_from} → {date_to}: "
                           f"{total:,.1f}h across {len(rows)} day(s) with entries. "
                           f"Queried OPMS directly just now (Perth date {today.isoformat()}) — "
                           "fresher than the nightly lake snapshot.")
            res.caveats.append("Raw OPMS sheet hours: sign-out gap deductions are NOT applied, "
                               "so totals can be slightly higher than get_weekly_timesheet.")
            if pages >= MAX_PAGES and cursor:
                res.caveats.append("Result truncated at the page cap — narrow the date range.")
        except KeyError as e:
            res.caveats.append(f"Missing OPMS credential in environment: {e}")
        except Exception as e:
            res.caveats.append(f"OPMS API error: {type(e).__name__}: {e}")
        return res
