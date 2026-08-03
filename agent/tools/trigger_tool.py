"""Governed automation trigger — first §25 Level-2 operational action.

Lets an authorised caller START a registered internal automation. No arbitrary
targets: only keys in TRIGGERS exist, each mapping to one Azure resource the
gateway's managed identity was explicitly granted rights on (least privilege:
'Container Apps Jobs Operator' scoped to that single job).

Same guardrail chain as the write tools: allowed_users pin + _confirm
challenge + required reason + dry_run default TRUE + post-start verification
(the created execution is read back) + full audit.
"""
import os

from ._base import ToolResult

ARM = "https://management.azure.com"
ARM_API = "2023-05-01"

TRIGGERS = {
    "lulu_refresh": {
        "what": "Full data-lake refresh — OPMS + SharePoint BMS -> bronze/silver/gold -> blob "
                "(the same job the nightly 02:00 Perth schedule runs; takes ~60 min).",
        "job_id": os.getenv(
            "LULU_REFRESH_JOB_ID",
            "/subscriptions/00000000-0000-4000-a000-000000000011/resourceGroups/lulu-rg"
            "/providers/Microsoft.App/jobs/lulu-refresh"),
        "note": "Refreshed gold lands in blob storage; the gateway serves the gold it loaded "
                "at startup, so lake queries pick the new data up after the next app restart "
                "or nightly rollover. get_live_worker_hours is unaffected (always real-time).",
    },
}


class TriggerTool:
    name = "trigger"

    def _arm_headers(self):
        """ARM token via the Container Apps managed identity (no stored secret)."""
        import requests
        r = requests.get(os.environ["IDENTITY_ENDPOINT"],
                         params={"resource": f"{ARM}/", "api-version": "2019-08-01"},
                         headers={"X-IDENTITY-HEADER": os.environ["IDENTITY_HEADER"]},
                         timeout=30)
        r.raise_for_status()
        return {"Authorization": f"Bearer {r.json()['access_token']}",
                "Content-Type": "application/json"}

    def _latest_execution(self, headers, job_id):
        import requests
        r = requests.get(f"{ARM}{job_id}/executions", headers=headers,
                         params={"api-version": ARM_API}, timeout=30)
        r.raise_for_status()
        execs = (r.json() or {}).get("value") or []
        if not execs:
            return None
        latest = max(execs, key=lambda e: (e.get("properties") or {}).get("startTime") or "")
        return {"execution": latest.get("name"),
                "status": (latest.get("properties") or {}).get("status"),
                "started": (latest.get("properties") or {}).get("startTime")}

    def trigger_automation(self, automation, reason=None, dry_run=True,
                           user_role="default"):
        res = ToolResult(tool=self.name, function="trigger_automation",
                         args={"automation": automation, "dry_run": bool(dry_run),
                               "reason": reason or ""})
        spec = TRIGGERS.get(str(automation or "").strip().lower())
        if spec is None:
            res.caveats.append(f"Unknown automation {automation!r} — registered triggers: "
                               f"{', '.join(sorted(TRIGGERS))}.")
            return res

        try:
            import requests
            headers = self._arm_headers()
            last = self._latest_execution(headers, spec["job_id"])

            if dry_run:
                res.ok = True
                res.data = [{"automation": automation, "would_start": spec["what"],
                             "last_execution": last}]
                res.row_count = 1
                res.confidence = "High"
                res.summary = (f"DRY RUN: would start '{automation}' — {spec['what']} "
                               f"Last execution: {last or 'none found'}. No changes have "
                               "been made. Execute with dry_run=false (confirmation still "
                               "required).")
                res.caveats.append(spec["note"])
                return res

            if last and last.get("status") == "Running":
                res.caveats.append(f"'{automation}' is ALREADY RUNNING (execution "
                                   f"{last['execution']} since {last['started']}) — refused "
                                   "to start a second overlapping run.")
                return res

            r = requests.post(f"{ARM}{spec['job_id']}/start", headers=headers,
                              params={"api-version": ARM_API}, json={}, timeout=60)
            r.raise_for_status()
            started = self._latest_execution(headers, spec["job_id"])
            verified = bool(started and started.get("status") in ("Running", "Processing"))
            res.ok = True
            res.data = [{"automation": automation, "started_execution": started,
                         "verified_running": verified}]
            res.row_count = 1
            res.confidence = "High" if verified else "Medium"
            res.summary = (f"Started '{automation}': execution "
                           f"{(started or {}).get('execution', '?')} is "
                           f"{(started or {}).get('status', 'submitted')}. {spec['what']}")
            res.caveats.append(spec["note"])
        except KeyError as e:
            res.caveats.append(f"Managed-identity environment not available: {e} — this "
                               "action only works on the cloud gateway, not local stdio.")
        except Exception as e:
            res.caveats.append(f"ARM error: {type(e).__name__}: {e}")
        return res
