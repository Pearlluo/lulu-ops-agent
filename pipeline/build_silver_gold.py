"""
Build Silver (cleaned/flattened dims) and Gold (curated) Parquet from the Bronze NDJSON,
per config/entity_registry.yaml. Field names are taken from the ACTUAL extracted data,
not the V2-doc dictionary (real schema wins).

Silver:  dim_person, dim_position, dim_project, dim_client
Gold:    employee_profile (person x position), roster_summary (employee x date)

Writes data/silver/*.parquet and data/gold/*.parquet, then uploads to blob silver/ & gold/.

Run:
    pip install pandas pyarrow azure-storage-blob python-dotenv
    python build_silver_gold.py                 # build + upload
    python build_silver_gold.py --no-upload      # build locally only
"""

import argparse
import datetime
import glob
import json
from pathlib import Path

import pandas as pd

TODAY = pd.Timestamp(datetime.date.today())

SCRIPT_DIR = Path(__file__).resolve().parent
DATA_DIR = SCRIPT_DIR.parent.parent
BRONZE = DATA_DIR / "bronze"
SILVER = DATA_DIR / "silver"
GOLD = DATA_DIR / "gold"
SILVER.mkdir(exist_ok=True)
GOLD.mkdir(exist_ok=True)


# ---------- bronze readers ----------

def _latest(path_glob):
    hits = sorted(glob.glob(path_glob))
    return hits[-1] if hits else None


def read_opms(name):
    f = _latest(str(BRONZE / "opms" / name / "ingest_date=*" / "items.ndjson"))
    return [json.loads(l) for l in open(f, encoding="utf-8")] if f else []


def read_bms(module, listname):
    f = _latest(str(BRONZE / "bms" / module / listname / "ingest_date=*" / "items.ndjson"))
    if not f:
        return []
    out = []
    for l in open(f, encoding="utf-8"):
        rec = json.loads(l)
        out.append(rec.get("fields", rec))   # SharePoint payload lives under 'fields'
    return out


def g(d, *path):
    """safe nested get: g(rec,'gender','name')"""
    cur = d
    for k in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(k)
    return cur


def to_int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


# ---------- SILVER ----------

def build_dim_person():
    emp = read_opms("employee")
    ppl = read_bms("ppl", "PPL-People")

    # BMS PPL-People keyed by OPMS employee id
    bms_by_opms = {}
    for p in ppl:
        oid = to_int(p.get("OPMS"))
        if oid is not None:
            bms_by_opms[oid] = p

    rows = []
    for e in emp:
        oid = e.get("id")
        b = bms_by_opms.get(oid, {})
        rows.append({
            "opms_employee_id": oid,
            "bms_person_id": b.get("id"),
            "person_id": b.get("PersonID"),
            "first_name": e.get("first_name"),
            "last_name": e.get("last_name"),
            "middle_name": e.get("middle_name"),
            "preferred_name": e.get("preferred_name"),
            "date_of_birth": e.get("date_of_birth"),
            "gender_name": g(e, "gender", "name"),
            "nationality_name": g(e, "nationality", "name"),
            "email_work": e.get("work_email_address"),
            "phone_work": e.get("work_phone"),
            "home_airport_code": g(e, "home_airport", "code"),
            "arrangement_type": b.get("Arrangement"),
            "is_active": b.get("Active"),
            "bms_position_id": b.get("PositionLookupId"),
            "ops_section_id": b.get("OpsSectionLookupId"),
            "bms_supplier_id": b.get("SupplierLookupId"),
        })
    df = pd.DataFrame(rows)
    df.to_parquet(SILVER / "dim_person.parquet", index=False)
    return df


def build_dim_position():
    pos = read_opms("positions")
    bms = read_bms("ppl", "PPL-Positions")
    bms_by_opms = {to_int(p.get("OPMSID")): p for p in bms if to_int(p.get("OPMSID")) is not None}

    rows = []
    for p in pos:
        oid = p.get("id")
        b = bms_by_opms.get(oid, {})
        rows.append({
            "opms_position_id": oid,
            "bms_position_id": b.get("id"),
            "position_name": p.get("name"),
            "company_name": g(p, "company", "name"),
            "access_level_id": b.get("AccessLevelLookupId"),
            "is_active": b.get("ZActive"),
            "code1": p.get("code1"), "code2": p.get("code2"), "code3": p.get("code3"),
        })
    df = pd.DataFrame(rows)
    df.to_parquet(SILVER / "dim_position.parquet", index=False)
    return df


def build_dim_project():
    proj = read_bms("jms", "JMS-Projects")
    rows = [{
        "bms_project_id": p.get("id"),
        "project_name": p.get("Title"),
        "project_code": p.get("ProjectID"),
        "status": p.get("Status"),
        "is_active": p.get("Active"),
        "bms_client_id": p.get("ClientLookupId"),
        "ops_section_id": p.get("OpsSectionLookupId"),
        "project_start_date": p.get("ProjectStartDate"),
        "modified_at": p.get("Modified"),
    } for p in proj]
    df = pd.DataFrame(rows)
    df.to_parquet(SILVER / "dim_project.parquet", index=False)
    return df


def build_dim_client():
    cli = read_bms("jms", "JMS-Clients")
    rows = [{
        "bms_client_id": c.get("id"),
        "client_name": c.get("Title"),
        "client_short_name": c.get("ClientShortName"),
        "address": c.get("Address"),
        "vendor_number": c.get("VENDOR_x0023_"),
        "is_active": c.get("Active"),
        "modified_at": c.get("Modified"),
    } for c in cli]
    df = pd.DataFrame(rows)
    df.to_parquet(SILVER / "dim_client.parquet", index=False)
    return df


# ---------- SILVER: more dims ----------

def build_dim_company():
    rows = [{"company_id": c.get("id"), "company_name": c.get("name")} for c in read_opms("companies")]
    df = pd.DataFrame(rows); df.to_parquet(SILVER / "dim_company.parquet", index=False); return df


def build_dim_site():
    rows = [{"opms_site_id": s.get("id"), "site_name": s.get("name"),
             "company_id": s.get("company_id"), "area_id": s.get("area_id")} for s in read_opms("sites")]
    df = pd.DataFrame(rows); df.to_parquet(SILVER / "dim_site.parquet", index=False); return df


def build_dim_supplier():
    rows = [{"bms_supplier_id": s.get("id"), "supplier_name": s.get("Title"), "is_active": s.get("Active"),
             "abn": s.get("ABN"), "phone": s.get("Phone"), "email": s.get("EmailContact"),
             "website": s.get("Website"), "address": s.get("Adress"), "opms_supplier_ref": s.get("OPMS")}
            for s in read_bms("sms", "SMS-Suppliers")]
    df = pd.DataFrame(rows); df.to_parquet(SILVER / "dim_supplier.parquet", index=False); return df


def build_dim_competency():
    rows = []
    for rec in read_opms("competencies"):
        c = rec.get("competency", rec)            # records are wrapped as {competency:{...}}
        rows.append({"opms_competency_id": c.get("id"), "competency_name": c.get("name"),
                     "group_name": g(c, "group", "name"), "classified": c.get("classified"),
                     "validity_years": g(c, "validity_period", "years")})
    df = pd.DataFrame(rows); df.to_parquet(SILVER / "dim_competency.parquet", index=False); return df


def build_dim_asset():
    rows = [{"bms_asset_id": a.get("id"), "asset_name": a.get("Title"), "asset_id": a.get("AssetID"),
             "status": a.get("Status"), "model": a.get("Model"), "serial_number": a.get("SerialNumber"),
             "manufacturer_id": a.get("ManufacturerLookupId"), "location_id": a.get("LocationLookupId"),
             "asset_group_id": a.get("AssetGroupLookupId"), "is_active": a.get("Active"),
             "opms_asset_ref": a.get("OPMS"), "modified_at": a.get("Modified")}
            for a in read_bms("ams", "AMS-Assets")]
    df = pd.DataFrame(rows); df.to_parquet(SILVER / "dim_asset.parquet", index=False); return df


def build_dim_job():
    rows = [{"bms_job_id": j.get("id"), "job_title": j.get("Title"), "job_code": j.get("JobID"),
             "job_status": j.get("JobStatus"), "is_active": j.get("Active"),
             "bms_project_id": j.get("ProjectLookupId"), "bms_client_id": j.get("ClientShortNameLookupId"),
             "work_location_id": j.get("WorkLocationLookupId"), "mg_lead_person_id": j.get("MGLeadLookupId"),
             "ops_section_id": j.get("OpsSectionLookupId"), "is_overhead": j.get("Overhead"),
             "modified_at": j.get("Modified")}
            for j in read_bms("jms", "JMS-Jobs")]
    df = pd.DataFrame(rows); df.to_parquet(SILVER / "dim_job.parquet", index=False); return df


# ---------- SILVER: facts ----------

def build_fact_roster():
    rows = []
    for r in read_opms("roster"):
        emp = r.get("employee", {})
        for d in (r.get("rostered_days") or []):
            rows.append({"opms_employee_id": emp.get("id"), "roster_date": d.get("date"),
                         "position_id": g(d, "position", "id"), "position_name": g(d, "position", "name"),
                         "work_type_id": g(d, "work_type", "id"), "work_type_name": g(d, "work_type", "name"),
                         "allowance_count": len(d.get("allowances") or [])})
    df = pd.DataFrame(rows); df.to_parquet(SILVER / "fact_roster.parquet", index=False); return df


def build_fact_timesheet():
    rows = []
    for t in read_opms("timesheet_entries"):
        for e in (t.get("entries") or []):
            rows.append({"timesheet_id": t.get("id"), "site_id": t.get("site_id"),
                         "supervisor_id": t.get("supervisor_id"), "timesheet_date": t.get("date"),
                         "status": t.get("status"), "opms_employee_id": g(e, "employee", "id"),
                         "hours": e.get("value"), "level1_name": g(e, "level1", "name"),
                         "level2_name": g(e, "level2", "name"), "allowance_id": g(e, "allowance", "id"),
                         "modified_at": t.get("last_modified_date")})
    df = pd.DataFrame(rows); df.to_parquet(SILVER / "fact_timesheet.parquet", index=False); return df


def build_fact_training():
    rows = [{"training_record_id": t.get("id"), "opms_employee_id": g(t, "worker", "id"),
             "competency_id": g(t, "competency", "id"), "competency_name": g(t, "competency", "name"),
             "status": t.get("status"), "issue_date": t.get("issue_date"), "expiry_date": t.get("expiry_date"),
             "document_number": t.get("document_number"), "has_document": t.get("has_document"),
             "modified_at": t.get("last_modified_date")} for t in read_opms("training_search")]
    df = pd.DataFrame(rows); df.to_parquet(SILVER / "fact_training.parquet", index=False); return df


def _s(v):
    """stringify mixed-type values (str/int/dict) for a stable parquet column"""
    if v is None:
        return None
    if isinstance(v, str):
        return v
    if isinstance(v, (dict, list)):
        return json.dumps(v, ensure_ascii=False)
    return str(v)


def build_fact_change_log():
    rows = [{"change_log_id": c.get("id"), "created_at": c.get("created_date"),
             "created_by_id": g(c, "created_by", "id"), "event_type": c.get("event"),
             "opms_employee_id": g(c, "employee", "id"),
             "value_before": _s(g(c, "display_values", "before")),
             "value_after": _s(g(c, "display_values", "after"))} for c in read_opms("change_log")]
    df = pd.DataFrame(rows); df.to_parquet(SILVER / "fact_change_log.parquet", index=False); return df


def build_fact_inventory_txn():
    rows = [{"bms_inv_transaction_id": x.get("id"), "title": x.get("Title"), "units": x.get("Units"),
             "store_item_id": x.get("ItemLookupId"), "to_store_id": x.get("ToStoreLookupId"),
             "from_purchase_id": x.get("FromPurchaseLookupId"), "modified_at": x.get("Modified")}
            for x in read_bms("inv", "INV-Transactions")]
    df = pd.DataFrame(rows); df.to_parquet(SILVER / "fact_inventory_txn.parquet", index=False); return df


def build_fact_purchase():
    headers = {h.get("id"): h for h in read_bms("fin", "FIN-Puchases")}
    rows = []
    for li in read_bms("fin", "FIN-PurchaseItems"):
        h = headers.get(to_int(li.get("PurchaseLookupId")), {}) or headers.get(li.get("PurchaseLookupId"), {})
        rows.append({"purchase_item_id": li.get("id"), "purchase_id": li.get("PurchaseLookupId"),
                     "store_item_id": li.get("StoreItemLookupId"), "unit_count": li.get("UnitCount"),
                     "unit_cost": li.get("UnitCost"), "invoice_date": h.get("InvDate"),
                     "supplier_id": h.get("SupplierLookupId"), "inv_number": h.get("InvNumber"),
                     "inv_amount": h.get("InvAmount")})
    df = pd.DataFrame(rows); df.to_parquet(SILVER / "fact_purchase.parquet", index=False); return df


# ---------- GOLD ----------

def lookup_title(flatname):
    """id -> Title map from a silver/flat mirror table (for resolving LookupIds to names)."""
    p = SILVER / "flat" / f"{flatname}.parquet"
    if not p.exists():
        return {}
    df = pd.read_parquet(p)
    if "id" not in df.columns or "Title" not in df.columns:
        return {}
    return {str(k): v for k, v in zip(df["id"], df["Title"])}


def build_employee_profile(dim_person, dim_position, dim_supplier):
    # position name (only real BMS-id positions, else null-keys cross-match under 'str' dtype)
    pos = (dim_position[dim_position["bms_position_id"].notna()]
           [["bms_position_id", "position_name", "company_name"]].drop_duplicates("bms_position_id"))
    df = dim_person.merge(pos, on="bms_position_id", how="left")
    # supplier name
    df["bms_supplier_id"] = pd.to_numeric(df["bms_supplier_id"], errors="coerce")
    sup = dim_supplier[["bms_supplier_id", "supplier_name"]].copy()
    sup["bms_supplier_id"] = pd.to_numeric(sup["bms_supplier_id"], errors="coerce")
    sup = sup.dropna(subset=["bms_supplier_id"])
    df = df.merge(sup, on="bms_supplier_id", how="left")
    # ops section name (resolve LookupId via silver/flat)
    ops = lookup_title("sp__SYS-OpsSections")
    df["ops_section_name"] = df["ops_section_id"].astype("string").map(ops)
    df.to_parquet(GOLD / "employee_profile.parquet", index=False)
    return df


def build_training_compliance(fact_training, dim_person, dim_competency):
    df = fact_training.copy()
    df["competency_id"] = pd.to_numeric(df["competency_id"], errors="coerce")
    df = df.merge(dim_person[["opms_employee_id", "first_name", "last_name", "arrangement_type", "is_active"]],
                  on="opms_employee_id", how="left")
    cc = dim_competency.rename(columns={"opms_competency_id": "competency_id"})[
        ["competency_id", "group_name", "validity_years", "classified"]]
    cc["competency_id"] = pd.to_numeric(cc["competency_id"], errors="coerce")
    df = df.merge(cc, on="competency_id", how="left")
    exp = pd.to_datetime(df["expiry_date"], errors="coerce")
    df["days_to_expiry"] = (exp - TODAY).dt.days
    df["is_expired"] = df["days_to_expiry"] < 0
    df["is_expiring_soon"] = (df["days_to_expiry"] >= 0) & (df["days_to_expiry"] <= 90)
    df.to_parquet(GOLD / "training_compliance.parquet", index=False)
    return df


def build_timesheet_summary(fact_timesheet, dim_person, dim_site):
    df = fact_timesheet.copy()
    df["hours"] = pd.to_numeric(df["hours"], errors="coerce")
    df["month"] = pd.to_datetime(df["timesheet_date"], errors="coerce").dt.to_period("M").astype("string")
    grp = (df.groupby(["opms_employee_id", "site_id", "month"], dropna=False)
             .agg(total_hours=("hours", "sum"), entry_count=("hours", "size")).reset_index())
    grp = grp.merge(dim_person[["opms_employee_id", "first_name", "last_name"]], on="opms_employee_id", how="left")
    site = dim_site[["opms_site_id", "site_name"]].rename(columns={"opms_site_id": "site_id"})
    grp = grp.merge(site, on="site_id", how="left")
    grp.to_parquet(GOLD / "timesheet_summary.parquet", index=False)
    return grp


def _truthy(s):
    return pd.Series(s).map(lambda v: str(v).lower() in ("true", "1", "yes")).astype(bool)


def build_project_job_summary(dim_project, dim_client, dim_job):
    j = dim_job.copy()
    j["bms_project_id"] = pd.to_numeric(j["bms_project_id"], errors="coerce")
    j["active_flag"] = _truthy(j["is_active"])
    agg = j.groupby("bms_project_id").agg(job_count=("bms_job_id", "size"),
                                          active_job_count=("active_flag", "sum")).reset_index()
    df = dim_project.copy()
    df["bms_project_id"] = pd.to_numeric(df["bms_project_id"], errors="coerce")
    df["bms_client_id"] = pd.to_numeric(df["bms_client_id"], errors="coerce")
    df = df.merge(agg, on="bms_project_id", how="left")
    cl = dim_client[["bms_client_id", "client_name"]].copy()
    cl["bms_client_id"] = pd.to_numeric(cl["bms_client_id"], errors="coerce")
    df = df.merge(cl, on="bms_client_id", how="left")
    df.to_parquet(GOLD / "project_job_summary.parquet", index=False)
    return df


# --- domain: Assets ---
def build_asset_register(dim_asset):
    df = dim_asset.copy()
    mans, locs, grps = lookup_title("sp__AMS-Manufacturers"), lookup_title("sp__AMS-Locations"), lookup_title("sp__AMS-AssetGroup")
    df["manufacturer_name"] = df["manufacturer_id"].astype("string").map(mans)
    df["location_name"] = df["location_id"].astype("string").map(locs)
    df["asset_group_name"] = df["asset_group_id"].astype("string").map(grps)
    df.to_parquet(GOLD / "asset_register.parquet", index=False)
    return df


# --- domain: HSEQ / safety ---
def build_hseq_register():
    p = SILVER / "flat" / "sp__EAM-IssuesAndActions.parquet"
    if not p.exists():
        return pd.DataFrame()
    src = pd.read_parquet(p)
    want = ["id", "Title", "ItemType", "Status", "ActionPriority", "Investigation",
            "ActionStartDate", "ActionDueDate", "ActionCompletedDate", "IssueDetail",
            "ActionPlan", "CauseDetail", "CompletionNotes", "Modified", "Created"]
    cols = [c for c in want if c in src.columns]
    df = src[cols].rename(columns={"id": "bms_issue_id", "Title": "issue_title", "ItemType": "issue_type",
                                   "Status": "issue_status", "ActionPriority": "action_priority",
                                   "ActionDueDate": "action_due_date", "ActionCompletedDate": "action_completed_date"})
    if "action_due_date" in df.columns:
        due = pd.to_datetime(df["action_due_date"], errors="coerce", utc=True).dt.tz_localize(None)
        done = pd.to_datetime(df.get("action_completed_date"), errors="coerce", utc=True).dt.tz_localize(None)
        df["is_open"] = done.isna()
        df["is_overdue"] = df["is_open"] & (due < TODAY)
    df.to_parquet(GOLD / "hseq_register.parquet", index=False)
    return df


# --- domain: Suppliers ---
def build_supplier_summary(dim_supplier, dim_person):
    df = dim_supplier.copy()
    df["bms_supplier_id"] = pd.to_numeric(df["bms_supplier_id"], errors="coerce")
    wk = dim_person.copy()
    wk["bms_supplier_id"] = pd.to_numeric(wk["bms_supplier_id"], errors="coerce")
    cnt = wk.dropna(subset=["bms_supplier_id"]).groupby("bms_supplier_id").size().rename("worker_count").reset_index()
    df = df.merge(cnt, on="bms_supplier_id", how="left")
    df["worker_count"] = df["worker_count"].fillna(0).astype(int)
    df.to_parquet(GOLD / "supplier_summary.parquet", index=False)
    return df


# --- domain: Finance / purchasing ---
def build_purchase_summary(fact_purchase):
    df = fact_purchase.copy()
    for c in ("unit_count", "unit_cost"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df["line_total"] = df["unit_count"] * df["unit_cost"]
    items = lookup_title("sp__FIN-StoreItems")
    suppliers = lookup_title("sp__SMS-Suppliers")
    grp = (df.groupby("purchase_id", dropna=False)
             .agg(supplier_id=("supplier_id", "first"), inv_number=("inv_number", "first"),
                  inv_amount=("inv_amount", "first"), invoice_date=("invoice_date", "first"),
                  line_count=("purchase_item_id", "size"), computed_total=("line_total", "sum")).reset_index())
    grp["supplier_name"] = grp["supplier_id"].astype("string").map(suppliers)
    grp.to_parquet(GOLD / "purchase_summary.parquet", index=False)
    return grp


def read_flat(name):
    p = SILVER / "flat" / f"{name}.parquet"
    return pd.read_parquet(p) if p.exists() else pd.DataFrame()


def _pick(df, mapping):
    """select+rename only columns that exist"""
    cols = {k: v for k, v in mapping.items() if k in df.columns}
    return df[list(cols)].rename(columns=cols)


def _write_gold(df, name):
    """Write a gold parquet, but refuse to clobber a good file with a column-less frame.

    When an upstream source comes back empty (missing silver flat, Xero 403/throttle),
    a builder can produce a 0-column DataFrame. Writing that yields a parquet DuckDB
    can't even build a view over, which crashes the whole app. Skip the write instead so
    the previous good data survives until the next healthy run.
    """
    out = GOLD / f"{name}.parquet"
    if df is None or len(df.columns) == 0:
        print(f"  [skip] {name}: empty/column-less result — keeping existing {out.name}")
        return df
    df.to_parquet(out, index=False)
    return df


# --- domain: Workforce / site assignment ---
def build_site_assignment():
    df = read_flat("opms__sites_employees")
    out = _pick(df, {"employee.id": "opms_employee_id", "employee.first_name": "first_name",
                     "employee.last_name": "last_name", "position.id": "opms_position_id",
                     "position.name": "position_name", "site.id": "opms_site_id",
                     "site.name": "site_name", "team.name": "team_name"})
    return _write_gold(out, "site_assignment")


# --- domain: Inventory ---
def build_inventory_summary():
    df = read_flat("sp__INV-Stores")
    out = _pick(df, {"id": "bms_store_id", "Title": "item_name", "Stock": "stock", "Active": "is_active",
                     "StoreLocationLookupId": "location_id", "CategoryLookupId": "category_id",
                     "EffectiveDate": "effective_date"})
    locs, cats = lookup_title("sp__INV-StoreLocations"), lookup_title("sp__INV-Categories")
    if "location_id" in out:
        out["location_name"] = out["location_id"].astype("string").map(locs)
    if "category_id" in out:
        out["category_name"] = out["category_id"].astype("string").map(cats)
    return _write_gold(out, "inventory_summary")


def _lookup_key(s):
    """LookupId column -> clean string key ('48', <NA>) regardless of int/float/str dtype."""
    return pd.to_numeric(s, errors="coerce").astype("Int64").astype("string")


def build_ppe_transactions():
    """PPE/workwear movement ledger, one row per INV-Transactions line, fully resolved.
    txn_kind: sign_out (to a person/job) / stock_in (purchase) / store_move / other."""
    df = read_flat("sp__INV-Transactions")
    out = _pick(df, {"id": "bms_txn_id", "Title": "title", "Units": "units",
                     "ItemLookupId": "item_id", "FromStoreLookupId": "from_location_id",
                     "ToStoreLookupId": "to_location_id", "PersonLookupId": "person_id",
                     "ToJobLookupId": "job_id", "FromPurchaseLookupId": "purchase_id",
                     "CommitTime": "commit_time", "Created": "created_at"})
    if len(out.columns) == 0:
        return _write_gold(out, "ppe_transactions")

    ts = pd.to_datetime(out["commit_time"], errors="coerce", utc=True)
    ts = ts.fillna(pd.to_datetime(out["created_at"], errors="coerce", utc=True))
    perth = ts.dt.tz_convert("Australia/Perth")
    out["txn_date"] = perth.dt.strftime("%Y-%m-%d")
    out["txn_month"] = perth.dt.strftime("%Y-%m")

    person = _lookup_key(out["person_id"])
    purchase = _lookup_key(out["purchase_id"])
    title = out["title"].astype("string").fillna("")
    out["txn_kind"] = "other"
    out.loc[title.str.startswith("Stores Moved"), "txn_kind"] = "store_move"
    out.loc[purchase.notna() | title.str.startswith("Purchase to store"), "txn_kind"] = "stock_in"
    out.loc[person.notna(), "txn_kind"] = "sign_out"

    locs = lookup_title("sp__INV-StoreLocations")
    out["from_location"] = _lookup_key(out["from_location_id"]).map(locs)
    out["to_location"] = _lookup_key(out["to_location_id"]).map(locs)
    out["person_name"] = person.map(lookup_title("sp__PPL-People"))

    items = read_flat("sp__FIN-StoreItems")
    if "id" in items.columns:
        ikey = items["id"].astype("string")
        out["item_name"] = _lookup_key(out["item_id"]).map(dict(zip(ikey, items.get("Title"))))
        out["item_code"] = _lookup_key(out["item_id"]).map(dict(zip(ikey, items.get("Code"))))

    jobs = read_flat("sp__JMS-Jobs")
    if "id" in jobs.columns:
        jkey = jobs["id"].astype("string")
        out["job_code"] = _lookup_key(out["job_id"]).map(dict(zip(jkey, jobs.get("Title"))))
        projects = lookup_title("sp__JMS-Projects")
        job_project = {j: projects.get(str(p)) for j, p in
                       zip(jkey, _lookup_key(jobs.get("ProjectLookupId")))}
        out["project_name"] = _lookup_key(out["job_id"]).map(job_project)

    out = out.drop(columns=["commit_time", "created_at", "purchase_id"], errors="ignore")
    return _write_gold(out, "ppe_transactions")


# --- domain: Audit / change activity ---
def build_audit_activity(fact_change_log, dim_person):
    df = fact_change_log.merge(dim_person[["opms_employee_id", "first_name", "last_name"]],
                               on="opms_employee_id", how="left")
    df.to_parquet(GOLD / "audit_activity.parquet", index=False)
    return df


# --- domain: Project / job detail (fully resolved) ---
def build_job_detail(dim_job, dim_project, dim_client, dim_person):
    df = dim_job.copy()
    for c in ("bms_project_id", "bms_client_id", "mg_lead_person_id"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    pr = dim_project[["bms_project_id", "project_name"]].copy()
    pr["bms_project_id"] = pd.to_numeric(pr["bms_project_id"], errors="coerce")
    df = df.merge(pr, on="bms_project_id", how="left")
    cl = dim_client[["bms_client_id", "client_name"]].copy()
    cl["bms_client_id"] = pd.to_numeric(cl["bms_client_id"], errors="coerce")
    df = df.merge(cl, on="bms_client_id", how="left")
    lead = dim_person[["opms_employee_id", "first_name", "last_name"]].rename(
        columns={"opms_employee_id": "mg_lead_person_id", "first_name": "lead_first_name",
                 "last_name": "lead_last_name"})
    df = df.merge(lead, on="mg_lead_person_id", how="left")
    wl, ops = lookup_title("sp__JMS-WorkLocations"), lookup_title("sp__SYS-OpsSections")
    df["work_location_name"] = df["work_location_id"].astype("string").map(wl)
    df["ops_section_name"] = df["ops_section_id"].astype("string").map(ops)
    df.to_parquet(GOLD / "job_detail.parquet", index=False)
    return df


# --- domain: Hardware ---
def build_hardware_register():
    df = read_flat("sp__AMS-Hardware")
    out = _pick(df, {"id": "bms_hardware_id", "Title": "hardware_name", "Code": "code", "Status": "status",
                     "InStock": "in_stock", "HirePrice": "hire_price", "Active": "is_active",
                     "CategoryLookupId": "category_id"})
    cats = lookup_title("sp__AMS-HardwareCategories")
    if "category_id" in out:
        out["category_name"] = out["category_id"].astype("string").map(cats)
    return _write_gold(out, "hardware_register")


# --- domain: Commercial rates ---
def build_rate_card(dim_project):
    df = read_flat("sp__JMS-Rates")
    out = _pick(df, {"id": "bms_rate_id", "Title": "rate_title", "ProjectLookupId": "bms_project_id",
                     "PositionLookupId": "bms_position_id", "DayShift": "day_shift_rate",
                     "NightShift": "night_shift_rate", "ProjectID": "project_code"})
    if "bms_project_id" in out:
        out["bms_project_id"] = pd.to_numeric(out["bms_project_id"], errors="coerce")
        pr = dim_project[["bms_project_id", "project_name"]].copy()
        pr["bms_project_id"] = pd.to_numeric(pr["bms_project_id"], errors="coerce")
        out = out.merge(pr, on="bms_project_id", how="left")
    return _write_gold(out, "rate_card")


# --- domain: Licences ---
def build_licence_register():
    df = read_flat("sp__PPL-Licences")
    out = _pick(df, {"id": "bms_licence_id", "FirstName": "first_name", "LastName": "last_name",
                     "OPMS": "opms_employee_id", "Licence": "licence", "LicenceItems": "licence_items",
                     "Mobile": "mobile", "WorkEmail": "email_work", "PositionLookupId": "bms_position_id"})
    return _write_gold(out, "licence_register")


# --- domain: Worker ranking ---
def build_worker_ranking():
    df = read_flat("sp__PPL-Ranking")
    out = _pick(df, {"id": "bms_ranking_id", "FirstName": "first_name", "LastName": "last_name",
                     "OPMS": "opms_employee_id", "SiteScoreIndex": "site_score", "MobScoreIndex": "mob_score",
                     "MobScore0": "mob_score_raw", "MobTest": "mob_test"})
    return _write_gold(out, "worker_ranking")


def build_roster_summary():
    resolve, _ = _get_client_resolver()

    def _client(project_str):
        hit = resolve(project_str)
        return (hit.get("client_code") or hit.get("client_name")) if hit else None

    rows = []
    # OPMS roster: explode rostered_days; project lives in resource_request_allocations
    # (port of the GitHub rates pipeline: OPMS resource_request.project is the project name)
    for r in read_opms("roster"):
        emp = r.get("employee", {})
        for d in (r.get("rostered_days") or []):
            project = None
            for alloc in (d.get("resource_request_allocations") or []):
                project = project or g(alloc, "resource_request", "project")
            rows.append({
                "opms_employee_id": emp.get("id"),
                "first_name": emp.get("first_name"),
                "last_name": emp.get("last_name"),
                "roster_date": d.get("date"),
                "position_name": g(d, "position", "name"),
                "work_type_name": g(d, "work_type", "name"),
                "project_name": project,
                "client_name": _client(project),
                "hours": None,
                "source": "opms",
            })
    # BMS PPL-Rosters
    for p in read_bms("ppl", "PPL-Rosters"):
        rows.append({
            "opms_employee_id": to_int(p.get("OPMS")),
            "first_name": p.get("First_x0020_Name"),
            "last_name": p.get("Last_x0020_Name"),
            "roster_date": p.get("Date_x0020_From"),
            "position_name": p.get("Position"),
            "work_type_name": p.get("WorkType"),
            "project_name": p.get("Project"),
            "client_name": _client(p.get("Project")),
            "hours": p.get("Hours"),
            "source": "bms",
        })
    df = pd.DataFrame(rows)
    # normalise roster_date to a Perth-local date. NOTE: per-value parse via _perth_dt —
    # a vectorised pd.to_datetime silently coerced ALL BMS ISO+Z dates to NaT
    # (mixed tz-aware/naive column), which hid every BMS roster row from date filters
    # (caught by data_quality_sentinel 2026-06-11).
    def _to_date(v):
        d = _perth_dt(v)
        return d.date().isoformat() if d else None
    df["roster_date"] = df["roster_date"].map(_to_date).astype("string")
    df.to_parquet(GOLD / "roster_summary.parquet", index=False)
    return df


# --- domain: Project / OPMS<->BMS bridge ---
# Port of the GitHub repo `AdminLuo-working-UpdateTimesheet-Projects-Rates-` (Sharepoint_contracts.py):
# an OPMS project string carries a job-code prefix ("SH-25006 - July FPS" -> "SH-25006") which keys
# into BMS: JMS-Jobs.JobID -> ProjectLookupId -> JMS-Projects (ATitle = client code "C0002-Newmont").
# Rows the prefix can't resolve fall back to the hand-maintained Project_Client_Map.csv
# (synced from that repo / blob timesheethour/config) — same order as the automation: CSV first, then JMS.
import re as _re

PROJECT_PREFIX_RE = _re.compile(r"^([A-Z]{2,4}-\d{4,6})", _re.IGNORECASE)
PROJECT_MAP_CSV = DATA_DIR / "config" / "Project_Client_Map.csv"


def _extract_job_prefix(s):
    """'SH-25006 - July FPS' -> 'SH-25006'"""
    m = PROJECT_PREFIX_RE.match(str(s or "").strip())
    return m.group(1).upper() if m else None


def _make_client_resolver():
    """Returns (resolve(project_str) -> dict|None, bridge_rows for the gold table)."""
    projects = {str(p.get("id")): p for p in read_bms("jms", "JMS-Projects")}
    clients = {str(c.get("id")): c for c in read_bms("jms", "JMS-Clients")}

    bridge_rows, job_map = [], {}
    for j in read_bms("jms", "JMS-Jobs"):
        prefix = _extract_job_prefix(j.get("JobID"))
        proj = projects.get(str(j.get("ProjectLookupId") or ""))
        if not prefix or not proj:
            continue
        cli = clients.get(str(proj.get("ClientLookupId") or ""), {})
        row = {
            "job_code": prefix,
            "job_title": j.get("Title"),
            "bms_project_id": proj.get("id"),
            "project_name": proj.get("Title"),
            "client_code": proj.get("ATitle"),
            "client_name": cli.get("Title"),
            "opms_project_name": None,
            "source": "jms_bridge",
        }
        job_map.setdefault(prefix, row)
        bridge_rows.append(row)

    manual_map = {}
    if PROJECT_MAP_CSV.exists():
        mp = pd.read_csv(PROJECT_MAP_CSV)
        for _, r in mp.iterrows():
            name = str(r["resourceRequestProject"]).strip()
            client = str(r["resourceRequestClient"]).strip()
            if name and client:
                row = {"job_code": _extract_job_prefix(name), "job_title": None,
                       "bms_project_id": None, "project_name": None, "client_code": client,
                       "client_name": None, "opms_project_name": name, "source": "manual_map"}
                manual_map[name] = row
                bridge_rows.append(row)

    def resolve(project_str):
        s = str(project_str or "").strip()
        if not s:
            return None
        if s in manual_map:                                   # automation order: CSV map first
            return manual_map[s]
        prefix = _extract_job_prefix(s)
        return job_map.get(prefix) if prefix else None

    return resolve, bridge_rows


_client_resolver_cache = None


def _get_client_resolver():
    global _client_resolver_cache
    if _client_resolver_cache is None:
        _client_resolver_cache = _make_client_resolver()
    return _client_resolver_cache


def build_project_bridge():
    _, bridge_rows = _get_client_resolver()
    df = pd.DataFrame(bridge_rows)
    df.to_parquet(GOLD / "project_bridge.parquet", index=False)
    return df


# --- domain: Time / weekly timesheet ---
# Port of the GitHub automation `acme_weeklytimesheet_automation` (Timesheethours1/2.py):
#   1. OPMS actual hours (timesheet entries summed per worker x Perth date) REPLACE the roster
#      hours when a match exists — same as the automation's weekly PPL-Rosters.Hours write-back,
#      but computed here straight from Bronze so it stays correct even if that write-back lags.
#   2. Gap hours come from the PPL-Timesheets sign-out/sign-in form: pair each Sign Out with the
#      next Sign In per worker, ignore reversed pairs and cross-day pairs > 6h, split at midnight.
#   3. actual_hours = max(0, roster_hours - gap_hours); Z.* plant lines at TRANSPORT & HIRE excluded.
MAX_CROSS_DAY_GAP_HOURS = 6


def _perth_dt(value):
    """SharePoint datetime -> Perth-local naive datetime (automation parity: UTC 'Z' + 8h)."""
    if value is None:
        return None
    s = str(value).strip()
    if not s or s.lower() == "nan":
        return None
    if "T" in s and s.endswith("Z"):
        dt = pd.to_datetime(s, errors="coerce", utc=True)
        if pd.isna(dt):
            return None
        return (dt.tz_convert(None) + pd.Timedelta(hours=8)).to_pydatetime()
    dt = pd.to_datetime(s, errors="coerce", dayfirst="/" in s)   # 14/04/2026 vs 2026-04-14
    return None if pd.isna(dt) else dt.to_pydatetime()


def _norm_opms(value):
    """Match the automation's key normalisation: '6.0' and '6' are the same worker."""
    s = str(value or "").strip()
    if not s or s.lower() == "nan":
        return None
    try:
        return str(int(float(s)))
    except ValueError:
        return s


def _build_gap_map(ts_items):
    """{(opms, date): gap_hours} from PPL-Timesheets Sign Out -> Sign In pairs."""
    by_opms = {}
    for f in ts_items:
        opms = _norm_opms(f.get("OPMS"))
        status = str(f.get("Status") or "").strip().lower()
        dt = _perth_dt(f.get("Date"))
        if not opms or status not in ("sign out", "sign in") or dt is None:
            continue
        by_opms.setdefault(opms, []).append((dt, status))

    gap = {}
    for opms, recs in by_opms.items():
        recs.sort(key=lambda x: x[0])
        open_out = None
        for dt, status in recs:
            if status == "sign out":
                open_out = dt
                continue
            if open_out is None:                      # sign in with no open sign out
                continue
            hours = (dt - open_out).total_seconds() / 3600
            if hours <= 0 or (open_out.date() != dt.date() and hours > MAX_CROSS_DAY_GAP_HOURS):
                open_out = None
                continue
            day = open_out.date()
            while day <= dt.date():                   # split the gap at midnight per day
                seg_start = max(open_out, datetime.datetime.combine(day, datetime.time.min))
                seg_end = min(dt, datetime.datetime.combine(day + datetime.timedelta(days=1),
                                                            datetime.time.min))
                h = (seg_end - seg_start).total_seconds() / 3600
                if h > 0:
                    gap[(opms, day)] = gap.get((opms, day), 0.0) + h
                day += datetime.timedelta(days=1)
            open_out = None
    return {k: round(v, 2) for k, v in gap.items()}


def _build_opms_hours_map():
    """{(opms, date): hours} — OPMS timesheet entry values summed per worker per day."""
    hours = {}
    for ts in read_opms("timesheet_entries"):
        d = _perth_dt(ts.get("date"))
        if d is None:
            continue
        day = d.date()
        for entry in (ts.get("entries") or []):
            opms = _norm_opms(g(entry, "employee", "id"))
            if not opms:
                continue
            try:
                v = float(entry.get("value") or 0)
            except (TypeError, ValueError):
                v = 0.0
            hours[(opms, day)] = hours.get((opms, day), 0.0) + v
    return {k: round(v, 2) for k, v in hours.items()}


def build_weekly_timesheet():
    site_map = {str(s.get("id")): s.get("Title") for s in read_bms("sys", "SYS-OpsSections")}
    supplier_map = {str(s.get("id")): s.get("Title") for s in read_bms("sms", "SMS-Suppliers")}
    gap_map = _build_gap_map(read_bms("ppl", "PPL-Timesheets"))
    opms_hours = _build_opms_hours_map()
    resolve, _ = _get_client_resolver()

    # supplier fallback — port of ExtraUpdateSupplier.py (acme_weeklytimesheet_automation):
    # PPL-People keyed by normalised OPMS ('6.0'=='6') -> SupplierLookupId, first occurrence
    # wins; used when the roster row's own supplier is missing (the production write-back
    # only touches the last 9 days and lags when the Azure Function is down).
    people_supplier = {}
    for p in read_bms("ppl", "PPL-People"):
        opms = _norm_opms(p.get("OPMS"))
        sid = str(p.get("SupplierLookupId") or p.get("SupplierId") or "").strip()
        if opms and sid and opms not in people_supplier:
            people_supplier[opms] = sid

    rows = []
    for p in read_bms("ppl", "PPL-Rosters"):
        opms = _norm_opms(p.get("OPMS"))
        d = _perth_dt(p.get("Date_x0020_From"))
        if not opms or d is None:
            continue
        work_date = d.date()
        position = str(p.get("Position") or "").strip()
        site_name = site_map.get(str(p.get("SiteLookupId") or ""), "") or ""
        if position.upper().startswith("Z.") and site_name.strip().upper() == "TRANSPORT & HIRE":
            continue                                  # plant/asset line, not a person
        key = (opms, work_date)
        if key in opms_hours:                         # OPMS actuals replace roster hours (step 1)
            roster_hours, hours_source = opms_hours[key], "opms"
        else:
            try:
                roster_hours, hours_source = float(p.get("Hours")), "bms_writeback"
            except (TypeError, ValueError):
                roster_hours, hours_source = 0.0, "none"
        gap_hours = gap_map.get(key, 0.0)
        work_type = str(p.get("WorkType") or "").strip()
        bridge_hit = resolve(p.get("Project"))
        rows.append({
            "opms_employee_id": to_int(opms),
            "first_name": p.get("First_x0020_Name"),
            "last_name": p.get("Last_x0020_Name"),
            "position_name": position or None,
            "project_name": p.get("Project"),
            "client_name": (bridge_hit.get("client_code") or bridge_hit.get("client_name")) if bridge_hit else None,
            "site_name": site_name or None,
            "supplier_name": (supplier_map.get(str(p.get("SupplierLookupId") or ""))
                              or supplier_map.get(people_supplier.get(opms, ""))
                              or None),
            "work_date": work_date.isoformat(),
            "shift_type": "NS" if work_type.upper() == "NIGHT SHIFT" else "DS",
            "roster_hours": round(roster_hours, 2),
            "gap_hours": round(gap_hours, 2),
            "actual_hours": round(max(0.0, roster_hours - gap_hours), 2),
            "hours_source": hours_source,
        })
    df = pd.DataFrame(rows)
    df.to_parquet(GOLD / "weekly_timesheet.parquet", index=False)
    return df


# --- domain: Commercial / Xero revenue (from OpsDB Azure SQL mirror) ---
# ACCREC = sales invoices (revenue to Acme); ACCPAY = supplier bills.
# Reference often carries the job code ("SH-26036 | 4500553525") -> project_bridge link.
# NOTE: the Xero sync currently ends ~2026-04; the daily brief carries a staleness rule.

def read_xero(name):
    f = _latest(str(BRONZE / "xero" / name / "ingest_date=*" / "items.ndjson"))
    return [json.loads(l) for l in open(f, encoding="utf-8")] if f else []


def build_invoice_register():
    """One row per Xero invoice, with the job code mined from Reference/InvoiceNumber."""
    resolve, _ = _get_client_resolver()
    rows = []
    for inv in read_xero("invoices"):
        if inv.get("Status") in ("DELETED", "VOIDED"):
            continue
        ref = str(inv.get("Reference") or "")
        # job codes come from the Reference ONLY — invoice numbers (INV-xxxx) and
        # purchase orders (PO-xxxx) match the prefix regex but are NOT job codes
        job_code = _extract_job_prefix(ref)
        if job_code and job_code.split("-")[0] in ("INV", "PO", "GST", "RCTI"):
            job_code = None
        hit = resolve(job_code) if job_code else None
        date = str(inv.get("DateString") or "")[:10] or None
        rows.append({
            "invoice_id": inv.get("InvoiceID"),
            "invoice_number": inv.get("InvoiceNumber"),
            "invoice_type": inv.get("Type"),                  # ACCREC=revenue, ACCPAY=bill
            "status": inv.get("Status"),
            "contact_name": inv.get("Contact_Name"),
            "reference": ref or None,
            "job_code": job_code,
            "bridge_client": (hit.get("client_code") or hit.get("client_name")) if hit else None,
            "invoice_date": date,
            "month": date[:7] if date else None,
            "due_date": str(inv.get("DueDateString") or "")[:10] or None,
            "subtotal": float(inv.get("SubTotal") or 0),
            "total": float(inv.get("Total") or 0),
            "amount_paid": float(inv.get("AmountPaid") or 0),
            "amount_due": float(inv.get("AmountDue") or 0),
        })
    df = pd.DataFrame(rows)
    return _write_gold(df, "invoice_register")


def build_revenue_summary():
    """Client x month revenue rollup (ACCREC, AUTHORISED/PAID only)."""
    # defensive: invoices can come back empty (e.g. Xero 403/throttle/no API access) -> empty
    # frame has no columns, and _write_gold then skips writing — on a fresh cloud container
    # there is no previous invoice_register.parquet at all. Don't crash the whole pipeline
    # (it would kill every later step incl. Link Health); emit an empty revenue_summary and move on.
    cols = ["contact_name", "month", "invoices", "invoiced", "paid", "outstanding"]
    reg_path = GOLD / "invoice_register.parquet"
    if not reg_path.exists():
        print("  [skip] revenue_summary: invoice_register.parquet missing (Xero source empty) — writing empty table")
        out = pd.DataFrame(columns=cols)
        out.to_parquet(GOLD / "revenue_summary.parquet", index=False)
        return out
    reg = pd.read_parquet(reg_path)
    if reg.empty or "invoice_type" not in reg.columns:
        out = pd.DataFrame(columns=cols)
        out.to_parquet(GOLD / "revenue_summary.parquet", index=False)
        return out
    rev = reg[(reg.invoice_type == "ACCREC") & (reg.status.isin(["AUTHORISED", "PAID"]))]
    out = (rev.groupby(["contact_name", "month"], as_index=False)
              .agg(invoices=("invoice_id", "count"), invoiced=("total", "sum"),
                   paid=("amount_paid", "sum"), outstanding=("amount_due", "sum")))
    out["invoiced"] = out["invoiced"].round(2)
    out["paid"] = out["paid"].round(2)
    out["outstanding"] = out["outstanding"].round(2)
    out.to_parquet(GOLD / "revenue_summary.parquet", index=False)
    return out


def build_file_index():
    """Every FILE in the BMS / IMS / FDS document libraries (bronze/bms_files, extract_bms_files.py).
    Powers the agent's find_files tool — filename/path search, not file contents."""
    f = BRONZE / "bms_files" / "files.jsonl"
    rows = [json.loads(l) for l in open(f, encoding="utf-8")] if f.exists() else []
    df = pd.DataFrame(rows)
    if len(df):
        df["site"] = df.get("site", "BMS")
        df["site"] = df["site"].fillna("BMS")            # rows from the pre-multi-site extractor
        df = (df.drop_duplicates(subset=["web_url"])      # delta can re-emit moved/renamed items
                .sort_values(["site", "library", "folder_path", "file_name"])
                .reset_index(drop=True))
        df["modified_at"] = pd.to_datetime(df["modified_at"], errors="coerce", utc=True)
        cols = ["site", "library", "file_name", "ext", "folder_path",
                "size_kb", "web_url", "modified_at", "modified_by"]
        df = df[[c for c in cols if c in df.columns]]
    return _write_gold(df, "file_index")


def build_fds_tables():
    """One gold table per FDS SharePoint list (bronze/fds, extract_sharepoint_bms.py --site FDS).
    Auto-schema: fields as-is minus SP system noise; non-scalar values JSON-encoded.
    Empty lists are skipped so no column-less parquet ever lands in gold."""
    import re as _re
    base = BRONZE / "fds"
    if not base.exists():
        return {}
    noise = {"@odata.etag", "Edit", "LinkTitle", "LinkTitleNoMenu", "ItemChildCount",
             "FolderChildCount", "_UIVersionString", "_ComplianceFlags", "_ComplianceTag",
             "_ComplianceTagWrittenTime", "_ComplianceTagUserId", "AppAuthorLookupId",
             "AppEditorLookupId", "ContentType", "Attachments"}
    counts = {}
    for lst_dir in sorted(base.glob("*/*")):
        if not lst_dir.is_dir() or lst_dir.parent.name == "_manifests":
            continue
        f = _latest(str(lst_dir / "ingest_date=*" / "items.ndjson"))
        if not f:
            continue
        rows = []
        for l in open(f, encoding="utf-8"):
            rec = json.loads(l).get("fields", {})
            rows.append({k: (json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else v)
                         for k, v in rec.items() if k not in noise})
        df = pd.DataFrame(rows)
        if not len(df.columns):
            continue                                   # empty list -> no table (never column-less parquet)
        tbl = "fds_" + _re.sub(r"[^a-z0-9]+", "_", lst_dir.name.replace("FDS-", "").lower()).strip("_")
        try:
            df.to_parquet(GOLD / f"{tbl}.parquet", index=False)
        except Exception:                              # mixed-type column -> stringify objects and retry
            for c in df.columns[df.dtypes == "object"]:
                df[c] = df[c].map(lambda v: None if v is None else str(v))
            df.to_parquet(GOLD / f"{tbl}.parquet", index=False)
        counts[tbl] = len(df)
    return counts


# ---------- upload ----------

def upload_layer(folder, prefix):
    import importlib.util
    spec = importlib.util.spec_from_file_location("u", str(SCRIPT_DIR / "upload_to_blob.py"))
    u = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(u)
    from azure.storage.blob import ContentSettings
    cc = u.get_service_client().get_container_client(u.CONTAINER)
    n = 0
    for f in Path(folder).glob("*.parquet"):
        with open(f, "rb") as fh:
            cc.upload_blob(f"{prefix}/{f.name}", fh, overwrite=True,
                           content_settings=ContentSettings(content_type="application/octet-stream"))
        n += 1
        print(f"  uploaded {prefix}/{f.name}")
    return n


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-upload", action="store_true")
    args = ap.parse_args()

    print("=== SILVER · dims ===")
    dp = build_dim_person();    print(f"  dim_person       {len(dp):6d}")
    dpos = build_dim_position();print(f"  dim_position     {len(dpos):6d}")
    dpr = build_dim_project();  print(f"  dim_project      {len(dpr):6d}")
    dc = build_dim_client();    print(f"  dim_client       {len(dc):6d}")
    print(f"  dim_company      {len(build_dim_company()):6d}")
    dsite = build_dim_site();   print(f"  dim_site         {len(dsite):6d}")
    dsup = build_dim_supplier();print(f"  dim_supplier     {len(dsup):6d}")
    dcomp = build_dim_competency(); print(f"  dim_competency   {len(dcomp):6d}")
    dasset = build_dim_asset(); print(f"  dim_asset        {len(dasset):6d}")
    djob = build_dim_job();     print(f"  dim_job          {len(djob):6d}")

    print("=== SILVER · facts ===")
    print(f"  fact_roster      {len(build_fact_roster()):6d}")
    ftime = build_fact_timesheet(); print(f"  fact_timesheet   {len(ftime):6d}")
    ftrain = build_fact_training(); print(f"  fact_training    {len(ftrain):6d}")
    fchg = build_fact_change_log(); print(f"  fact_change_log  {len(fchg):6d}")
    print(f"  fact_inventory_txn {len(build_fact_inventory_txn()):6d}")
    fpur = build_fact_purchase(); print(f"  fact_purchase    {len(fpur):6d}")

    print("=== GOLD (by business domain) ===")
    print(f"  [People]   employee_profile    {len(build_employee_profile(dp, dpos, dsup)):6d}")
    print(f"  [People]   training_compliance {len(build_training_compliance(ftrain, dp, dcomp)):6d}")
    print(f"  [Roster]   roster_summary      {len(build_roster_summary()):6d}")
    print(f"  [Time]     timesheet_summary   {len(build_timesheet_summary(ftime, dp, dsite)):6d}")
    print(f"  [Time]     weekly_timesheet    {len(build_weekly_timesheet()):6d}")
    print(f"  [Project]  project_bridge      {len(build_project_bridge()):6d}")
    print(f"  [Commercial] invoice_register  {len(build_invoice_register()):6d}")
    print(f"  [Commercial] revenue_summary   {len(build_revenue_summary()):6d}")
    print(f"  [Project]  project_job_summary {len(build_project_job_summary(dpr, dc, djob)):6d}")
    print(f"  [Asset]    asset_register      {len(build_asset_register(dasset)):6d}")
    print(f"  [HSEQ]     hseq_register       {len(build_hseq_register()):6d}")
    print(f"  [Supplier] supplier_summary    {len(build_supplier_summary(dsup, dp)):6d}")
    print(f"  [Finance]  purchase_summary    {len(build_purchase_summary(fpur)):6d}")
    print(f"  [Workforce] site_assignment    {len(build_site_assignment()):6d}")
    print(f"  [Inventory] inventory_summary  {len(build_inventory_summary()):6d}")
    print(f"  [Inventory] ppe_transactions   {len(build_ppe_transactions()):6d}")
    print(f"  [Audit]    audit_activity      {len(build_audit_activity(fchg, dp)):6d}")
    print(f"  [Project]  job_detail          {len(build_job_detail(djob, dpr, dc, dp)):6d}")
    print(f"  [Hardware] hardware_register   {len(build_hardware_register()):6d}")
    print(f"  [Commercial] rate_card         {len(build_rate_card(dpr)):6d}")
    print(f"  [Compliance] licence_register  {len(build_licence_register()):6d}")
    print(f"  [Workforce] worker_ranking     {len(build_worker_ranking()):6d}")
    print(f"  [Files]    file_index          {len(build_file_index()):6d}")
    _fds = build_fds_tables()
    print(f"  [FDS]      fds_* lists         {len(_fds):6d} tables / {sum(_fds.values()):7d} rows")

    if not args.no_upload:
        print("=== UPLOAD ===")
        upload_layer(SILVER, "silver")
        upload_layer(GOLD, "gold")

    print("\nDONE.")


if __name__ == "__main__":
    main()
