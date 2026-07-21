"""Azure Ops domain — the Ops/Infra agent's eyes on the Azure estate.

Unlike the 9 Gold data tools, this one does NOT query Gold and does NOT go through
the SQL security chain. Its source is the live Azure control plane via the `az` CLI
(the operator's delegated login). It is STRICTLY READ-ONLY here — restart / rerun /
scale are deliberately NOT implemented (they are Phase-B write actions that must go
through the Orchestrator's approval gate + audit log).

Answers: is the nightly refresh OK, is lulu-app up, how many systems are online,
what failed last night.  Results are cached (az is slow) with a short TTL.

Windows note: az log streaming crashes on cp1252; we force UTF-8 + no-color env.
"""

import json
import os
import shutil
import subprocess
import time

# On Windows `az` is az.cmd — resolve the real path so subprocess (no shell) can run it.
_AZ = shutil.which("az") or shutil.which("az.cmd") or "az"

RG = "lulu-rg"
REFRESH_JOB = "lulu-refresh"
APP = "lulu-app"

_AZ_ENV = dict(os.environ, AZURE_CORE_NO_COLOR="true", PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
_cache = {}          # key -> (expires_at, value)
_TTL = 60            # seconds


def _az(args, timeout=40):
    """Run an `az ... -o json` command read-only; return parsed JSON or None on any failure."""
    try:
        out = subprocess.run([_AZ] + args + ["-o", "json"], capture_output=True, text=True,
                             timeout=timeout, env=_AZ_ENV, encoding="utf-8", errors="replace")
        if out.returncode != 0 or not out.stdout.strip():
            return None
        return json.loads(out.stdout)
    except Exception:
        return None


def _cached(key, fn):
    now = time.time()
    hit = _cache.get(key)
    if hit and now < hit[0]:
        return hit[1]
    val = fn()
    _cache[key] = (now + _TTL, val)
    return val


class AzureOpsTool:
    """Read-only monitoring of the Azure estate (resources, jobs, the nightly refresh)."""

    name = "azure_ops"

    # ---------- the nightly data refresh (the thing we keep debugging) ----------
    def refresh_status(self, n=5):
        def fetch():
            execs = _az(["containerapp", "job", "execution", "list", "-n", REFRESH_JOB, "-g", RG,
                         "--query", "[].{name:name, status:properties.status, "
                                    "start:properties.startTime, end:properties.endTime}"]) or []
            execs.sort(key=lambda e: e.get("start") or "", reverse=True)
            return execs[:n]
        execs = _cached("refresh", fetch)
        if not execs:
            return {"ok": False, "summary": "lulu-refresh: 无法读取执行记录（az 未登录或 job 不存在）", "runs": []}
        latest = execs[0]
        st = latest.get("status")
        streak = 0
        for e in execs:
            if e.get("status") == "Failed":
                streak += 1
            else:
                break
        verdict = {"Succeeded": "✅ 正常", "Running": "⏳ 运行中", "Failed": "❌ 失败"}.get(st, st)
        summ = f"夜间数据刷新 lulu-refresh 最近一次: {verdict} ({latest.get('start','?')})"
        if streak >= 2 and st == "Failed":
            summ += f" · 连续失败 {streak} 次"
        return {"ok": st in ("Succeeded", "Running"), "status": st, "fail_streak": streak,
                "summary": summ, "runs": execs}

    # ---------- the Lulu app container ----------
    def app_status(self):
        def fetch():
            return _az(["containerapp", "show", "-n", APP, "-g", RG,
                        "--query", "{running:properties.runningStatus, "
                                   "fqdn:properties.configuration.ingress.fqdn}"])
        d = _cached("app", fetch) or {}
        run = d.get("running")
        return {"ok": run == "Running", "status": run or "unknown",
                "fqdn": d.get("fqdn"), "summary": f"lulu-app 容器: {run or '未知'}"}

    # ---------- the wider estate ----------
    def estate(self):
        def fetch():
            res = _az(["resource", "list",
                       "--query", "[].{name:name, type:type, rg:resourceGroup}"]) or []
            return res
        res = _cached("estate", fetch)
        if res is None:
            return {"ok": False, "summary": "无法读取 Azure 资源清单（az 未登录？）", "by_type": {}, "total": 0}
        by_type = {}
        for r in res:
            t = (r.get("type") or "").split("/")[-1] or "other"
            by_type[t] = by_type.get(t, 0) + 1
        web = sum(1 for r in res if "Microsoft.Web/sites" in (r.get("type") or ""))
        return {"ok": True, "total": len(res), "web_apps": web, "by_type": by_type,
                "summary": f"Azure 资源 {len(res)} 个（含 {web} 个 Web/Function App）"}

    # ---------- one-glance health (Orchestrator '看所有系统状态') ----------
    def health_summary(self):
        ref = self.refresh_status()
        app = self.app_status()
        est = self.estate()
        alerts = []
        if not ref["ok"]:
            alerts.append(ref["summary"])
        if not app["ok"]:
            alerts.append(app["summary"])
        lines = [
            f"• 数据刷新: {ref['summary']}",
            f"• Lulu 应用: {app['summary']}",
            f"• 资源规模: {est['summary']}",
        ]
        return {
            "ok": ref["ok"] and app["ok"] and est["ok"],
            "alerts": alerts,
            "refresh": ref, "app": app, "estate": est,
            "summary": "Azure Ops 概览\n" + "\n".join(lines) +
                       (f"\n⚠ {len(alerts)} 项需关注" if alerts else "\n全部正常"),
        }


if __name__ == "__main__":
    t = AzureOpsTool()
    print("=== health_summary ===")
    print(t.health_summary()["summary"])
    print("\n=== refresh runs ===")
    for r in t.refresh_status()["runs"]:
        print(f"  {r.get('start'):28} {r.get('status')}")
