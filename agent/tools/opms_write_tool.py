"""Worker-rating write tool — governed write capability (§25/§26).

Design rule (2026-07-29, after the direct-OPMS pilot): a write capability must
target the SOURCE OF TRUTH of its data flow, never a downstream copy. For
ratings that flow is:

    PPL-RankingTX (rating transactions, SharePoint)          <- source of truth
      -> nightly 'Ranking Flow Updated'  (average per person -> PPL-Ranking)
      -> nightly 'Ranking Data OPMS'     (PATCH OPMS employee additionalID12)

submit_worker_rating therefore:
  1. creates a rating transaction in PPL-RankingTX (what the Shutdown Rating
     site does), Supervisor = the authenticated caller (server-injected UPN,
     never client-supplied);
  2. immediately recomputes the worker's average across ALL their transactions
     with the same math as the nightly flow and pushes it to OPMS — so the
     change is live at once AND tonight's flows recompute the identical value
     (the push is idempotent with the scheduled pipeline).

Guardrail chain (all server-side): allowed_users pin + requires_confirmation +
requires_reason + dry_run default TRUE + post-write verification of BOTH
records + full audit.
"""
import os

from ._base import ToolResult

# ---- OPMS ----
AUTH_URL = "https://auth.opms.com.au/api/authenticate/token"
API_BASE = "https://api.opms.com.au"
RANKING_FIELD = "additionalID12"

# ---- SharePoint BMS via Graph ----
GRAPH_SITE = "https://graph.microsoft.com/v1.0/sites/yourtenant.sharepoint.com:/sites/BMS:"
LIST_RANKING_TX = "00000000-0000-4000-a000-000000000008"   # PPL-RankingTX
LIST_PEOPLE = "00000000-0000-4000-a000-000000000009"       # PPL-People
LIST_JOBS = "00000000-0000-4000-a000-000000000010"         # JMS-Jobs

RATING_MIN, RATING_MAX = 1, 5


class OpmsWriteTool:
    name = "opms_write"

    # -- OPMS client-credential helpers (same auth the pipeline extractors use) --

    def _token(self):
        import requests
        r = requests.post(AUTH_URL, data="grant_type=client_credentials",
                          headers={"Content-Type": "application/x-www-form-urlencoded"},
                          auth=(os.environ["OPMS_CLIENT_ID"], os.environ["OPMS_CLIENT_SECRET"]),
                          timeout=30)
        r.raise_for_status()
        return r.json()["access_token"]

    def _get_employee(self, headers, employee_id):
        import requests
        r = requests.get(f"{API_BASE}/employee/{employee_id}", headers=headers, timeout=30)
        r.raise_for_status()
        j = r.json()
        if isinstance(j, list):
            j = j[0] if j else {}
        return j or {}

    # -- Graph helpers (same app credentials the SharePoint extractors use) --

    def _graph_headers(self):
        import requests
        r = requests.post(
            f"https://login.microsoftonline.com/{os.environ['SHAREPOINT_TENANT_ID']}/oauth2/v2.0/token",
            data={"grant_type": "client_credentials",
                  "client_id": os.environ["SHAREPOINT_CLIENT_ID"],
                  "client_secret": os.environ["SHAREPOINT_CLIENT_SECRET"],
                  "scope": "https://graph.microsoft.com/.default"},
            timeout=30)
        r.raise_for_status()
        return {"Authorization": f"Bearer {r.json()['access_token']}",
                "Accept": "application/json",
                # BMS list columns are not indexed for these filters
                "Prefer": "HonorNonIndexedQueriesWarningMayFailRandomly"}

    def _graph_items(self, headers, list_id, flt, select="*", top=200):
        """GET list items matching an OData filter, following pagination."""
        import requests
        url = (f"{GRAPH_SITE}/lists/{list_id}/items"
               f"?$expand=fields($select={select})&$filter={flt}&$top={top}")
        items = []
        while url:
            r = requests.get(url, headers=headers, timeout=60)
            r.raise_for_status()
            j = r.json()
            items.extend(j.get("value", []))
            url = j.get("@odata.nextLink")
        return items

    def _person_scores(self, headers, person_item_id):
        """All rating scores for one person across PPL-RankingTX (nightly-flow math:
        every comma-separated numeric token of every non-empty RankingData row)."""
        rows = self._graph_items(headers, LIST_RANKING_TX,
                                 f"fields/PersonLookupId eq {int(person_item_id)}",
                                 select="RankingData")
        scores = []
        for it in rows:
            raw = (it.get("fields") or {}).get("RankingData") or ""
            for tok in str(raw).split(","):
                tok = tok.strip()
                if tok:
                    try:
                        scores.append(float(tok))
                    except ValueError:
                        pass
        return scores

    # -- the governed write ---------------------------------------------------

    def submit_worker_rating(self, employee_id, job_code, rating, comments=None,
                             reason=None, dry_run=True, _caller_upn=None,
                             user_role="default"):
        res = ToolResult(tool=self.name, function="submit_worker_rating",
                         args={"employee_id": employee_id, "job_code": job_code,
                               "rating": rating, "dry_run": bool(dry_run),
                               "reason": reason or ""})
        try:
            emp_id = int(employee_id)
            score = float(rating)
        except (TypeError, ValueError):
            res.caveats.append("employee_id must be an integer and rating numeric — refused.")
            return res
        if score != int(score) or not (RATING_MIN <= score <= RATING_MAX):
            res.caveats.append(f"rating must be a whole number in range "
                               f"{RATING_MIN}–{RATING_MAX} (got {rating!r}) — refused.")
            return res
        score = int(score)
        job_code = str(job_code or "").strip().upper()
        if not job_code:
            res.caveats.append("job_code is required — ask the user which job the rating is for.")
            return res

        try:
            import requests
            gh = self._graph_headers()

            people = self._graph_items(headers=gh, list_id=LIST_PEOPLE,
                                       flt=f"fields/OPMS eq {emp_id}",
                                       select="Title,OPMS")
            if not people:
                res.caveats.append(f"No PPL-People row with OPMS id {emp_id} — refused. "
                                   "Resolve the person with search_employee first.")
                return res
            person = people[0]
            person_id = int(person["id"])
            person_name = (person.get("fields") or {}).get("Title", "?")

            job_flt = "fields/JobID eq '" + job_code.replace("'", "''") + "'"
            jobs = self._graph_items(headers=gh, list_id=LIST_JOBS, flt=job_flt,
                                     select="JobID,Title")
            if not jobs:
                res.caveats.append(f"No JMS-Jobs row with JobID '{job_code}' — refused. "
                                   "Use the exact job code, e.g. 'SH-26036'.")
                return res
            job = jobs[0]
            job_title = (job.get("fields") or {}).get("Title", "")

            supervisor_id = None
            if _caller_upn:
                sups = self._graph_items(headers=gh, list_id=LIST_PEOPLE,
                                         flt="fields/WorkEmail eq '"
                                             + str(_caller_upn).replace("'", "''") + "'",
                                         select="Title,WorkEmail")
                if sups:
                    supervisor_id = int(sups[0]["id"])
                else:
                    res.caveats.append(f"Caller {_caller_upn} has no PPL-People row — "
                                       "Supervisor left blank on the transaction.")

            prior = self._person_scores(gh, person_id)
            projected = f"{(sum(prior) + score) / (len(prior) + 1):.2f}"

            opms_headers = {"Authorization": f"Bearer {self._token()}",
                            "Content-Type": "application/json"}
            before = self._get_employee(opms_headers, emp_id)
            current = before.get(RANKING_FIELD)

            row = {"employee": person_name, "opms_employee_id": emp_id,
                   "job": f"{job_code} {job_title}".strip(), "rating": score,
                   "prior_score_count": len(prior),
                   "current_opms_value": current, "projected_opms_average": projected}

            if dry_run:
                res.ok = True
                res.data, res.row_count = [row], 1
                res.confidence = "High"
                res.summary = (f"DRY RUN: would record rating {score}/{RATING_MAX} for "
                               f"{person_name} on {job_code} in PPL-RankingTX and push the "
                               f"recomputed average {projected} (from {len(prior) + 1} scores) "
                               f"to OPMS {RANKING_FIELD} (currently {current!r}). "
                               "No changes have been made. Execute with dry_run=false "
                               "(confirmation still required).")
                return res

            fields = {"Title": "LuLu Submit", "PersonLookupId": person_id,
                      "JobLookupId": int(job["id"]), "RankingData": str(score)}
            if comments:
                fields["Comments"] = str(comments)
            if supervisor_id is not None:
                fields["SupervisorLookupId"] = supervisor_id
            r = requests.post(f"{GRAPH_SITE}/lists/{LIST_RANKING_TX}/items",
                              headers=gh, json={"fields": fields}, timeout=60)
            r.raise_for_status()
            created_id = r.json().get("id")

            check = requests.get(f"{GRAPH_SITE}/lists/{LIST_RANKING_TX}/items/{created_id}"
                                 "?$expand=fields($select=RankingData)",
                                 headers=gh, timeout=60)
            check.raise_for_status()
            sp_verified = ((check.json().get("fields") or {}).get("RankingData") == str(score))

            fresh = self._person_scores(gh, person_id) or [float(score)]
            new_avg = f"{sum(fresh) / len(fresh):.2f}"
            r = requests.patch(f"{API_BASE}/employee/{emp_id}", headers=opms_headers,
                               json={RANKING_FIELD: new_avg}, timeout=30)
            r.raise_for_status()
            after = self._get_employee(opms_headers, emp_id)
            opms_verified = str(after.get(RANKING_FIELD)) == new_avg

            verified = sp_verified and opms_verified
            row.update(rankingtx_item_id=created_id, sp_verified=sp_verified,
                       opms_written_average=new_avg, opms_verified=opms_verified,
                       verified=verified)
            res.ok = verified
            res.data, res.row_count = [row], 1
            res.confidence = "High" if verified else "Low"
            res.summary = (f"Recorded rating {score}/{RATING_MAX} for {person_name} on "
                           f"{job_code} (PPL-RankingTX item {created_id}) and pushed new "
                           f"OPMS average {new_avg} ({current!r} before); verification "
                           f"{'PASSED' if verified else 'FAILED'} "
                           f"(SharePoint {'ok' if sp_verified else 'MISMATCH'}, "
                           f"OPMS {'ok' if opms_verified else 'MISMATCH'}).")
            if not verified:
                res.caveats.append("Do not retry blindly — check PPL-RankingTX and the "
                                   "OPMS record before any further write.")
            res.caveats.append("Tonight's 'Ranking Flow Updated' + 'Ranking Data OPMS' flows "
                               "recompute the same average from PPL-RankingTX, so the "
                               "scheduled pipeline stays consistent with this change.")
        except KeyError as e:
            res.caveats.append(f"Missing credential in environment: {e}")
        except Exception as e:
            res.caveats.append(f"Write-chain error: {type(e).__name__}: {e}")
        return res
