"""
Lulu Operations Center — observability-first UI for the workforce agent.

    1. Operations Dashboard   (the boss's daily view — today's problems, no question needed)
    2. Ask Lulu               (ops asks; answer + full agent trace side by side)
    3. Agent Trace            (debugging: tool analytics, gateway status, recent runs)

Run:  streamlit run lulu_ops_center.py
Works WITHOUT any LLM API key (deterministic planner + direct Gold metrics).
If ANTHROPIC_API_KEY etc. are configured, Ask Lulu can also run through the LLM Gateway.
"""

import sys
from datetime import datetime
from pathlib import Path

import streamlit as st

AGENT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(AGENT_DIR))

import ops_metrics as M                                   # noqa: E402
from lulu_agent import LuluAgent                          # noqa: E402  (deterministic V2 planner)
from tool_usage_logger import UsageLogger                 # noqa: E402
from llm_provider import gateway_status                   # noqa: E402
from conversation_trace_logger import TraceLogger         # noqa: E402
from auth import require_login, logout_button, admin_panel  # noqa: E402

CAT_ICON = str(AGENT_DIR / "static" / "lulu_cat.svg")     # Lulu's face (黑猫 logo + chat avatar)
CAT_FAVICON = str(AGENT_DIR / "static" / "lulu_favicon.png")   # browser-tab icon (NOT the purple emoji cat)
ORB_ICON = str(AGENT_DIR / "static" / "lulu_orb.svg")    # golden particle "memory ball" — logo / avatar / favicon (replaces the cat)

# Sidebar logo = an INTERACTIVE particle sphere (no solid ball). Golden dots on a 3D sphere,
# auto-rotating, with a one-time intro (tiny dense ball → expands), and mouse-repel interaction.
# Rendered via components.html (needs JS), so it lives in an iframe — see render in the sidebar.
LULU_ORB_HEIGHT = 76
LULU_ORB_HTML = """
<style>html,body{margin:0;padding:0;overflow:hidden;background:transparent}</style>
<canvas id="orb" width="140" height="140"
        style="width:70px;height:70px;display:block;margin:0 auto;cursor:pointer"></canvas>
<script>
const cv=document.getElementById('orb'),ctx=cv.getContext('2d');
const W=140,H=140,CX=W/2,CY=H/2,R=46,N=180;
// --- surface: fibonacci sphere, evenly spread points on a unit sphere ---
const pts=[],inc=Math.PI*(3-Math.sqrt(5));
for(let i=0;i<N;i++){const y=1-(i/(N-1))*2,r=Math.sqrt(1-y*y),phi=i*inc;
  pts.push({surf:1,x:Math.cos(phi)*r,y:y,z:Math.sin(phi)*r,ox:0,oy:0});}
// --- emitters: dots that are born at the CENTER and travel outward, then recycle ---
const rnd=Math.random;
function spawn(e){const phi=rnd()*6.283,ct=rnd()*2-1,st=Math.sqrt(1-ct*ct);
  e.dx=st*Math.cos(phi);e.dy=ct;e.dz=st*Math.sin(phi);e.life=rnd()*0.25;e.sp=0.006+rnd()*0.009;return e;}
const em=[];for(let i=0;i<40;i++)em.push(spawn({surf:0}));
let ang=0,t=0,mx=-999,my=-999;
const ease=x=>1-Math.pow(1-x,3);
cv.addEventListener('mousemove',e=>{const b=cv.getBoundingClientRect();
  mx=(e.clientX-b.left)*W/b.width;my=(e.clientY-b.top)*H/b.height;});
cv.addEventListener('mouseleave',()=>{mx=-999;my=-999;});
function frame(){
  t++;const intro=Math.min(1,t/72);
  const scale=(0.10+0.90*ease(intro))*(1+0.09*Math.sin(t*0.045));  // intro expand + breathing pulse
  ang+=0.005;const ca=Math.cos(ang),sa=Math.sin(ang);
  ctx.clearRect(0,0,W,H);
  // soft glow halo behind everything (transparent edges → reads as halo, not a solid ball)
  const g=ctx.createRadialGradient(CX,CY,0,CX,CY,R*scale*1.35);
  g.addColorStop(0,'rgba(255,205,120,.20)');g.addColorStop(.5,'rgba(255,180,90,.06)');g.addColorStop(1,'rgba(0,0,0,0)');
  ctx.fillStyle=g;ctx.fillRect(0,0,W,H);
  // advance emitters; r=life is radial distance 0..1 (center → surface)
  for(const e of em){e.life+=e.sp;if(e.life>=1)spawn(e);}
  // build one list (surface + emitters), rotate around Y, project, depth-sort, draw
  const all=[];
  for(const p of pts){const x=p.x*ca-p.z*sa,z=p.x*sa+p.z*ca;
    all.push({p:p,surf:1,sx:CX+x*R*scale,sy:CY+p.y*R*scale,z:z});}
  for(const e of em){const r=e.life,ex=e.dx*r,ez=e.dz*r,x=ex*ca-ez*sa,z=ex*sa+ez*ca;
    all.push({surf:0,life:e.life,sx:CX+x*R*scale,sy:CY+e.dy*r*R*scale,z:z});}
  for(const q of all){if(!q.surf)continue;      // mouse repel + spring back (surface dots only)
    const dx=q.sx+q.p.ox-mx,dy=q.sy+q.p.oy-my,d=Math.hypot(dx,dy);
    if(d<26){const f=(26-d)/26*7;q.p.ox+=dx/(d||1)*f;q.p.oy+=dy/(d||1)*f;}
    q.p.ox*=0.86;q.p.oy*=0.86;q.sx+=q.p.ox;q.sy+=q.p.oy;}
  all.sort((a,b)=>a.z-b.z);                       // painter's algo: far dots first
  for(const q of all){const depth=(q.z+1)/2;
    if(q.surf){
      ctx.fillStyle='rgba(255,'+(200+depth*40|0)+','+(120+depth*70|0)+','+(0.22+0.62*depth)+')';
      ctx.shadowColor='rgba(255,200,110,.9)';ctx.shadowBlur=7*depth*scale;
      ctx.beginPath();ctx.arc(q.sx,q.sy,(0.7+depth*1.7)*scale+0.3,0,7);ctx.fill();
    }else{                                        // emitter: bright, fades in at center & out at rim
      const op=Math.sin(q.life*3.14159)*(0.35+0.55*depth);
      ctx.fillStyle='rgba(255,240,200,'+op+')';
      ctx.shadowColor='rgba(255,215,140,.95)';ctx.shadowBlur=9*depth*scale;
      ctx.beginPath();ctx.arc(q.sx,q.sy,(0.5+q.life*1.6)*scale+0.3,0,7);ctx.fill();
    }}
  requestAnimationFrame(frame);
}
frame();
</script>
"""
st.set_page_config(page_title="Lulu Operations Center", page_icon=ORB_ICON, layout="wide")

# replace Streamlit's top-right "running man" with a slowly spinning golden memory-ball
import urllib.parse as _ul
_ORB_DATA = "data:image/svg+xml," + _ul.quote(open(ORB_ICON, encoding="utf-8").read())
st.markdown(f"""
<style>
[data-testid="stStatusWidget"] img, [data-testid="stStatusWidget"] svg {{ display: none !important; }}
[data-testid="stStatusWidget"]::before {{
    content: url("{_ORB_DATA}");
    display: inline-block; width: 26px; height: 26px; margin-right: 4px; vertical-align: middle;
    animation: lulu-orb-spin 6s linear infinite;
}}
@keyframes lulu-orb-spin {{ to {{ transform: rotate(360deg); }} }}
</style>""", unsafe_allow_html=True)

# ---- global sci-fi / tech theme (applies to ALL pages) ----
st.markdown("""
<style>
:root{ --cy:#46e0ff; --mint:#7cf0d8; --ln:#16243a; --ln2:#23364f;
       --card1:rgba(13,21,36,.72); --card2:rgba(8,13,22,.72); --mut:#7e90aa; }
[data-testid="stAppViewContainer"]{ background:
  radial-gradient(1100px 640px at 78% -12%, rgba(70,224,255,.07), transparent 60%),
  radial-gradient(900px 600px at 0% 110%, rgba(167,139,250,.06), transparent 55%), #05080f; }
[data-testid="stHeader"]{ background:rgba(5,8,15,.55); backdrop-filter:blur(6px); }
[data-testid="stSidebar"]{ background:linear-gradient(180deg,#0a111c,#06090f); border-right:1px solid var(--ln); }
.stApp, .stMarkdown, p, span, label, li{ color:#cbd9ec; }

/* keep reading width sane on ultra-wide monitors (Galaxy page re-widens itself) */
section.main .block-container, [data-testid="stMainBlockContainer"]{ max-width:1600px; margin:0 auto; }

/* headings — clearer hierarchy + a small accent bar on section titles */
h1,h2,h3,h4{ letter-spacing:.6px; }
h1{ color:#e7f4ff; text-shadow:0 0 18px rgba(70,224,255,.22); }
h2,h3{ color:#bfe6ff; }
[data-testid="stHeading"] h2, [data-testid="stHeading"] h3{
  position:relative; padding-left:13px; margin-top:.4rem; }
[data-testid="stHeading"] h2::before, [data-testid="stHeading"] h3::before{
  content:""; position:absolute; left:0; top:.18em; bottom:.18em; width:3px;
  border-radius:3px; background:linear-gradient(180deg,var(--cy),var(--mint)); }
a{ color:var(--cy) !important; }
hr, [data-testid="stSidebar"] hr{ border-color:var(--ln) !important; }
[data-testid="stCaptionContainer"]{ color:var(--mut) !important; }

.stButton>button, .stDownloadButton>button{ background:rgba(70,224,255,.07); border:1px solid #26384f; color:#cfe0f2; border-radius:9px; transition:.15s; }
.stButton>button:hover{ border-color:var(--cy); color:#fff; box-shadow:0 0 12px rgba(70,224,255,.25); }

/* metric cards — premium feel: top highlight line, hover lift, bigger value */
[data-testid="stMetric"]{ position:relative; overflow:hidden;
  background:linear-gradient(180deg,var(--card1),var(--card2)); border:1px solid var(--ln);
  border-radius:14px; padding:14px 16px; transition:border-color .15s, box-shadow .15s, transform .15s; }
[data-testid="stMetric"]::after{ content:""; position:absolute; top:0; left:0; right:0; height:2px;
  background:linear-gradient(90deg,var(--cy),transparent 72%); opacity:.55; }
[data-testid="stMetric"]:hover{ border-color:var(--ln2); transform:translateY(-1px); box-shadow:0 6px 22px rgba(0,0,0,.35); }
[data-testid="stMetricValue"]{ font-family:ui-monospace,Consolas,monospace; color:var(--cy); font-size:2.1rem; }
[data-testid="stMetricLabel"]{ color:var(--mut); letter-spacing:.6px; text-transform:uppercase; font-size:.74rem; }

/* bordered containers + dataframes + expander — unified border & radius */
[data-testid="stVerticalBlockBorderWrapper"]{ border-radius:14px; }
[data-testid="stDataFrame"]{ border:1px solid var(--ln); border-radius:12px; overflow:hidden; }
[data-testid="stExpander"]{ border:1px solid var(--ln); border-radius:12px; background:rgba(10,17,28,.6); }

/* inputs — selectbox / multiselect / their tags share the theme */
[data-baseweb="select"]>div{ background:var(--card2) !important; border-color:var(--ln) !important; border-radius:9px !important; }
[data-baseweb="select"]>div:hover{ border-color:var(--ln2) !important; }
[data-baseweb="tag"]{ background:rgba(70,224,255,.16) !important; border:1px solid #2b4a63 !important; color:#dff3ff !important; }

/* sidebar nav radios */
[data-testid="stSidebar"] [role=radiogroup] label{ border:1px solid transparent; border-radius:9px; padding:6px 9px; margin:2px 0; transition:.12s; }
[data-testid="stSidebar"] [role=radiogroup] label:hover{ background:rgba(70,224,255,.06); border-color:var(--ln); }
.stTabs [aria-selected="true"]{ color:var(--cy); }
code{ color:#9bf0c8; }
</style>""", unsafe_allow_html=True)


# login wall disabled — default to Admin / Admin_IT (re-enable with require_login() later)
user = {"name": "Admin", "email": "admin@company.com.au", "role": "Admin_IT"}


# ---------------- cached singletons ----------------
@st.cache_resource
def det_agent():
    return LuluAgent()


@st.cache_resource
def usage_logger():
    return UsageLogger()


@st.cache_data(ttl=600, show_spinner="Refreshing Gold metrics…")
def kpis():
    return M.get_kpis()


@st.cache_data(ttl=600, show_spinner=False)
def command_center():
    return {
        "supplier_risk": M.get_supplier_risk(),
        "expiry_ladder": M.get_expiry_ladder(),
        "risk_by_project": M.get_risk_by_project(),
        "deployable": M.get_deployable_preview(),
        "urgent": M.get_urgent_expiries(7),
        "expiry_forecast": M.get_expiry_forecast(6),
        "workforce_by_supplier": M.get_workforce_by_supplier(),
        "weekly_hours": M.get_weekly_hours(10),
    }


@st.cache_data(ttl=600, show_spinner=False)
def recommendations():
    return M.get_recommendations(kpis())


@st.cache_data(ttl=600, show_spinner=False)
def business_exposure():
    return M.get_business_exposure()


@st.cache_data(ttl=600, show_spinner=False)
def wcc_data():
    """Raw Gold frames for the interactive (Tableau-like) Workforce Command Center."""
    import pandas as _pd
    g = AGENT_DIR.parent / "gold"

    def _rd(n):
        p = g / (n + ".parquet")
        return _pd.read_parquet(p) if p.exists() else _pd.DataFrame()
    return {"emp": _rd("employee_profile"), "tc": _rd("training_compliance"),
            "ros": _rd("roster_summary"), "wts": _rd("weekly_timesheet")}


@st.cache_data(ttl=600, show_spinner=False)
def jms_projects():
    """JMS-Projects master (read-only Gold: project_job_summary) — 72 projects with pipeline status."""
    import pandas as _pd
    p = AGENT_DIR.parent / "gold" / "project_job_summary.parquet"
    if not p.exists():
        return _pd.DataFrame()
    df = _pd.read_parquet(p)
    df["status"] = df["status"].fillna("(no status)")
    df["client_name"] = df.get("client_name", "").fillna("(no client)")
    df["project_name"] = df["project_name"].fillna("(no project)")
    for c in ("job_count", "active_job_count"):
        if c in df:
            df[c] = df[c].fillna(0).astype(int)
    return df


@st.cache_data(ttl=600, show_spinner=False)
def jms_jobs():
    """JMS-Jobs for the interactive analysis section (read-only Gold: job_detail)."""
    import pandas as _pd
    p = AGENT_DIR.parent / "gold" / "job_detail.parquet"
    if not p.exists():
        return _pd.DataFrame()
    df = _pd.read_parquet(p)
    df["lead"] = (df.get("lead_first_name", "").fillna("") + " "
                  + df.get("lead_last_name", "").fillna("")).str.strip().replace("", "(no lead)")
    df["project_name"] = df["project_name"].fillna("(no project)")
    df["job_status"] = df["job_status"].fillna("(no status)")
    df["client_name"] = df.get("client_name", "").fillna("(no client)")
    return df


def gw_status():
    try:
        return gateway_status()
    except Exception:
        return {}


# ---------------- sidebar ----------------
with st.sidebar:
    c_logo, c_name = st.columns([1, 2])
    with c_logo:
        import streamlit.components.v1 as components
        components.html(LULU_ORB_HTML, height=LULU_ORB_HEIGHT, scrolling=False)
    c_name.title("Lulu")
    # Operations Dashboard is the boss's landing page; System Galaxy is the executive system map
    # (secondary). Agent Trace is a dev/admin tool — hidden from non-admins.
    _is_admin = user.get("role") == "Admin_IT"
    _nav_pages = ["Operations Dashboard", "System Galaxy", "Ask Lulu"] + (["Agent Trace"] if _is_admin else [])
    page = st.radio("View", _nav_pages, label_visibility="collapsed")
    st.divider()

    # identity — the role is BOUND to the login, not chosen from a dropdown
    st.caption(f"**{user['name']}**  \n{user['email']}")
    if user["role"] == "Admin_IT":
        role = st.selectbox("Role (Admin test switch)",
                            ["Admin_IT", "default", "HR_Manager", "Finance"])
    else:
        role = user["role"]
        st.caption(f"Role: `{role}`")
    logout_button()
    st.divider()

    # Recents — this user's own conversations (rebuilt from the trace log)
    if page.startswith("Ask"):
        if st.button("New chat", type="primary", use_container_width=True):
            for k in ("conv_id", "chat", "engine_history", "last_pill"):
                st.session_state.pop(k, None)
            st.rerun()
        st.caption("Recents")
        _recents = TraceLogger().conversations(limit=8, user=user["email"])
        cur = st.session_state.get("conv_id")
        for c in _recents:
            label = ("▶ " if c["conversation_id"] == cur else "") + c["title"][:26] + f" · {c['turns']}"
            if st.button(label, key="rc_" + c["conversation_id"], use_container_width=True):
                hist, chat = TraceLogger().load_conversation(c["conversation_id"])
                st.session_state.conv_id = c["conversation_id"]
                st.session_state.engine_history = hist
                st.session_state.chat = chat
                st.rerun()
        if not _recents:
            st.caption("(no conversations yet)")
        st.divider()

    gs = gw_status()
    if gs and _is_admin:                       # engine/model detail is admin-only plumbing
        st.caption("LLM Gateway")
        for r in ("planner", "answer", "fallback"):
            d = gs.get(r, {})
            dot = "🟢" if d.get("available") else "⚪"
            st.caption(f"{dot} {r}: {d.get('provider','?')}/{d.get('model','?')}")
    if st.button("Refresh data", help="Pull the latest nightly Gold from cloud blob and refresh."):
        try:
            import blob_gold as _bg
            with st.spinner("Refreshing data from cloud…"):
                _bg.pull_gold(force=True)      # force an immediate blob pull (no-op locally)
                _bg.pull_state(force=True)     # + non-gold UI files (link_health.json)
                _bg.regenerate_local_state()   # recompute snapshot + DQ report from fresh gold
        except Exception:
            pass
        st.cache_data.clear()
        st.rerun()
    from pathlib import Path as _P
    _api = _P(__file__).resolve().parents[1] / "Raw Data" / "API"
    _sync_ok = (_api / "sync_from_cloud.py").exists()      # pipeline code isn't baked into the cloud image
    if st.button("🔄 Sync from cloud", disabled=not _sync_ok,
                 help="Pull the latest gold/silver from the nightly cloud refresh into the local lake"
                      if _sync_ok else "Local-only — the pipeline code isn't shipped in the cloud image."):
        import subprocess, sys as _sys
        with st.spinner("Syncing data lake from cloud blob…"):
            r = subprocess.run([_sys.executable, str(_api / "sync_from_cloud.py")], cwd=str(_api))
        if r.returncode == 0:
            st.cache_data.clear()
            st.success("Synced. Showing latest cloud data.")
            st.rerun()
        else:
            st.error("Sync failed — check BLOB_CONNECTION_STRING / network.")


# ---- keep the cloud Gold fresh: pull the nightly-built parquet from blob (no-op locally) ----
# The image bakes a snapshot of Gold; this pulls the latest from lulu-data/gold/ on a TTL so the
# boss sees yesterday's nightly refresh without us rebuilding the image. Local dev (no blob conn) skips.
try:
    import blob_gold as _bg
    if _bg.pull_gold():                       # truthy only when NEW parquet were downloaded
        with st.spinner("Refreshing data from cloud…"):
            _bg.pull_state(force=True)        # non-gold UI files from blob (link_health.json)
            _bg.regenerate_local_state()      # recompute snapshot + DQ report from the fresh gold
        st.cache_data.clear()                 # drop the cached loaders so they re-read fresh gold
    else:
        _bg.pull_state()                      # gold unchanged ≠ audit unchanged: link_health.json has
                                              # its own 10-min TTL so page loads stay fresh all day
except Exception:
    pass


# =====================================================================
# PAGE 1 — OPERATIONS DASHBOARD
# =====================================================================
if "Galaxy" in page:
    import streamlit.components.v1 as components
    from pathlib import Path as _CCPath
    # immersive: minimise page chrome so the galaxy fills the view
    st.markdown("<style>section.main .block-container,[data-testid='stMainBlockContainer']{padding:0.4rem 0.4rem 0 0.4rem;max-width:100%;}</style>", unsafe_allow_html=True)

    # ---- Folder link health (native panel: table + actions together; updates on action) ----
    _lhp = _CCPath(__file__).resolve().parent / "link_health.json"
    _apidir = _CCPath(__file__).resolve().parents[1] / "Raw Data" / "API"
    import json as _json
    _lhd = _json.loads(_lhp.read_text(encoding="utf-8")) if _lhp.exists() else {}
    _probs = sum(v.get("broken", 0) + v.get("missing", 0)
                 for v in _lhd.values() if isinstance(v, dict) and "broken" in v)
    _label = f"🔗 Folder link health — {_probs} broken/missing" if _probs else "🔗 Folder link health — all healthy ✓"
    with st.expander(_label, expanded=bool(_probs)):
        import subprocess as _sp, sys as _sys2
        import pandas as _pd
        _c1, _c2, _c3 = st.columns([2, 2, 6])
        # The live SharePoint audit needs the pipeline code (data/Raw Data/API) + SharePoint creds,
        # neither of which exist in the cloud app image. So: where the pipeline IS present (local /
        # pipeline host) run the real audit/repair; in the cloud app, the "Re-check" button instead
        # pulls the LATEST nightly audit result from blob (the nightly job re-checks + auto-repairs).
        _live_ok = _apidir.exists() and (_apidir / "check_links.py").exists()
        if _live_ok:
            if _c1.button("🔄 Re-check (live)", use_container_width=True, help="Live audit (~1 min). Read-only."):
                with st.spinner("Checking folder links…"):
                    _sp.run([_sys2.executable, str(_apidir / "check_links.py")], cwd=str(_apidir))
                st.rerun()
            if _c2.button("🔧 Fix links now", use_container_width=True, help="Auto-repair stale links (backed up)."):
                with st.spinner("Repairing stale links…"):
                    _sp.run([_sys2.executable, str(_apidir / "check_links.py"), "--repair"], cwd=str(_apidir))
                st.rerun()
        else:
            if _c1.button("🔄 Refresh from cloud", use_container_width=True,
                          help="Fetch the latest folder-link audit. The nightly job re-checks + auto-repairs and uploads it."):
                try:
                    import blob_gold as _bg
                    _bg.pull_state(force=True)
                except Exception:
                    pass
                st.rerun()
            _c2.button("🔧 Fix links now", use_container_width=True, disabled=True,
                       help="Auto-repair runs in the nightly job (needs SharePoint access) — not from the cloud app.")
            _c3.caption("ℹ️ The nightly job re-checks **and auto-repairs** folder links, then uploads the result. "
                        "This button fetches that latest audit; a live re-scan runs where the pipeline lives.")
        for _tbl, _v in _lhd.items():
            if not isinstance(_v, dict) or "broken" not in _v:
                continue
            _stamp = str(_lhd.get("checked_at", ""))[:16].replace("T", " ")
            st.caption(f"**{_tbl}** — {_v['ok']} ok · {_v['broken']} broken · {_v['missing']} missing · checked {_stamp} UTC")
            if _v.get("items"):
                _rows = [{"Job": it["jobid"], "Title": it["title"],
                          "Ops": it["cells"]["OpsFolder"], "Com": it["cells"]["ComFolder"],
                          "Plan": it["cells"]["PlanningFolder"],
                          "Fixed": "✓" if it.get("fixed") else ""} for it in _v["items"]]
                st.dataframe(_pd.DataFrame(_rows), use_container_width=True, hide_index=True)
            else:
                st.caption("✓ no problems")

    # ---- P2: Action Console (native repair control layer) ----------------------------------
    # The galaxy itself is an iframe (display only) — it cannot call back into Python. This
    # native panel is where issues actually get handled: select issue → confirmation panel ->
    # dry-run → (safe runs / needs_approval gate / manual task) → execute → audit log.
    import sys as _sysC
    _ckptC = str(_CCPath(__file__).resolve().parent / "cockpit")
    if _ckptC not in _sysC.path:
        _sysC.path.insert(0, _ckptC)
    _iv = {"issues": [], "by_node": {}, "alert_count": 0, "node_meta": {}, "entities": {}}
    _arun = _alog = None
    _ACTIONS = {}
    try:
        import issue_registry as _ireg
        _iv = _ireg.build()
    except Exception:
        pass
    try:
        import action_runner as _arun
        import action_log as _alog
        _ACTIONS = _arun.load_actions()
    except Exception:
        _arun = _alog = None

    _open_issues = [i for i in _iv.get("issues", []) if isinstance(i, dict)]
    _SEVI = {"critical": "🔴", "high": "🟠", "medium": "🟡", "low": "🔵"}
    _SAFEB = {"safe_auto": "🟢 Can Auto Run", "needs_approval": "🟠 Requires Approval",
              "manual_only": "⚪ Manual Review"}
    with st.expander(f"⚙️ Action Console — repair control layer ({len(_open_issues)} open issues)",
                     expanded=False):
        if _arun is None:
            st.caption("Action runner unavailable (cockpit modules failed to import).")
        elif not _open_issues:
            st.caption("No open issues to action.")
        else:
            _opts = {f"{_SEVI.get(i.get('severity'), '·')} {i.get('id')} · {i.get('title', '')[:64]}": i
                     for i in _open_issues}
            _sel = st.selectbox("Issue to handle", list(_opts.keys()))
            _iss = _opts[_sel]
            _iid = _iss.get("id")
            _ref = _iss.get("action_ref")
            _act = _ACTIONS.get(_ref, {}) if _ref else {}
            _safety = _act.get("safety") or _iss.get("safety") or "manual_only"
            _writes = bool(_act.get("writes"))
            _status = _alog.issue_status(_iid) if _alog else None

            # session state (per issue+action) for the approval workflow
            _reqk, _apprk, _dsk = f"req_{_iid}", f"appr_{_iid}", f"dry_{_iid}"

            # ---- confirmation panel ----
            m1, m2, m3, m4 = st.columns(4)
            m1.metric("Severity", _iss.get("severity", "—"))
            m2.metric("Affected", f"{_iss.get('affected_count', '—'):,}" if isinstance(_iss.get('affected_count'), int) else "—")
            m3.metric("Safety", {"safe_auto": "Auto", "needs_approval": "Approval", "manual_only": "Manual"}.get(_safety, "—"))
            m4.metric("Status", (_status or "open"))
            st.markdown(f"**{_iss.get('title', '')}**  \n"
                        f"`{_iid}` · entity `{_iss.get('entity', '—')}` · node `{_iss.get('node', '—')}` · "
                        f"owner {_iss.get('owner', '—')} · source `{_iss.get('source', '—')}`")
            st.markdown(f"**Action:** {_act.get('label', _ref or '—')} — {_SAFEB.get(_safety, _safety)}"
                        + ("  ·  ✍️ **writes production data**" if _writes else "  ·  read-only"))
            if _act.get("description"):
                st.caption(_act["description"])
            if _act.get("backup"):
                st.caption(f"🛟 Backup before write: `{_act['backup']}`  ·  Rollback: {_act.get('rollback', '—')}")

            def _show(_r):
                """render an action result + persist its audit event."""
                if not _r:
                    return
                (st.success if _r.get("ok") else (st.warning if _r.get("refused") or _r.get("not_wired") else st.error))(
                    _r.get("output") or _r.get("error") or "(no output)")
                if _r.get("output") and _r.get("returncode") is not None:
                    st.code(_r["output"], language="text")

            st.divider()
            # ---- safe read-only tools (always available) ----
            st.caption("**Safe tools (read-only — never write production):**")
            s1, s2, s3 = st.columns(3)
            if s1.button("✅ Validate records", key=f"val_{_iid}", use_container_width=True):
                _r = _arun.run_action("validate_affected_records", issue=_iss)
                _alog.log_event(_iid, "validate_affected_records", "executed", result="ok", detail=_r.get("output", ""))
                _show(_r)
            if s2.button("⬇ Export list", key=f"exp_{_iid}", use_container_width=True):
                _r = _arun.run_action("export_affected_records", issue=_iss)
                _alog.log_event(_iid, "export_affected_records", "executed", result="ok", detail=_r.get("output", ""))
                _show(_r)
            if s3.button("🔁 Rebuild registry", key=f"reb_{_iid}", use_container_width=True):
                _r = _arun.run_action("rebuild_issue_registry", issue=_iss)
                _alog.log_event(_iid, "rebuild_issue_registry", "executed", result="ok", detail=_r.get("output", ""))
                _show(_r)
            g1, g2 = st.columns(2)
            if g1.button("🩺 Re-run data quality sentinel", key=f"snt_{_iid}", use_container_width=True):
                with st.spinner("Running data-quality checks against Gold…"):
                    _r = _arun.run_action("rerun_data_quality_sentinel", issue=_iss)
                _alog.log_event(_iid, "rerun_data_quality_sentinel", "executed",
                                result=("ok" if _r.get("ok") else "failed"), detail=_r.get("output", ""))
                _show(_r)
            if g2.button("🔗 Re-check folder links (live ~1 min)", key=f"rcl_{_iid}", use_container_width=True):
                with st.spinner("Auditing folder links against SharePoint…"):
                    _r = _arun.run_action("recheck_folder_links", issue=_iss)
                _alog.log_event(_iid, "recheck_folder_links", "executed",
                                result=("ok" if _r.get("ok") else "failed"), detail=_r.get("output", ""))
                _show(_r)

            st.divider()
            # ---- the issue's bound action, gated by safety level ----
            if _safety == "safe_auto":
                if st.button(f"▶ Run now: {_act.get('label', _ref)}", key=f"run_{_iid}",
                             type="primary", use_container_width=True):
                    _r = _arun.run_action(_ref, mode="execute", issue=_iss)
                    _alog.log_event(_iid, _ref, "executed", mode="execute",
                                    result=("resolved" if _r.get("ok") else "failed"),
                                    writes=_writes, detail=_r.get("output", _r.get("error", "")))
                    _show(_r)

            elif _safety == "needs_approval":
                st.caption("This action needs approval. **1)** Dry-run/preview → **2)** Request approval → "
                           "**3)** Approve → **4)** Execute.")
                if "dry_run" in _act.get("modes", []):
                    if st.button("🔍 1. Dry-run / Preview (writes nothing)", key=f"dry_{_iid}_btn",
                                 use_container_width=True):
                        with st.spinner("Previewing — resolving links against SharePoint…"):
                            _r = _arun.run_action(_ref, mode="dry_run", issue=_iss)
                        st.session_state[_dsk] = _r.get("ok", False)
                        _alog.log_event(_iid, _ref, "dry_run", mode="dry_run", writes=False,
                                        detail=_r.get("output", _r.get("error", "")))
                        _show(_r)
                        if _r.get("changed"):
                            st.info(f"Preview: would fix {_r['changed'].get('fixed', 0)}, "
                                    f"skip {_r['changed'].get('skipped', 0)} (ID unresolved).")
                else:
                    st.session_state[_dsk] = True   # actions without a dry-run mode skip that gate

                if not st.session_state.get(_apprk):
                    if st.button("📝 2. Request approval", key=f"reqbtn_{_iid}", use_container_width=True):
                        st.session_state[_reqk] = True
                        _alog.log_event(_iid, _ref, "pending_approval", mode="execute", writes=_writes,
                                        detail="approval requested")
                        st.rerun()
                    if st.session_state.get(_reqk):
                        st.warning("⏳ Pending approval (mock approver).")
                        a1, a2 = st.columns(2)
                        if a1.button("✅ 3. Approve", key=f"appbtn_{_iid}", type="primary", use_container_width=True):
                            st.session_state[_apprk] = True
                            _alog.log_event(_iid, _ref, "approved", mode="execute", writes=_writes,
                                            detail="approved by mock approver")
                            st.rerun()
                        if a2.button("⛔ Reject", key=f"rejbtn_{_iid}", use_container_width=True):
                            st.session_state[_reqk] = False
                            _alog.log_event(_iid, _ref, "rejected", mode="execute", writes=_writes,
                                            detail="rejected → manual review")
                            st.rerun()
                else:
                    st.success("✅ Approved.")
                    _can = st.session_state.get(_dsk, False)
                    if not _can:
                        st.caption("Run the dry-run/preview first to enable Execute.")
                    if st.button("⚡ 4. Execute repair", key=f"exe_{_iid}", type="primary",
                                 disabled=not _can, use_container_width=True):
                        with st.spinner("Executing repair (backing up originals first)…"):
                            _r = _arun.run_action(_ref, mode="execute", issue=_iss, approved=True)
                        _res = "resolved" if _r.get("ok") else "failed"
                        if _r.get("changed") and _r["changed"].get("skipped") and _r["changed"].get("fixed"):
                            _res = "partial"
                        _alog.log_event(_iid, _ref, "executed", mode="execute", result=_res,
                                        writes=_writes, detail=_r.get("output", _r.get("error", "")))
                        _show(_r)
                        st.info(f"Issue status → **{_res}**. (Re-check links / rebuild registry to confirm.)")

            else:   # manual_only
                st.caption("⚪ Cannot be auto-repaired — handle manually.")
                q1, q2 = st.columns(2)
                if q1.button("📋 Create manual task", key=f"man_{_iid}", use_container_width=True):
                    _r = _arun.run_action(_ref or "review_missing_docs", mode="execute", issue=_iss)
                    _alog.log_event(_iid, _ref or "manual", "manual", detail=_r.get("output", ""))
                    _show(_r)
                if q2.button("⬇ Export affected list", key=f"manexp_{_iid}", use_container_width=True):
                    _r = _arun.run_action("export_affected_records", issue=_iss)
                    _alog.log_event(_iid, "export_affected_records", "executed", result="ok", detail=_r.get("output", ""))
                    _show(_r)

            # ---- audit log ----
            st.divider()
            st.caption("**Recent actions — audit log** (`logs/cockpit_action_log.jsonl`)")
            _evs = _alog.read_events(limit=12)
            if _evs:
                st.dataframe([{"time": e.get("ts", "")[:19].replace("T", " "), "issue": e.get("issue_id"),
                               "action": e.get("action_key"), "status": e.get("status"),
                               "result": e.get("result") or "", "writes": "✍️" if e.get("writes") else "",
                               "by": e.get("actor")} for e in _evs],
                             use_container_width=True, hide_index=True)
            else:
                st.caption("No actions logged yet.")

    _cc = _CCPath(__file__).resolve().parent / "command_center.html"
    _html = _cc.read_text(encoding="utf-8")
    # inject the latest link-health snapshot (built by check_links.py) into the JMS-Jobs card
    _lh = _CCPath(__file__).resolve().parent / "link_health.json"
    if _lh.exists():
        import json as _json
        _html = _html.replace("var LINKHEALTH = {};  /*__LINKHEALTH__*/",
                              "var LINKHEALTH = " + _lh.read_text(encoding="utf-8") + ";")

    # ---- inject live per-agent business detail (light JSON only — no DuckDB / parquet) ----
    def _fmt_money(v):
        try:
            v = float(v)
        except (TypeError, ValueError):
            return "—"
        if abs(v) >= 1e6:
            return f"${v/1e6:.2f}M"
        if abs(v) >= 1e3:
            return f"${v/1e3:.0f}K"
        return f"${v:.0f}"

    _agent_detail = {}
    try:
        _snapf = _CCPath(__file__).resolve().parent / "snapshots.jsonl"
        _snap = {}
        if _snapf.exists():
            _slines = [l for l in _snapf.read_text(encoding="utf-8").splitlines() if l.strip()]
            if _slines:
                _snap = _json.loads(_slines[-1])
        if _snap:
            _agent_detail["_asof"] = str(_snap.get("date", ""))
            _wr = _snap.get("workforce_risk", 0)
            _agent_detail["biz"] = {
                "title": "TODAY · WORKFORCE", "stats": [
                    {"n": _wr, "l": "RISK COMBOS (30D)", "s": "bad" if _wr > 50 else "warn" if _wr > 10 else "ok"},
                    {"n": _snap.get("expiring_7d", 0), "l": "CERTS EXPIRING ≤7D", "s": "bad"},
                    {"n": _snap.get("expiring_30d", 0), "l": "CERTS EXPIRING ≤30D", "s": "warn"},
                    {"n": _snap.get("deployable", 0), "l": "DEPLOYABLE NOW", "s": "ok"},
                    {"n": _snap.get("idle_pool", 0), "l": "IDLE POOL (BENCH)", "s": ""}],
                "lines": [["Expired certs (total)", f"{_snap.get('expired_total', 0):,}"],
                          ["Active projects", str(_snap.get("active_projects", 0))],
                          ["Top-risk supplier", f"{_snap.get('top_supplier_name', '-')} ({_snap.get('top_supplier_expired', 0)} expired)"]],
                "alert": (f"{_wr} rostered-worker × expired-cert combos (30d). Re-validate certs for rostered workers first." if _wr > 50 else "")}
            _xa = _snap.get("xero_age_days", 0)
            _agent_detail["fin"] = {
                "title": "FINANCE · RECEIVABLES", "stats": [
                    {"n": _snap.get("ar_count", 0), "l": "OUTSTANDING INVOICES", "s": "warn"},
                    {"n": _fmt_money(_snap.get("ar_total", 0)), "l": "RECEIVABLES TOTAL", "s": "warn"},
                    {"n": f"{_xa}d", "l": "XERO DATA AGE", "s": "bad" if _xa > 30 else "ok"}],
                "lines": [[f"Revenue {_snap.get('rev_last_month_label', '')}", _fmt_money(_snap.get('rev_last_month', 0))]],
                "alert": f"Xero sync stale ({_xa}d) — figures end ~2026-04. invoice_register table currently unreadable; finance views may be incomplete."}
            _ss = _snap.get("sentinel_status", "?")
            _af = _snap.get("automation_failures", 0)
            _ga = _snap.get("gold_age_hours", 0)
            _rh = _snap.get("roster_horizon_days", 0)
            _agent_detail["ops"] = {
                "title": "PIPELINE · HEALTH", "stats": [
                    {"n": _ss, "l": "DATA SENTINEL", "s": "ok" if _ss == "OK" else "bad"},
                    {"n": f"{_ga:.0f}h", "l": "GOLD DATA AGE", "s": "ok" if _ga < 30 else "warn"},
                    {"n": f"{_rh}d", "l": "ROSTER HORIZON", "s": "ok" if _rh >= 60 else "warn"},
                    {"n": _af, "l": "AUTOMATION FAILS", "s": "ok" if _af == 0 else "bad"}],
                "lines": [["Last full week hours", f"{_snap.get('hours_last', 0):,.0f}"],
                          ["Week-on-week", f"{_snap.get('hours_wow_change_pct', 0):+.0f}%"]],
                "alert": ("Data sentinel not OK — open data_quality_report." if _ss != "OK" else "")}
        # FILE agent ← folder link health (already loaded above as _lhd)
        _fb = sum(v.get("broken", 0) for v in _lhd.values() if isinstance(v, dict) and "broken" in v)
        _fm = sum(v.get("missing", 0) for v in _lhd.values() if isinstance(v, dict) and "missing" in v)
        _agent_detail["file"] = {
            "title": "FILES · LINK HEALTH", "stats": [
                {"n": _fb, "l": "BROKEN LINKS", "s": "bad" if _fb else "ok"},
                {"n": _fm, "l": "MISSING LINKS", "s": "warn" if _fm else "ok"}],
            "lines": [["Checked", (str(_lhd.get("checked_at", ""))[:16].replace("T", " ") + " UTC") if _lhd.get("checked_at") else "—"]],
            "alert": (f"{_fb} broken / {_fm} missing folder links in JMS-Jobs." if (_fb or _fm) else "")}
    except Exception:
        _agent_detail = {}
    _html = _html.replace("var AGENTDETAIL = {};  /*__AGENTDETAIL__*/",
                          "var AGENTDETAIL = " + _json.dumps(_agent_detail, ensure_ascii=False, default=str) + ";")

    # ---- P1: inject unified Issue Registry (reuse _iv already built for the Action Console) ----
    _html = _html.replace("var ISSUES = [];  /*__ISSUES__*/",
                          "var ISSUES = " + _json.dumps(_iv.get("issues", []), ensure_ascii=False, default=str) + ";")
    _html = _html.replace("var NODEHEALTH = {};  /*__NODEHEALTH__*/",
                          "var NODEHEALTH = " + _json.dumps(_iv.get("by_node", {}), ensure_ascii=False, default=str) + ";")
    _html = _html.replace("var ALERTCOUNT = null;  /*__ALERTCOUNT__*/",
                          "var ALERTCOUNT = " + _json.dumps(_iv.get("alert_count", 0)) + ";")
    _html = _html.replace("var NODEMETA = {};  /*__NODEMETA__*/",
                          "var NODEMETA = " + _json.dumps(_iv.get("node_meta", {}), ensure_ascii=False, default=str) + ";")
    _html = _html.replace("var ENTITIES = {};  /*__ENTITIES__*/",
                          "var ENTITIES = " + _json.dumps(_iv.get("entities", {}), ensure_ascii=False, default=str) + ";")

    components.html(_html, height=900, scrolling=False)

elif page.startswith("Operations"):
    from lulu_time import perth_now
    st.title("Lulu Operations Center")
    st.caption(f"Acme Group workforce intelligence · Gold layer · {perth_now():%a %d %b %Y %H:%M} AWST")

    # ---- load the live issue stream (same engine the cockpit/Action Console use) ----
    from pathlib import Path as _PathD
    import sys as _sysD
    _ckptD = str(_PathD(__file__).resolve().parent / "cockpit")
    if _ckptD not in _sysD.path:
        _sysD.path.insert(0, _ckptD)
    _issues, _iregD = [], None
    try:
        import issue_registry as _iregD
        _issues = [dict(i) for i in _iregD.build().get("issues", []) if isinstance(i, dict)]
        try:                                          # attach the live (audit-derived) status
            import action_log as _alogD
            for _i in _issues:
                _i["status"] = _alogD.issue_status(_i.get("id")) or _i.get("status", "open")
        except Exception:
            pass
    except Exception:
        _issues = []
    _SEVRANK = {"critical": 4, "high": 3, "medium": 2, "low": 1}
    _SEVICON = {"critical": "🔴", "high": "🟠", "medium": "🟡", "low": "🔵"}
    _issues.sort(key=lambda i: _SEVRANK.get(i.get("severity"), 0), reverse=True)

    # ===== Operations Today — conclusion first: what LuLu found, before any chart =====
    _crit = sum(1 for i in _issues if i.get("severity") == "critical")
    _high = sum(1 for i in _issues if i.get("severity") == "high")
    _med = sum(1 for i in _issues if i.get("severity") == "medium")
    _low = sum(1 for i in _issues if i.get("severity") == "low")
    _attention = _crit + _high
    _pending = sum(1 for i in _issues if i.get("status") == "pending_approval")
    _missing = sum(1 for i in _issues if i.get("type") in ("missing_field", "data_freshness"))
    _autofix = sum(1 for i in _issues if i.get("safety") == "safe_auto" and i.get("repairable"))

    st.markdown("#### Operations Today")
    st.markdown(f"LuLu identified **{len(_issues)}** operational issue{'s' if len(_issues) != 1 else ''} today. "
                f"**{_attention}** require management attention."
                + (f"  ·  **{_pending}** awaiting your approval." if _pending else ""))

    # freshness line (trust): per-source ok / warn / stale
    try:
        import ops_assistant as _oaD
        _ficon = {"ok": "🟢", "warn": "🟡", "stale": "🔴"}
        _fbits = []
        for _k2, _v2 in _oaD.source_status().items():
            _stt = _v2.get("status")
            _note = f" ({_v2.get('note')})" if (_stt != "ok" and _v2.get("note")) else ""
            _fbits.append(f"{_ficon.get(_stt, '·')} {_k2}{_note}")
        if _fbits:
            st.caption("Data freshness:  " + "    ".join(_fbits))
    except Exception:
        pass

    k = kpis()
    try:
        _bx = business_exposure()
    except Exception:
        _bx = {}

    # ---- LuLu recommends — the three moves that matter, before any numbers ----
    _reccards = []
    for _i in _issues:                                    # top open issues by severity first
        if _i.get("status") in ("resolved", "risk_accepted", "snoozed"):
            continue
        _reccards.append({
            "title": _i.get("suggested_fix") or _i.get("title", ""),
            "why": _i.get("title", ""),
            "impact": _i.get("business_impact", ""),
        })
        if len(_reccards) == 2:
            break
    if k.get("deployable"):                               # always close with the opportunity
        _reccards.append({
            "title": "Match available workers to upcoming jobs",
            "why": f"{k['deployable']} fully compliant workers are currently not rostered.",
            "impact": "Backfill upcoming shortages from this pool before contacting additional suppliers.",
        })
    if _reccards:
        with st.container(border=True):
            st.subheader("💡 LuLu recommends")
            for _n, _rc in enumerate(_reccards[:3], 1):
                st.markdown(f"**{_n}. {_rc['title']}**")
                if _rc.get("why"):
                    st.markdown(_rc["why"])
                if _rc.get("impact"):
                    st.caption(f"Impact: {_rc['impact']}")
            st.caption("Act on these in the Issue Queue below, or System Galaxy → Action Console for gated repairs.")

    # ---- the four numbers that matter (business framing, not record counts) ----
    _rk = st.columns(4)
    with _rk[0], st.container(border=True):
        st.metric("Management attention", _attention,
                  help="Critical + high issues open right now")
    with _rk[1], st.container(border=True):
        st.metric("Workers at risk", _bx.get("workers_at_risk", k.get("workforce_risk", 0)),
                  help="Active workers holding at least one expired certificate")
    with _rk[2], st.container(border=True):
        st.metric("Jobs exposed", _bx.get("jobs_exposed", 0),
                  help="Upcoming projects with at-risk workers on the roster")
    with _rk[3], st.container(border=True):
        st.metric("Deployable now", k.get("deployable", 0),
                  help="Fully compliant field workers not currently rostered")

    # severity + record-to-people translation as small chips, not a full row of cards
    st.caption(f"🔴 {_crit} Critical · 🟠 {_high} High · 🟡 {_med} Medium · 🔵 {_low} Low"
               + (f" · ⏳ {_pending} pending approval" if _pending else "")
               + (f" · {_missing} data health issue{'s' if _missing != 1 else ''}" if _missing else ""))
    if _bx:
        st.caption(f"{k.get('expired_total', 0):,} expired certificate records → "
                   f"**{_bx.get('workers_at_risk', 0)} active workers** affected · "
                   f"{_bx.get('at_risk_rostered', 0)} currently rostered · "
                   f"{_bx.get('jobs_exposed', 0)} upcoming jobs exposed · "
                   f"{k.get('expiring_30d', 0)} certificates expiring within 30 days")

    # ===== Issue Queue — the unified problem list (one detected issue per row) =====
    with st.container(border=True):
        st.subheader("🚨 Issue Queue")
        if not _issues:
            st.success("No open issues detected right now.")
        else:
            import pandas as _pdQ

            def _src_of(i):
                return str(i.get("evidence_source", "")).split(":")[0] or i.get("node", "")

            _fq = st.columns(4)
            _sev_opts = [s for s in ["critical", "high", "medium", "low"]
                         if any(i.get("severity") == s for i in _issues)]
            _src_opts = sorted({_src_of(i) for i in _issues})
            _typ_opts = sorted({i.get("type", "") for i in _issues})
            _sta_opts = sorted({i.get("status", "open") for i in _issues})
            _fsev = _fq[0].multiselect("Severity", _sev_opts, key="iq_sev", placeholder="All severities")
            _fsrc = _fq[1].multiselect("Source", _src_opts, key="iq_src", placeholder="All sources")
            _ftyp = _fq[2].multiselect("Type", _typ_opts, key="iq_typ", placeholder="All types")
            _fsta = _fq[3].multiselect("Status", _sta_opts, key="iq_sta", placeholder="All statuses")

            _rows = []
            for i in _issues:
                if _fsev and i.get("severity") not in _fsev:
                    continue
                if _fsrc and _src_of(i) not in _fsrc:
                    continue
                if _ftyp and i.get("type") not in _ftyp:
                    continue
                if _fsta and i.get("status", "open") not in _fsta:
                    continue
                _rows.append({
                    "Priority": _SEVICON.get(i.get("severity"), "·") + " " + str(i.get("severity", "")),
                    "Issue": i.get("title", ""),
                    "Business impact": i.get("business_impact")
                                       or (f"{i.get('affected_count', 0):,} records affected"
                                           if i.get("affected_count") else ""),
                    "Owner": i.get("owner", ""),
                    "Recommended action": i.get("suggested_fix", ""),
                    "Status": i.get("status", "open"),
                    "_id": i.get("id")})

            _hq = st.columns([6, 1])
            _hq[0].caption(f"{len(_rows)} of {len(_issues)} issue(s) · sorted by severity · click a row for evidence")
            if not _rows:
                st.caption("No issues match the filters.")
            else:
                _dfq = _pdQ.DataFrame(_rows)
                _hq[1].download_button("⬇ CSV", _dfq.drop(columns=["_id"]).to_csv(index=False).encode("utf-8"),
                                       file_name="lulu_issue_queue.csv", mime="text/csv",
                                       use_container_width=True, key="iq_dl")
                _evq = st.dataframe(_dfq.drop(columns=["_id"]), use_container_width=True, hide_index=True,
                                    on_select="rerun", selection_mode="single-row", key="iq_tbl")
                try:
                    _selrows = _evq.selection.rows if hasattr(_evq, "selection") else _evq["selection"]["rows"]
                except Exception:
                    _selrows = []
                if _selrows:
                    _det = next((x for x in _issues if x.get("id") == _rows[_selrows[0]]["_id"]), None)
                    if _det:
                        with st.container(border=True):
                            st.markdown(f"**{_SEVICON.get(_det.get('severity'), '·')} {_det.get('title', '')}**")
                            st.caption(f"owner {_det.get('owner')} · status `{_det.get('status', 'open')}`")
                            if _det.get("business_impact"):
                                st.markdown(f"**Business impact:** {_det['business_impact']}")
                            if _det.get("suggested_fix"):
                                st.markdown(f"**Recommended action:** {_det['suggested_fix']}")

                            # ---- work the issue right here: approve / assign / run / snooze / accept ----
                            _iidQ = _det.get("id")
                            _refQ = _det.get("action_ref")
                            _safeQ = _det.get("safety")
                            _canlog = "_alogD" in dir() and _alogD is not None
                            _bq = st.columns(5)
                            if _refQ and _det.get("repairable") and _safeQ == "safe_auto":
                                if _bq[0].button("▶ Run repair", key=f"iq_run_{_iidQ}", use_container_width=True,
                                                 help="Safe automatic repair — runs immediately, fully audited"):
                                    try:
                                        import action_runner as _arunQ
                                        _rQ = _arunQ.run_action(_refQ, mode="execute", issue=_det)
                                        _alogD.log_event(_iidQ, _refQ, "executed",
                                                         result=_rQ.get("result", "ok"),
                                                         detail=_rQ.get("output", "")[:500])
                                        st.success("Repair executed — see audit trail in the Action Console.")
                                    except Exception as _exQ:
                                        st.error(f"Repair failed: {_exQ}")
                                    st.rerun()
                            elif _refQ and _det.get("repairable"):
                                if _bq[0].button("✅ Approve & run", key=f"iq_appr_{_iidQ}", use_container_width=True,
                                                 help="This repair writes data — clicking is your approval; every step is audited"):
                                    try:
                                        import action_runner as _arunQ
                                        _alogD.log_event(_iidQ, _refQ, "approved")
                                        _rQ = _arunQ.run_action(_refQ, mode="execute", issue=_det, approved=True)
                                        _alogD.log_event(_iidQ, _refQ, "executed",
                                                         result=_rQ.get("result", "ok"),
                                                         detail=_rQ.get("output", "")[:500])
                                        st.success("Approved and executed — see audit trail in the Action Console.")
                                    except Exception as _exQ:
                                        st.error(f"Repair failed: {_exQ}")
                                    st.rerun()
                            else:
                                _bq[0].button("▶ Run repair", key=f"iq_norun_{_iidQ}", disabled=True,
                                              use_container_width=True, help="No automated repair bound to this issue")
                            with _bq[1].popover("👤 Assign", use_container_width=True):
                                _ownQ = st.text_input("Owner", value=str(_det.get("owner") or ""),
                                                      key=f"iq_own_{_iidQ}")
                                if st.button("Assign owner", key=f"iq_assign_{_iidQ}") and _canlog:
                                    _alogD.log_event(_iidQ, "assign_owner", "assigned", detail=_ownQ)
                                    st.rerun()
                            if _bq[2].button("💤 Snooze 7d", key=f"iq_snz_{_iidQ}", use_container_width=True,
                                             help="Hide from recommendations for a week — stays in the queue"):
                                if _canlog:
                                    _alogD.log_event(_iidQ, "snooze", "snoozed", detail="7 days")
                                st.rerun()
                            if _bq[3].button("⚠ Accept risk", key=f"iq_acc_{_iidQ}", use_container_width=True,
                                             help="Record a deliberate decision to accept this risk (audited)"):
                                if _canlog:
                                    _alogD.log_event(_iidQ, "accept_risk", "risk_accepted")
                                st.rerun()
                            _bq[4].caption("")

                            # technical detail lives BELOW the decision, not in the main table
                            with st.expander("Technical details", expanded=False):
                                st.caption(f"`{_det.get('id')}` · type `{_det.get('type')}` "
                                           f"· entity `{_det.get('entity', '')}` "
                                           f"· source `{_det.get('evidence_source', '')}` "
                                           f"· safety `{_safeQ}`")
                                _evit = _det.get("evidence_items") or []
                                if _evit:
                                    st.dataframe(_pdQ.DataFrame(_evit), use_container_width=True, hide_index=True)
                                try:
                                    _histQ = _alogD.read_events(limit=10, issue_id=_iidQ) if _canlog else []
                                except Exception:
                                    _histQ = []
                                if _histQ:
                                    st.caption("Audit history:")
                                    for _e in _histQ:
                                        st.caption(f"{_e.get('ts', '')[:19].replace('T', ' ')} · "
                                                   f"{_e.get('action_key')} → {_e.get('status')}"
                                                   + (f" ({_e.get('detail')})" if _e.get("detail") else ""))

    # ---- Today's Brief (the 07:00 email, on the dashboard too) ----
    _briefs = sorted((AGENT_DIR / "logs" / "briefs").glob("brief_*.md"))
    if _briefs:
        with st.container(border=True):
            _latest = _briefs[-1]
            with st.expander(f"Today's Brief — emailed daily at 07:00 ({_latest.stem[6:]})",
                             expanded=False):
                st.markdown(_latest.read_text(encoding="utf-8"))

    st.subheader("Workforce Command Center")
    cc = command_center()

    import pandas as pd
    import plotly.express as px

    # ---- cool cyan-mint palette (matches the UI chrome) — warm tones reserved for warnings/peaks ----
    CYAN = "#46e0ff"; MINT = "#7cf0d8"; AQUA = "#3fc9d6"; COOL_SOFT = "#5fd6c4"
    CORAL = "#e76f51"; AMBER = "#e9c46a"
    DONUT_SEQ = ["#46e0ff", "#7cf0d8", "#3fc9d6", "#2a8fb8", "#3b5b86",
                 "#5b6f9e", "#8d99ae", "#b8c4d6"]

    try:                                      # follow the user's Streamlit theme
        _dark = st.context.theme.type == "dark"
    except Exception:
        _dark = False
    _font = "#e8e8ea" if _dark else "#31333f"
    _muted = "#9aa3b2" if _dark else "#6b7280"

    def _pbi(fig, h=330):
        """Power BI look: theme-aware, no gridline clutter, labels on the data."""
        fig.update_layout(template="plotly_dark" if _dark else "plotly_white",
                          paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
                          height=h, margin=dict(l=10, r=16, t=44, b=10),
                          font=dict(size=13, color=_font),
                          title_font=dict(size=15, color=_font), showlegend=False)
        fig.update_xaxes(showgrid=False, zeroline=False, tickfont=dict(color=_muted))
        fig.update_yaxes(showgrid=False, zeroline=False, tickfont=dict(color=_muted))
        return fig

    # ---- interactive (Tableau-like): global filters + click cross-filter drive all 5 charts ----
    _w = wcc_data()
    _emp, _tc, _ros, _wts = _w["emp"], _w["tc"], _w["ros"], _w["wts"]
    if "wcc_xf" not in st.session_state:
        st.session_state.wcc_xf = {"supplier": None, "project": None, "month": None}
    _xf = st.session_state.wcc_xf

    def _clickval(ev, field):
        try:
            pts = ev["selection"]["points"] if isinstance(ev, dict) else ev.selection.points
            if pts:
                return pts[0].get(field)
        except Exception:
            pass
        return None

    def _consume(ev, key, dim, field):
        """fire only when THIS chart's selection changed (prevents click ping-pong)."""
        v = _clickval(ev, field)
        if v != st.session_state.get("wcc_last_" + key):
            st.session_state["wcc_last_" + key] = v
            if v:
                _xf[dim] = v
                st.session_state["wcc_focus"] = key   # remember which chart was last clicked (drives Detail)
                st.rerun()

    _sup_opts = sorted(_emp["supplier_name"].dropna().unique()) if not _emp.empty else []
    _prj_opts = sorted(set(_ros["project_name"].dropna().unique()) | set(_wts["project_name"].dropna().unique())) \
        if (not _ros.empty or not _wts.empty) else []
    fbar = st.columns([3, 3, 3, 2])
    _sel_sup = fbar[0].multiselect("Supplier", _sup_opts, key="wcc_sup", placeholder="All suppliers")
    _sel_prj = fbar[1].multiselect("Job", _prj_opts, key="wcc_prj", placeholder="All jobs")
    # ---- global time range: ONE control for every dated view (hours / roster / cert expiry) ----
    _today = pd.Timestamp.now().normalize()
    _RANGES = {
        "Last 90d → next 6mo": (_today - pd.Timedelta(days=90), _today + pd.DateOffset(months=6)),
        "Next 30 days": (_today, _today + pd.Timedelta(days=30)),
        "Next 90 days": (_today, _today + pd.Timedelta(days=90)),
        "Next 6 months": (_today, _today + pd.DateOffset(months=6)),
        "Next 12 months": (_today, _today + pd.DateOffset(months=12)),
        "Last 90 days": (_today - pd.Timedelta(days=90), _today),
        "Last 12 months": (_today - pd.DateOffset(months=12), _today),
    }
    _rchoice = fbar[2].selectbox("📅 Time range", list(_RANGES.keys()) + ["Custom…"], key="wcc_range",
                                 help="Filters the dated charts below: workforce hours, roster risk, certificate expiry.")
    if fbar[3].button("↺ Clear click-filter", use_container_width=True):
        st.session_state.wcc_xf = {"supplier": None, "project": None, "month": None}
        st.rerun()
    if _rchoice == "Custom…":
        _dr = st.date_input("Custom date range",
                            value=((_today - pd.Timedelta(days=90)).date(), (_today + pd.DateOffset(months=6)).date()),
                            key="wcc_range_custom")
        if isinstance(_dr, (list, tuple)) and len(_dr) == 2:
            _gts, _gte = pd.Timestamp(_dr[0]), pd.Timestamp(_dr[1])
        else:
            _gts, _gte = _today - pd.Timedelta(days=90), _today + pd.DateOffset(months=6)
    else:
        _gts, _gte = _RANGES[_rchoice]
    st.caption(f"📅 Showing **{_gts:%d %b %Y} → {_gte:%d %b %Y}** · drives workforce hours, roster risk & cert-expiry below")

    _SUP = set(_sel_sup) | ({_xf["supplier"]} if _xf["supplier"] else set())
    _PRJ = set(_sel_prj) | ({_xf["project"]} if _xf["project"] else set())
    _chips = []
    if _SUP:
        _chips.append("supplier ∈ {" + ", ".join(sorted(_SUP)) + "}")
    if _PRJ:
        _chips.append("job ∈ {" + ", ".join(list(_PRJ)[:3]) + ("…" if len(_PRJ) > 3 else "") + "}")
    if _xf["month"]:
        _chips.append("expiring " + str(_xf["month"]))
    if _chips:
        st.caption("🔎 " + "  ·  ".join(_chips) + "  — click a bar/slice to cross-filter, ↺ to reset.")

    _wids = None
    if _SUP and not _emp.empty:
        _s = set(_emp[_emp.supplier_name.isin(_SUP)].opms_employee_id)
        _wids = _s if _wids is None else (_wids & _s)
    if _PRJ:
        _p = set(_ros[_ros.project_name.isin(_PRJ)].opms_employee_id) | set(_wts[_wts.project_name.isin(_PRJ)].opms_employee_id)
        _wids = _p if _wids is None else (_wids & _p)

    def _wf(frame):
        return frame if (_wids is None or "opms_employee_id" not in frame) else frame[frame.opms_employee_id.isin(_wids)]

    _supmap = _emp[["opms_employee_id", "supplier_name"]] if not _emp.empty else pd.DataFrame(columns=["opms_employee_id", "supplier_name"])

    a, b = st.columns(2)
    with a, st.container(border=True):          # ---- expired certs per supplier ----
        _e = _wf(_tc[_tc["is_expired"] == True]).merge(_supmap, on="opms_employee_id", how="left")
        g = _e.groupby("supplier_name").size().reset_index(name="expired_certs") \
            .sort_values("expired_certs", ascending=False).head(8)
        if not g.empty:
            colors = [CORAL if v == g["expired_certs"].max() else COOL_SOFT for v in g["expired_certs"]]
            fig = px.bar(g, x="expired_certs", y="supplier_name", orientation="h", text="expired_certs",
                         title="Compliance risk by supplier")
            fig.update_traces(marker_color=colors, textposition="outside", cliponaxis=False)
            fig.update_yaxes(categoryorder="total ascending", title=None)
            fig.update_xaxes(title=None, visible=False)
            _consume(st.plotly_chart(_pbi(fig), use_container_width=True, on_select="rerun", key="xf_sup"),
                     "xf_sup", "supplier", "y")
        else:
            st.caption("No expired certs match the filter.")
    with b, st.container(border=True):          # ---- at-risk workers per project (90d) ----
        _rd = _ros.copy()
        _rd["rd"] = pd.to_datetime(_rd["roster_date"], errors="coerce")
        _expired_ids = set(_tc[_tc["is_expired"] == True].opms_employee_id)
        _rr = _wf(_rd[(_rd.rd >= _gts) & (_rd.rd <= _gte) & (_rd.opms_employee_id.isin(_expired_ids))])
        g = _rr.groupby("project_name").opms_employee_id.nunique().reset_index(name="at_risk_workers") \
            .sort_values("at_risk_workers", ascending=False).head(8)
        if not g.empty:
            g["project_name"] = g["project_name"].str.slice(0, 38)
            fig = px.bar(g, x="at_risk_workers", y="project_name", orientation="h", text="at_risk_workers",
                         title="Roster risk by job", color_discrete_sequence=[AQUA])
            fig.update_yaxes(categoryorder="total ascending", title=None)
            fig.update_xaxes(title=None, visible=False)
            fig.update_traces(textposition="outside", cliponaxis=False)
            _consume(st.plotly_chart(_pbi(fig), use_container_width=True, on_select="rerun", key="xf_prj"),
                     "xf_prj", "project", "y")
        else:
            st.caption("No at-risk roster matches the filter.")

    c, d = st.columns(2)
    with c, st.container(border=True):          # ---- certs expiring next 6 months ----
        _f = _wf(_tc).copy()
        _f["exp"] = pd.to_datetime(_f["expiry_date"], errors="coerce")
        _f = _f[(_f.exp >= _gts) & (_f.exp <= _gte)]
        _f["month"] = _f.exp.dt.strftime("%Y-%m")
        g = _f.groupby("month").size().reset_index(name="certs_expiring").sort_values("month")
        if not g.empty:
            colors = [CORAL if v == g["certs_expiring"].max() else COOL_SOFT for v in g["certs_expiring"]]
            fig = px.bar(g, x="month", y="certs_expiring", text="certs_expiring", title="Certs expiring")
            fig.update_traces(marker_color=colors, textposition="outside", cliponaxis=False)
            fig.update_xaxes(title=None, type="category")
            fig.update_yaxes(title=None, visible=False)
            _consume(st.plotly_chart(_pbi(fig), use_container_width=True, on_select="rerun", key="xf_mon"),
                     "xf_mon", "month", "x")
        else:
            st.caption("No upcoming expiries match the filter.")
    with d, st.container(border=True):          # ---- active workforce by supplier (donut) ----
        _ae = _wf(_emp[_emp["is_active"] == True])
        g = _ae.groupby("supplier_name").size().reset_index(name="workers") \
            .sort_values("workers", ascending=False).head(10)
        if not g.empty:
            fig = px.pie(g, values="workers", names="supplier_name", hole=.62,
                         title="Active workforce by supplier", color_discrete_sequence=DONUT_SEQ)
            fig.update_traces(textinfo="label+value", textposition="outside",
                              marker=dict(line=dict(color="rgba(0,0,0,0)", width=2)))
            fig.add_annotation(text=f"<b>{int(g['workers'].sum())}</b><br>active", showarrow=False,
                               font=dict(size=18, color=_font))
            fig.update_layout(showlegend=False)
            _consume(st.plotly_chart(_pbi(fig), use_container_width=True, on_select="rerun", key="xf_don"),
                     "xf_don", "supplier", "label")
        else:
            st.caption("No active workers match the filter.")

    # ---- Drill-through Detail: the raw rows behind whichever bar/slice you clicked ----
    # Left-click a chart above (Plotly can't do a browser right-click menu); this panel
    # then lists the underlying records and adapts its columns to that chart.
    with st.container(border=True):
        _focus = st.session_state.get("wcc_focus")
        if _focus == "xf_prj" and _xf["project"]:                 # Roster risk by job → at-risk roster rows
            _rd2 = _ros.copy()
            _rd2["rd"] = pd.to_datetime(_rd2["roster_date"], errors="coerce")
            _exp_ids = set(_tc[_tc["is_expired"] == True].opms_employee_id)
            det = _wf(_rd2[((_rd2.rd >= _gts) & (_rd2.rd <= _gte))
                           & (_rd2.opms_employee_id.isin(_exp_ids))
                           & (_rd2.project_name == _xf["project"])]).merge(_supmap, on="opms_employee_id", how="left")
            det = det[["first_name", "last_name", "supplier_name", "project_name",
                       "position_name", "roster_date", "hours"]].sort_values("roster_date", ascending=False)
            _cap = f"at-risk roster rows · job = {_xf['project']} · expired-cert holders in selected range"
        elif _focus == "xf_mon" and _xf["month"]:                 # Certs expiring → that month's certs
            det = _wf(_tc).copy()
            det["exp"] = pd.to_datetime(det["expiry_date"], errors="coerce")
            det = det[det.exp.dt.strftime("%Y-%m") == _xf["month"]].merge(_supmap, on="opms_employee_id", how="left")
            det = det[["first_name", "last_name", "supplier_name", "competency_name",
                       "status", "expiry_date", "days_to_expiry"]].sort_values("expiry_date")
            _cap = f"certificates expiring in {_xf['month']}"
        else:                                                     # supplier / donut / nothing → expired certs in scope
            det = _wf(_tc[_tc["is_expired"] == True]).merge(_supmap, on="opms_employee_id", how="left")
            det = det[["first_name", "last_name", "supplier_name", "competency_name",
                       "status", "expiry_date", "days_to_expiry"]].sort_values("expiry_date")
            _cap = "expired certificates in current scope" + (f" · supplier = {_xf['supplier']}" if _xf.get("supplier") else "")

        _hh = st.columns([6, 1])
        _hh[0].markdown(f"**Detail — {len(det):,} row(s)**  ·  {_cap}")
        if det.empty:
            st.caption("Click a bar or slice above to drill into the rows behind it.")
        else:
            _hh[1].download_button("⬇ CSV", det.to_csv(index=False).encode("utf-8"),
                                   file_name="lulu_detail.csv", mime="text/csv",
                                   use_container_width=True, key="wcc_det_dl")
            st.dataframe(det.rename(columns={
                "first_name": "First", "last_name": "Last", "supplier_name": "Supplier",
                "competency_name": "Competency", "status": "Status", "expiry_date": "Expiry",
                "days_to_expiry": "Days to expiry", "project_name": "Job", "position_name": "Position",
                "roster_date": "Roster date", "hours": "Hours"}),
                use_container_width=True, hide_index=True, height=300)

    # ---- weekly actual hours (filtered by supplier/project directly on the timesheet) ----
    _h = _wts.copy()
    if _SUP and "supplier_name" in _h:
        _h = _h[_h.supplier_name.isin(_SUP)]
    if _PRJ and "project_name" in _h:
        _h = _h[_h.project_name.isin(_PRJ)]
    if "work_date" in _h:                          # honour the global time range
        _hwd = pd.to_datetime(_h["work_date"], errors="coerce")
        _h = _h[(_hwd >= _gts) & (_hwd <= _gte)]
    if not _h.empty:
        _h["wk"] = pd.to_datetime(_h["work_date"], errors="coerce").dt.to_period("W").dt.start_time
        g = _h.groupby("wk")["actual_hours"].sum().reset_index().dropna().sort_values("wk")
        if not g.empty:
            _card_hours = st.container(border=True)
            g["week"] = g.wk.dt.strftime("%Y-%m-%d")
            fig = px.area(g, x="week", y="actual_hours", markers=True,
                          title="Actual worked hours per week (weekly timesheet logic)", color_discrete_sequence=[AQUA])
            fig.update_traces(line=dict(width=3), fillcolor="rgba(42,157,143,.18)")
            fig.update_xaxes(title=None, type="category")
            fig.update_yaxes(title=None)
            with _card_hours:
                st.plotly_chart(_pbi(fig, h=300), use_container_width=True)

    e1, e2, e3 = st.columns(3)                 # ---- expiry ladder KPI strip ----
    lad = cc["expiry_ladder"]
    with e1, st.container(border=True):
        st.metric("Expiring ≤ 7 days", lad[7])
    with e2, st.container(border=True):
        st.metric("Expiring ≤ 30 days", lad[30])
    with e3, st.container(border=True):
        st.metric("Expiring ≤ 90 days", lad[90])

    t1, t2 = st.columns(2)
    with t1, st.container(border=True):
        st.markdown("**Most urgent (≤7 days)**")
        st.dataframe(cc["urgent"], use_container_width=True, hide_index=True)
    with t2, st.container(border=True):
        st.markdown(f"**Deployable now ({k['deployable']} fully compliant, not rostered)**")
        st.dataframe(cc["deployable"], use_container_width=True, hide_index=True)

    # ============ JMS Analysis (merged: JMS-Projects (X) JMS-Jobs) ============
    # Relationship: JMS-Projects (client/engagement, pipeline Status) → JMS-Jobs (work items, JobStatus).
    # JMS-Jobs.Project == JMS-Projects.ATitle (ProjectID-Title). Joined here on bms_project_id at JOB grain
    # so one sunburst drills Project-stage → Client → Project → Job-status → Job.
    st.divider()
    st.subheader("JMS Analysis")
    _pj = jms_projects()
    _jd = jms_jobs()
    if _jd.empty:
        st.caption("job_detail not available in Gold.")
    else:
        # cool cyan-mint family for good/neutral statuses; warm kept only as signals
        # (amber = In Progress / attention, coral = Unsuccesful, grey = Cancelled/UNSURE)
        _SMAP = {"COMPLETE/CLOSED": "#2a8fb8", "Complete- INVOICE": "#3fc9d6",
                 "In Progress": "#e9c46a", "Job Lead": "#5fd6c4", "Approved": "#46e0ff",
                 "Requested": "#5b6f9e", "Quote Sent": "#a78bda", "Repeat Order": "#7cf0d8",
                 "Cancelled": "#5b6675", "Unsuccesful": "#e76f51", "UNSURE": "#8d99ae"}
        # project attributes keyed by bms_project_id
        if not _pj.empty:
            _ac = _pj["project_code"].fillna("").astype(str).str.strip()
            _amap = {pid: (c + "-" + n) if c else n for pid, c, n
                     in zip(_pj["bms_project_id"], _ac, _pj["project_name"].astype(str))}
            _stmap = dict(zip(_pj["bms_project_id"], _pj["status"]))
            _clmap = dict(zip(_pj["bms_project_id"], _pj["client_name"]))
            _nopipe = int((_pj["job_count"] == 0).sum())
        else:
            _amap, _stmap, _clmap, _nopipe = {}, {}, {}, 0
        J = _jd.copy()
        _has_pid = "bms_project_id" in J
        J["atitle"] = (J["bms_project_id"].map(_amap).fillna(J["project_name"]) if _has_pid else J["project_name"])
        J["pstage"] = (J["bms_project_id"].map(_stmap).fillna("(no stage)") if _has_pid else "(no stage)")
        J["pclient"] = (J["bms_project_id"].map(_clmap).fillna(J["client_name"]) if _has_pid else J["client_name"])
        J["job_label"] = J["job_code"].replace("", pd.NA).fillna(J["job_title"]).fillna("(job)")
        st.caption(f"{len(_pj)} projects ({_nopipe} pipeline-only, no jobs yet) · {len(J)} jobs · "
                   f"{J['atitle'].nunique()} projects with jobs · source `gold:project_job_summary` join `job_detail`")

        # ---- cascading filters: each dropdown's options narrow by the OTHER two (+ active) ----
        # Read prior selections from session_state; each widget is re-sanitised against its
        # freshly-computed options before render, so shrinking option sets never crash Streamlit.
        _sel_st = st.session_state.get("jm_st", [])
        _sel_pr = st.session_state.get("jm_pr", [])
        _sel_js = st.session_state.get("jm_js", [])
        _sel_act = st.session_state.get("jm_act", False)

        def _jm_scope(skip):
            f = J
            if skip != "act" and _sel_act and "is_active" in f:
                f = f[f.is_active == True]
            if skip != "st" and _sel_st:
                f = f[f.pstage.isin(_sel_st)]
            if skip != "pr" and _sel_pr:
                f = f[f.atitle.isin(_sel_pr)]
            if skip != "js" and _sel_js:
                f = f[f.job_status.isin(_sel_js)]
            return f

        def _jm_ms(col, label, key, skip, srccol, ph):
            opts = sorted(_jm_scope(skip)[srccol].dropna().unique())
            if key in st.session_state:  # drop any now-invalid prior picks before render
                st.session_state[key] = [v for v in st.session_state[key] if v in opts]
            return col.multiselect(label, opts, placeholder=ph, key=key)

        f1, f2, f3, f4 = st.columns([3, 3, 3, 2])
        _fst = _jm_ms(f1, "Project stage", "jm_st", "st", "pstage", "All stages")
        _fpr = _jm_ms(f2, "Project (ID-Title)", "jm_pr", "pr", "atitle", "All projects")
        _fjs = _jm_ms(f3, "Job status", "jm_js", "js", "job_status", "All job statuses")
        _fact = f4.checkbox("Active jobs only", value=False, key="jm_act")
        d = J.copy()
        if _fst:
            d = d[d.pstage.isin(_fst)]
        if _fpr:
            d = d[d.atitle.isin(_fpr)]
        if _fjs:
            d = d[d.job_status.isin(_fjs)]
        if _fact and "is_active" in d:
            d = d[d.is_active == True]

        _open = ~d.job_status.str.contains("COMPLETE|Cancel|Unsucces", case=False, na=False)
        k1, k2, k3, k4 = st.columns(4)
        with k1, st.container(border=True):
            st.metric("Projects (with jobs)", d.atitle.nunique())
        with k2, st.container(border=True):
            st.metric("Jobs", len(d))
        with k3, st.container(border=True):
            st.metric("In Progress (jobs)", int(d.job_status.str.contains("Progress", case=False, na=False).sum()))
        with k4, st.container(border=True):
            st.metric("Open (not closed)", int(_open.sum()))

        if d.empty:
            st.info("No jobs match the filters.")
        else:
            if "jm_xf" not in st.session_state:
                st.session_state.jm_xf = {"stage": None, "client": None, "project": None, "status": None, "job": None}
            _x = st.session_state.jm_xf
            c1, c2 = st.columns([3, 2])
            _order = c1.radio("Drill order",
                              ["Stage → Client → Project → Job", "Project → Status → Job",
                               "Client → Project → Status", "Status → Project → Job"],
                              horizontal=True, key="jm_order")
            _chart = c2.radio("Chart", ["Sunburst", "Treemap"], horizontal=True, key="jm_chart")
            _PATHS = {"Stage → Client → Project → Job": ["pstage", "pclient", "atitle", "job_label"],
                      "Project → Status → Job": ["atitle", "job_status", "job_label"],
                      "Client → Project → Status": ["pclient", "atitle", "job_status"],
                      "Status → Project → Job": ["job_status", "atitle", "job_label"]}
            _g = d.assign(n=1)
            _mk = px.sunburst if _chart == "Sunburst" else px.treemap
            fig = _mk(_g, path=_PATHS[_order], values="n", color="job_status", color_discrete_map=_SMAP)
            fig.update_traces(hovertemplate="<b>%{label}</b><br>%{value} jobs<extra></extra>")
            fig.update_layout(height=520, margin=dict(t=12, l=4, r=4, b=4), uirevision="jm_sun",
                              paper_bgcolor="rgba(0,0,0,0)", font=dict(size=13, color=_font))
            _setSt, _setCl, _setPr = set(d.pstage), set(d.pclient), set(d.atitle)
            _setJs, _setJb = set(d.job_status), set(d.job_label)
            _ev = st.plotly_chart(fig, use_container_width=True, on_select="rerun", key="jm_sb")
            try:
                _pts = _ev["selection"]["points"] if isinstance(_ev, dict) else _ev.selection.points
                _lab = _pts[0].get("label") if _pts else None
            except Exception:
                _lab = None
            if _lab != st.session_state.get("jm_last_sb"):
                st.session_state["jm_last_sb"] = _lab
                if _lab:
                    if _lab in _setSt:
                        _x["stage"] = _lab
                    elif _lab in _setCl:
                        _x["client"] = _lab
                    elif _lab in _setPr:
                        _x["project"] = _lab
                    elif _lab in _setJs:
                        _x["status"] = _lab
                    elif _lab in _setJb:
                        _x["job"] = _lab
                    st.rerun()
            st.caption("Click a wedge to drill in AND list those jobs below · click the centre to zoom out.")

            sb = d.job_status.value_counts().reset_index()
            sb.columns = ["job_status", "jobs"]
            figb = px.bar(sb, x="jobs", y="job_status", orientation="h", text="jobs",
                          color="job_status", color_discrete_map=_SMAP, title="Jobs by status (filtered)")
            figb.update_yaxes(categoryorder="total ascending", title=None)
            figb.update_xaxes(title=None, visible=False)
            figb.update_traces(textposition="outside", cliponaxis=False)
            st.plotly_chart(_pbi(figb, h=max(220, 30 * len(sb))), use_container_width=True)

            jl = d
            _bits = []
            if _x["stage"]:
                jl = jl[jl.pstage == _x["stage"]]
                _bits.append("stage = " + str(_x["stage"]))
            if _x["client"]:
                jl = jl[jl.pclient == _x["client"]]
                _bits.append("client = " + str(_x["client"]))
            if _x["project"]:
                jl = jl[jl.atitle == _x["project"]]
                _bits.append("project = " + str(_x["project"]))
            if _x["status"]:
                jl = jl[jl.job_status == _x["status"]]
                _bits.append("job status = " + str(_x["status"]))
            if _x["job"]:
                jl = jl[jl.job_label == _x["job"]]
                _bits.append("job = " + str(_x["job"]))
            h1, h2 = st.columns([6, 1])
            h1.markdown(f"**Jobs — {len(jl)} job(s) · {jl.atitle.nunique()} project(s)**"
                        + ("  ·  " + " · ".join(_bits) if _bits else "  · (all filtered jobs — click a wedge to narrow)"))
            if h2.button("↺ Clear", key="jm_clear", use_container_width=True):
                st.session_state.jm_xf = {"stage": None, "client": None, "project": None, "status": None, "job": None}
                st.rerun()
            st.dataframe(
                jl[["job_code", "job_title", "atitle", "pstage", "job_status", "pclient", "lead"]].rename(
                    columns={"job_code": "Job", "job_title": "Title", "atitle": "Project (ID-Title)",
                             "pstage": "Project stage", "job_status": "Job status",
                             "pclient": "Client", "lead": "Lead (person)"}),
                use_container_width=True, hide_index=True)

    # ---- LuLu activity — proof of work, deliberately LAST (below business content) ----
    st.divider()
    st.subheader("LuLu activity")
    _scanned = 0
    if _iregD is not None:
        for _t in ("training_compliance", "roster_summary", "invoice_register"):
            try:
                _df = _iregD._read_gold(_t)
                if _df is not None:
                    _scanned += len(_df)
            except Exception:
                pass
    _acted = 0
    try:
        _logf = _PathD(__file__).resolve().parent / "logs" / "cockpit_action_log.jsonl"
        if _logf.exists():
            import json as _jjD
            from lulu_time import perth_now as _pnowD
            _todayD = f"{_pnowD():%Y-%m-%d}"
            for _ln in _logf.read_text(encoding="utf-8").splitlines():
                if not _ln.strip():
                    continue
                try:
                    _ev = _jjD.loads(_ln)
                    if _ev.get("status") in ("executed", "resolved", "approved") \
                            and str(_ev.get("ts", "")).startswith(_todayD):
                        _acted += 1
                except Exception:
                    pass
    except Exception:
        _acted = 0
    _ra = st.columns(4)
    with _ra[0], st.container(border=True):
        st.metric("Records checked", f"{_scanned:,}")
    with _ra[1], st.container(border=True):
        st.metric("Issues detected", len(_issues))
    with _ra[2], st.container(border=True):
        st.metric("Ready for approved repair", _autofix,
                  help="Repairs LuLu can run as soon as they are approved — nothing changes without approval")
    with _ra[3], st.container(border=True):
        st.metric("Actions completed today", _acted)

    st.divider()
    st.subheader("Recent actions")
    # privacy: each account sees ONLY its own questions (older rows have no user -> hidden for all)
    recs = [r for r in usage_logger().records if r.get("user") == user["email"]][-8:][::-1]
    if recs:
        for r in recs:
            ts = r.get("ts", "")[11:16]
            st.caption(f"{ts} · **{r.get('question','')[:70]}** → `{r.get('tool')}.{r.get('function')}` "
                       f"({r.get('row_count')} rows, {r.get('confidence')})")
    else:
        st.caption("No actions logged yet — ask Lulu something.")


# =====================================================================
# PAGE 2 — ASK LULU (multi-turn chat: follow-ups inherit context; 👍/👎 feeds the learning loop)
# =====================================================================
elif page.startswith("Ask"):
    import uuid

    st.title("Ask Lulu")
    st.caption("Multi-turn chat — follow-ups inherit context ('and this week?', 'what about CARTER?'). "
               "Every answer carries its trace; 👍/👎 feeds the learning loop.")

    if "conv_id" not in st.session_state:
        st.session_state.conv_id = uuid.uuid4().hex[:12]
        st.session_state.chat = []             # rendered messages
        st.session_state.engine_history = []   # context passed to the engines

    engines = ["Deterministic planner (no LLM, instant)"]
    _gs = gw_status()
    if _gs.get("planner", {}).get("available") or _gs.get("fallback", {}).get("available"):
        _p = _gs["planner"] if _gs["planner"]["available"] else _gs["fallback"]
        engines.append(f"LLM Gateway ({_p['provider']}/{_p['model']} reasons + picks tools)")
    # default to the LLM Gateway when it's available, else the deterministic planner.
    # Engine choice is developer plumbing — only admins see the switch; everyone else
    # gets the best available engine automatically.
    _default_engine = len(engines) - 1
    if _is_admin:
        engine = st.radio("Engine", engines, horizontal=True, index=_default_engine)
    else:
        engine = engines[_default_engine]
        st.caption("Answers are generated using verified operational tools.")

    import ops_assistant as _oa
    # ---- quick prompts: the boss doesn't need to know how to ask (grouped by ops area) ----
    with st.expander("💡 Quick questions — click one to ask", expanded=not st.session_state.chat):
        for _cat, _prompts in _oa.QUICK_PROMPTS.items():
            st.caption(f"**{_cat}**")
            _qcols = st.columns(len(_prompts))
            for _qj, (_qc, _qp) in enumerate(zip(_qcols, _prompts)):
                if _qc.button(_qp, key=f"qp_{_cat}_{_qj}", use_container_width=True):
                    st.session_state["_pending_q"] = _qp
                    st.rerun()
    ex = None

    tracelog = TraceLogger()

    # ---- render the conversation so far ----
    for i, m in enumerate(st.session_state.chat):
        with st.chat_message("user", avatar="🧑"):
            st.markdown(m["q"])
        with st.chat_message("assistant", avatar=ORB_ICON):
            conf = m["conf"]
            (st.success if conf == "High" else st.info if conf == "Medium" else st.warning)(m["answer"])
            for c in m["caveats"]:
                st.caption(f"Note: {c}")

            # ---- Operational answer contract: freshness · risk · missing data · actions ----
            _ctx = m.get("ctx") or {}
            if _ctx:
                if _ctx.get("freshness_warning"):
                    st.warning("⏳ " + _ctx["freshness_warning"])
                _fr = _ctx.get("freshness") or []
                if _fr:
                    st.caption("**Data freshness**")
                    for _fc, _f in zip(st.columns(len(_fr)), _fr):
                        _icon = "🟢" if _f.get("status") == "ok" else "🔴"
                        _fc.caption(f"{_icon} **{_f['source']}**  \n{_f.get('asof', '?')} · {_f.get('note', '')}")
                for _rk in (_ctx.get("risks") or []):
                    (st.error if _rk.get("severity") in ("critical", "high") else st.warning)(
                        f"⚠️ Risk · {_rk.get('title', '')}  ·  `{_rk.get('id', '')}`")
                _ms = _ctx.get("missing")
                if _ms:
                    st.info("🧩 Missing data — I won't guess: " + _ms.get("say", ""))
                    st.caption("Capture needed: " + " · ".join(_ms.get("need", [])))
                    if st.button("➕ " + _ms.get("action_label", "Capture requirement"), key=f"miss{i}"):
                        import sys as _s
                        _ck = str(AGENT_DIR / "cockpit")
                        if _ck not in _s.path:
                            _s.path.insert(0, _ck)
                        import action_log as _al
                        _al.log_event("ask-" + str(m.get("trace_id", i)), "create_requirement",
                                      "manual", detail="requested: " + _ms.get("what", ""))
                        st.success("Logged a request to capture this requirement (no production change).")
                if m.get("export_rows"):
                    import pandas as _pd2
                    st.dataframe(_pd2.DataFrame(m["export_rows"]).head(50),
                                 use_container_width=True, hide_index=True)
                _acts = _ctx.get("actions") or []
                if _acts:
                    st.caption("**Next actions** — creates an approval-gated draft, nothing runs:")
                    for _ac, _a in zip(st.columns(len(_acts)), _acts):
                        _badge = {"safe_auto": "🟢", "needs_approval": "🟠",
                                  "manual_only": "⚪"}.get(_a.get("safety"), "")
                        if _ac.button(f"{_badge} {_a['label']}", key=f"act{i}_{_a['key']}",
                                      use_container_width=True):
                            import sys as _s2
                            _ck2 = str(AGENT_DIR / "cockpit")
                            if _ck2 not in _s2.path:
                                _s2.path.insert(0, _ck2)
                            import action_log as _al2
                            _stt = "manual" if _a.get("safety") == "safe_auto" else "pending_approval"
                            _al2.log_event("ask-" + str(m.get("trace_id", i)), _a["key"], _stt,
                                           writes=_a.get("writes", False),
                                           detail="drafted from Ask LuLu: " + m.get("q", "")[:120])
                            st.success(f"Drafted '{_a['label']}' → {_stt}. Approve it in the Action Console.")

            with st.expander("Agent Trace"):
                for label, val in m["trace_steps"]:
                    st.markdown(f"**{label}**  \n`{val}`")
                if m["sqls"] and m["sqls"][0]:
                    st.code(m["sqls"][0].replace(" ;; ", "\n\n"), language="sql")
            fb = m.get("feedback")
            c1, c2, c4, c3 = st.columns([1, 1, 3, 7])
            if m.get("export_rows"):
                import io as _io
                import pandas as _pd
                _buf = _io.BytesIO()
                _pd.DataFrame(m["export_rows"]).to_excel(_buf, index=False, sheet_name="Lulu")
                c4.download_button("Export Excel", data=_buf.getvalue(),
                                   file_name=f"lulu_{m.get('trace_id', i)}.xlsx",
                                   mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                                   key=f"dl{i}", use_container_width=True)
            if fb:
                c3.caption("Feedback recorded: " + ("👍" if fb == "up" else "👎 (sent to Bug Inbox)"))
            else:
                if c1.button("👍", key=f"up{i}"):
                    tracelog.log_feedback(m["trace_id"], "thumbs_up")
                    m["feedback"] = "up"
                    st.rerun()
                if c2.button("👎", key=f"down{i}"):
                    tracelog.log_feedback(m["trace_id"], "thumbs_down — answer wrong/unhelpful",
                                          correction_flag=True)
                    m["feedback"] = "down"
                    st.rerun()

    # ---- paste bridge: Ctrl+V a screenshot/file straight into the chat input ----
    # (Streamlit 1.58 chat_input only supports the attach button + drag-drop; this JS
    #  forwards clipboard files from anywhere on the page into the hidden file input.)
    import streamlit.components.v1 as components
    components.html("""
    <script>
    (function () {
      let doc;
      try { doc = window.parent.document; } catch (e) { return; }
      if (doc.__luluPasteHookV3) return;            // versioned: old tabs re-arm after updates
      doc.__luluPasteHookV3 = true;
      console.log('[Lulu] paste bridge v3 armed');

      function note(msg, color) {
        const n = doc.createElement('div');
        n.textContent = msg;
        n.style.cssText = 'position:fixed;bottom:110px;left:50%;transform:translateX(-50%);' +
          'background:' + color + ';color:#fff;padding:6px 14px;border-radius:8px;' +
          'z-index:99999;font-size:13px;opacity:.96;box-shadow:0 2px 10px rgba(0,0,0,.35)';
        doc.body.appendChild(n);
        setTimeout(() => n.remove(), 2600);
      }

      function grabFiles(cd) {
        let files = Array.from((cd && cd.files) || []);
        if (!files.length && cd && cd.items) {
          for (const it of cd.items) {
            if (it.kind === 'file') { const f = it.getAsFile(); if (f) files.push(f); }
          }
        }
        return files;
      }

      function inject(files) {
        const ts = new Date().toISOString().slice(0, 19).replace(/[:T]/g, '-');
        const dt = new DataTransfer();
        files.forEach((f, i) => {
          const name = (!f.name || f.name === 'image.png')
            ? 'pasted_' + ts + (files.length > 1 ? '_' + i : '') + '.png' : f.name;
          dt.items.add(new File([f], name, {type: f.type || 'image/png'}));
        });
        const zone = doc.querySelector('[data-testid="stChatInput"]');
        const input = (zone && zone.querySelector('input[type="file"]')) ||
                      doc.querySelector('[data-testid="stBottom"] input[type="file"]') ||
                      doc.querySelector('input[type="file"]');
        if (input) {
          input.files = dt.files;
          input.dispatchEvent(new Event('change', {bubbles: true}));
          note('📎 ' + Array.from(dt.files).map(f => f.name).join(', '), '#2a9d8f');
          return true;
        }
        if (zone) {
          const ev = new Event('drop', {bubbles: true, cancelable: true});
          ev.dataTransfer = dt;
          zone.dispatchEvent(ev);
          note('📎 ' + Array.from(dt.files).map(f => f.name).join(', '), '#2a9d8f');
          return true;
        }
        note('Paste failed: chat input not found', '#d64550');
        return false;
      }

      function onPaste(e) {
        if (e.__luluSeen) return;                   // textarea + document both listen; act once
        e.__luluSeen = true;
        const files = grabFiles(e.clipboardData);
        if (!files.length) {
          // visible feedback even when there is NO file — tells us the event DID arrive
          if (!(e.clipboardData && e.clipboardData.getData('text')))
            note('clipboard has no file', '#8d6e63');
          return;                                   // plain text pastes normally
        }
        e.preventDefault();
        e.stopImmediatePropagation();               // silence any older bridge listeners
        inject(files);
      }

      // belt AND braces: document capture + direct hook on the chat textarea (re-attached
      // whenever Streamlit re-renders it)
      doc.addEventListener('paste', onPaste, true);
      function armTextarea() {
        const ta = doc.querySelector('[data-testid="stChatInputTextArea"]');
        if (ta && !ta.__luluPaste) { ta.__luluPaste = true; ta.addEventListener('paste', onPaste, true); }
      }
      armTextarea();
      new MutationObserver(armTextarea).observe(doc.body, {childList: true, subtree: true});
    })();
    </script>""", height=0)

    # ---- input (chat box with attachments; example pill fires once) ----
    raw = st.chat_input("Ask Lulu… paste a screenshot/file with Ctrl+V, or drag it in",
                        accept_file="multiple",
                        file_type=["png", "jpg", "jpeg", "webp", "csv", "xlsx", "xlsm",
                                   "pdf", "txt", "md", "json", "yaml", "log"])
    q, files = None, []
    if raw is not None:
        if hasattr(raw, "text"):                  # ChatInputValue (text + files)
            q, files = (raw.text or ""), list(raw.files or [])
        else:                                     # plain string (no attachment)
            q = str(raw)
    if not q and not files and st.session_state.get("_pending_q"):
        q = st.session_state.pop("_pending_q")

    if (q and q.strip()) or files:
        q = (q or "").strip()
        q_display = q or "Analyse this attachment"
        engine_question = q
        if files:
            with st.spinner(f"Reading {len(files)} attachment(s)…"):
                from attachment_analyzer import analyze_attachments
                attach_ctx = analyze_attachments(files, q)
            q_display += "  \n" + "  ".join(f"`📎 {f.name}`" for f in files)
            engine_question = (q or "Please analyse this attachment and give the key points") + "\n\n" + attach_ctx
        hist = st.session_state.engine_history
        # attachments need the reasoning model — auto-use the gateway when available
        use_llm = engine.startswith("LLM") or (files and len(engines) > 1)
        if use_llm:
            from llm_agent_runner import LuluGatewayAgent
            with st.spinner("Planner model reasoning…"):
                r = LuluGatewayAgent().ask(engine_question, user_role=role, user=user["email"], history=hist,
                                           conversation_id=st.session_state.conv_id)
            answer, conf, caveats, trace_id = r.final_answer, r.confidence, r.caveats, r.trace_id
            first = r.tools_called[0] if r.tools_called else {}
            tool, function, args = first.get("name", ""), first.get("name", ""), first.get("args", {})
            export_rows = next((t.get("data") for t in reversed(r.tools_called)
                                if t.get("ok") and t.get("data")), [])
            trace_steps = [("Planner", r.planner_model)] + \
                          [("Tool", f"{t['name']}({t['args']}) → {t['rows']} rows · {t['confidence']}")
                           for t in r.tools_called] + \
                          ([("Answer model", r.answer_model)] if r.answer_model else []) + \
                          ([("Tokens", str(r.tokens))] if r.tokens else [])
            sqls = []
        else:
            with st.spinner("Routing through the semantic planner…"):
                r = det_agent().ask(engine_question, user_role=role, user=user["email"], history=hist,
                                    conversation_id=st.session_state.conv_id)
            answer, conf, caveats, trace_id = r.answer, r.confidence, r.caveats, r.trace_id
            tool, function, args = r.tool, r.function, r.args
            export_rows = getattr(r, "export_rows", [])
            trace_steps = [("Domain", r.domain or "(clarification)"),
                           ("Tool", f"{r.tool}.{r.function}" if r.tool else "—"),
                           ("Args", str(r.args) if r.args else "—")]
            if r.domain == "Follow-up":
                trace_steps += [("Context", s) for s in r.plan_steps]
            if r.is_meta:
                trace_steps += [("Step", s["tool"] + "." + s["function"] + f" → {s['rows']} rows")
                                for s in r.step_results]
            if getattr(r, "memory_used", None):
                trace_steps += [("Memory", m_) for m_ in r.memory_used]
            if getattr(r, "learned", ""):
                trace_steps += [("Learned", r.learned)]
            trace_steps += [("Tables", ", ".join(r.tables) if r.tables else "—"),
                            ("Validator", "PASS" if r.validator_ok else "n/a")]
            sqls = [r.sql] if r.sql else []

        # ---- derive the operational answer-contract context (freshness / risk / actions / missing) ----
        try:
            _ctx = _oa.build_context(q, getattr(r, "domain", "") or "",
                                     list(getattr(r, "tables", []) or []))
        except Exception:
            _ctx = {}
        st.session_state.chat.append({"q": q_display, "answer": answer, "conf": conf, "caveats": caveats,
                                      "trace_steps": trace_steps, "sqls": sqls,
                                      "trace_id": trace_id, "feedback": None,
                                      "export_rows": export_rows, "ctx": _ctx})
        st.session_state.engine_history.append({"question": q_display, "answer": answer,
                                                "tool": tool, "function": function, "args": args})
        st.rerun()


# =====================================================================
# PAGE 3 — AGENT TRACE (debugging)
# =====================================================================
else:
    st.title("Agent Trace")
    st.caption("Which tools earn their keep — usage, success, follow-ups, corrections.")
    admin_panel(user)      # user management (Admin_IT only)

    log = usage_logger()
    rep = log.report(last_n=100)
    st.markdown(f"**Window:** last {rep['window']} of {rep['total_logged']} logged calls")
    if rep["tools"]:
        st.dataframe(rep["tools"], use_container_width=True, hide_index=True)
    else:
        st.info("No tool calls logged yet.")

    st.divider()
    st.subheader("Recent calls")
    recent = log.records[-25:][::-1]
    if recent:
        st.dataframe(
            [{"time": r.get("ts", "")[:19].replace("T", " "), "question": r.get("question", "")[:60],
              "tool": f"{r.get('tool')}.{r.get('function')}", "ok": r.get("ok"),
              "rows": r.get("row_count"), "confidence": r.get("confidence"),
              "role": r.get("role"), "followed_up": r.get("followed_up"),
              "corrected": r.get("corrected")} for r in recent],
            use_container_width=True, hide_index=True)

    st.divider()
    st.subheader("Business Memory — what Lulu has learned")
    try:
        from memory_manager import MemoryManager
        mm = MemoryManager()
        cm = mm.company
        mc1, mc2 = st.columns(2)
        with mc1:
            st.markdown("**Site rules**")
            rules = cm.get("site_rules", {})
            if rules:
                st.dataframe([{"site": s.upper(), "required": ", ".join(r["required_tickets"]),
                               "learned": r.get("learned")} for s, r in rules.items()],
                             use_container_width=True, hide_index=True)
            else:
                st.caption("None yet — tell Lulu e.g. 'NWM requires: VOC, WAH, Driver Licence'")
            st.markdown("**Definitions**")
            for t, d in cm.get("definitions", {}).items():
                st.caption(f"• **{t}** = {d.get('meaning')}")
        with mc2:
            st.markdown("**Supplier flags**")
            for f in cm.get("suppliers", {}).get("high_risk", []):
                st.caption(f"• {f.get('name')} — flagged {f.get('noted')} ({f.get('note','')[:50]})")
            st.markdown("**User profile (conversation memory)**")
            st.json(mm.user_profile("admin"), expanded=False)
    except Exception as ex:
        st.caption(f"memory unavailable: {ex}")

    st.divider()
    st.subheader("LLM Gateway")
    gs = gw_status()
    if gs:
        st.dataframe(
            [{"role": r, "provider": gs[r]["provider"], "model": gs[r]["model"],
              "api_key_env": gs[r]["api_key_env"],
              "available": "🟢" if gs[r]["available"] else "⚪ no key"}
             for r in ("planner", "answer", "fallback")],
            use_container_width=True, hide_index=True)
        st.caption(f"two_stage: {gs.get('two_stage')} · temperature: {gs.get('temperature')} "
                   "· switch models in admin_settings.json — no code change")
