"""Patch the exported Container App YAML to mount the Azure Files share at the logs dir."""
import sys
import yaml

p = sys.argv[1]
d = yaml.safe_load(open(p, encoding="utf-8"))
t = d["properties"]["template"]

vols = t.get("volumes") or []
if not any((v or {}).get("name") == "lulu-logs" for v in vols):
    vols.append({"name": "lulu-logs", "storageType": "AzureFile", "storageName": "lulustate"})
t["volumes"] = vols

c = t["containers"][0]
mounts = c.get("volumeMounts") or []
if not any((m or {}).get("volumeName") == "lulu-logs" for m in mounts):
    mounts.append({"volumeName": "lulu-logs", "mountPath": "/app/data/agent/logs"})
c["volumeMounts"] = mounts

yaml.safe_dump(d, open(p, "w", encoding="utf-8"), sort_keys=False, allow_unicode=True)
print("volumes:", t["volumes"])
print("mounts:", c["volumeMounts"])
