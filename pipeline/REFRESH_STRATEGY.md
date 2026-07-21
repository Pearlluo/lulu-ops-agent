# Lulu Lake — Refresh Strategy

Status: **v1 — current mode is FULL REFRESH**. This doc records the design so we can switch to
incremental (upsert + watermark + lookback) later without re-deciding anything.

---

## 1. Current mode: FULL REFRESH (overwrite)

`run_pipeline.py` re-extracts **everything** each run and **overwrites** the blob:
- Bronze: written per `ingest_date=YYYY-MM-DD`; same-day re-run overwrites that partition.
- Silver / Gold: parquet **fully replaced** each build.

**Implications (why this is currently fine and actually safest):**
- ✅ Edits to old records: re-fetched in full every run → always reflected.
- ✅ Deletes: a deleted record simply isn't in the new full pull → disappears.
- ✅ No watermark / lookback / upsert logic to get wrong.
- ⚠️ Cost: re-pulls all data (~15–20 min at current ~1 GB scale). Fine for now.

`config/watermarks.json` is written each run as a **forward-looking timestamp record**; it is
**not yet used to filter** anything.

---

## 2. When to switch to incremental

Switch when **any** of these is true:
- A full run exceeds ~30–60 min, or
- API call volume/throttling on OPMS or Graph becomes a problem, or
- Data volume grows materially (e.g. Bronze > 10–20 GB).

Until then, keep full refresh.

---

## 3. Incremental design (target)

**Merge mode = UPSERT by primary key. NEVER append.**
- For each incoming record: if PK exists → overwrite that row **only if** its watermark is newer;
  if PK is new → insert. (Implement as a parquet/delta MERGE, or read-modify-write per Silver table.)
- Appending would create duplicate PKs — explicitly disallowed.

**Watermark logic:**
- Persist per-source (ideally per-object) high-watermark = max(watermark column) of the last
  successful run, in `config/watermarks.json`.
- Next run pulls records with `watermark > (last_high_watermark - LOOKBACK)`.

**Lookback window = 48h (default).**
- Guards against clock skew, late-committed edits, and timezone boundaries.
- Cheap re-processing of a 48h tail; upsert makes re-pulling unchanged rows idempotent.

**Deletes (incremental cannot see them):**
- Run a **weekly FULL reconcile**: pull all PKs, compare to the table's PK set, mark missing PKs
  as deleted (soft-delete flag `is_deleted=true` + `deleted_at`), or hard-delete.
- OPMS alternative: `/change_log` events may include delete-type events — can drive targeted deletes.

---

## 4. Per-source reference (PK · watermark · mode)

### SharePoint (Microsoft Graph)
| Object class | PK | Watermark | Incremental param | Merge | Notes |
|---|---|---|---|---|---|
| All lists | `id` (item ID) → `bms_<entity>_id` | `Modified` | `$filter=fields/Modified gt '<ts>'` | upsert | needs indexed `Modified` + header `Prefer: HonorNonIndexedQueriesWarningMayFailRandomly` |
| Document libraries | `UniqueId` (GUID) | `Modified` | same | upsert | metadata only |

### OPMS (Data API)
| Object | PK | Watermark | Incremental param | Merge | Notes |
|---|---|---|---|---|---|
| `/change_log` | `id` | `created_date` | `created_after` + cursor `next` | upsert | **CDC backbone; only retained from 2025-01-08** |
| `/timesheets/entries` | `(timesheet_id, employee_id)` | `last_modified_date` | `modified_since` (full ISO) + cursor `after`, `page_size<=25` | upsert | |
| `/training/search` | `id` (training_record) | `last_modified_date` | `modified_since` + partition by `status` × `employee_ids` | upsert | no offset paging; CSV ids |
| `/roster` | `(employee_id, date)` | — (no modified filter) | re-pull rolling window (e.g. last 90d + future) | upsert | 90-day max window per call |
| `/joballocations` | `id` | — | rolling window | upsert | same 90-day cap |
| `/employee`, `/positions`, `/sites`, `/companies`, dims | `id` | **no server-side delta** | detect changed ids via `/change_log`, re-pull by id; else full-refresh (cheap) | upsert | |
| reference (`/airports`,`/countries`,`/genders`,…) | `id`/`Id` | none | full-refresh (tiny, monthly) | replace | |

---

## 5. Implementation checklist (when we build it)

1. **Watermark store**: read `config/watermarks.json` at start; write new high-watermarks at end (per object).
2. **extract_sharepoint_bms.py**: already supports `--since`; wire it to read watermark − lookback per list.
3. **extract_opms.py**: wire `change_log.created_after`, `timesheets.modified_since`, `training.modified_since`
   from watermark − lookback; keep roster/joballocations on a rolling window.
4. **Silver/Gold build → upsert**: change `build_*` from "overwrite parquet" to
   "read existing parquet → MERGE on PK (keep max watermark) → write". (Consider Delta Lake for real MERGE.)
5. **Weekly reconcile job**: separate schedule; full PK pull → mark/remove deleted.
6. **Keep a periodic full refresh** (e.g. weekly) as a safety net even after incremental is on.

---

## 6. Gotchas (from live extraction — do not relearn)
- OPMS `/change_log` history starts **2025-01-08**; anything older isn't in CDC.
- OPMS dimensions have **no modified filter** — incremental for them must go through `/change_log` or stay full-refresh.
- OPMS list-id params are **CSV**; `roster`/`joballocations` cap at **90 days/call**; `sites/employees` &
  `timesheets/entries` return `next_cursor` but page via the **`after`** param.
- `/training/search` has **no working offset** — must partition by `status` × `employee_ids`.
- SharePoint `$filter` on `Modified` needs the **Prefer header** above or it may fail on non-indexed paths.
- Permission-gated (won't refresh regardless of mode): OPMS `payslips`, `expense_claims`, `timesheets/configuration`.
