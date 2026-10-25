"""Write synthetic kubectl JSON fixtures shaped like `kubectl get ... -o json`.

Values are fake. The "broken" namespace carries mistakes the install guide warns
about, one per check, so the demo and tests exercise each rule.
"""

import base64
import copy
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from baic_preflight.guide import FIXED, load_spec  # noqa: E402

OUT = Path(__file__).resolve().parent.parent / "fixtures"
REG = "registry.acme.example.com"

PEM_PRIV = "-----BEGIN PRIVATE KEY-----\nMIIEfakefakefake\n-----END PRIVATE KEY-----\n"
PEM_PUB = "-----BEGIN PUBLIC KEY-----\nMIIBfakefakefake\n-----END PUBLIC KEY-----\n"

GOOD_VALUES = {
    "xforge-db": {"host": "pg.acme.internal", "username": "baic", "password": "s3cret"},
    "tag-db": {"host": "pg.acme.internal", "username": "baic", "password": "s3cret",
               "jdbc_url": "jdbc:postgresql://pg.acme.internal:5432/forge-tag"},
    "xforge-redis": {"host": "redis.acme.internal", "endpoint": "redis.acme.internal:6380",
                     "auth_token": "tok"},
    "forge-user-management": {"APP_CLIENT_ID": "cid", "APP_CLIENT_SECRET": "csec",
                              "APP_JWT_PRIVATE_KEY": PEM_PRIV, "APP_JWT_PUBLIC_KEY": PEM_PUB},
    "miscellaneous": {"MASTER_KEY_TOKEN_MGMT": "mk", "ld_sdk_key": "sdk-123"},
    "launchdarkly-uniphore": {"sdkKey": "sdk-123"},
}


def b64(s: str) -> str:
    return base64.b64encode(s.encode()).decode()


def secret(name, data, stype="Opaque"):
    return {"apiVersion": "v1", "kind": "Secret", "type": stype,
            "metadata": {"name": name, "namespace": "baic"},
            "data": {k: b64(v) for k, v in data.items()}}


def good_namespace(spec):
    items = []
    for name, s in spec.secrets.items():
        if s.type == "docker-registry":
            cfg = {"auths": {REG: {"auth": b64("robot:pw")}}}
            items.append(secret(name, {".dockerconfigjson": json.dumps(cfg)},
                                "kubernetes.io/dockerconfigjson"))
            continue
        vals = {k.name: (k.value if k.kind == FIXED else "") for k in s.keys}
        vals.update(GOOD_VALUES.get(name, {}))
        items.append(secret(name, vals))
    return {"apiVersion": "v1", "kind": "List", "items": items}


def broken_namespace(spec):
    ns = good_namespace(spec)
    by = {s["metadata"]["name"]: s for s in ns["items"]}
    # 1. pull secret created with Uniphore's Harbor robot instead of the mirror's creds
    by["docker-config"]["data"][".dockerconfigjson"] = b64(json.dumps(
        {"auths": {"registry.uniphore.com": {"auth": b64("robot$customer-acme+puller:tok")}}}))
    # 2. last key of the 30-key miscellaneous command dropped
    del by["miscellaneous"]["data"]["agentic_search_bucket"]
    # 3. no LaunchDarkly access, so both SDK keys left empty
    by["miscellaneous"]["data"]["ld_sdk_key"] = ""
    by["launchdarkly-uniphore"]["data"]["sdkKey"] = ""
    # 4. tag-db host edited, jdbc_url copy-pasted from the guide's example host
    by["tag-db"]["data"]["jdbc_url"] = b64("jdbc:postgresql://pg-old.acme.internal:5432/forge-tag")
    # 5. one of the 7 one-line provider Secrets skipped
    ns["items"] = [s for s in ns["items"] if s["metadata"]["name"] != "rackspace-secret"]
    # 6. JWT public key file was never filled in
    by["forge-user-management"]["data"]["APP_JWT_PUBLIC_KEY"] = b64(
        "-----BEGIN PUBLIC KEY-----\n<UNIPHORE-ISSUED-PUBLIC-KEY-CONTENT>\n-----END PUBLIC KEY-----\n")
    return ns


def node(name, cpu, mem_gib, alloc_cpu, alloc_mem_gib, version="v1.29.6-eks-1552ad0", control=False):
    n = {"metadata": {"name": name, "labels": {}},
         "spec": {},
         "status": {"capacity": {"cpu": str(cpu), "memory": f"{int(mem_gib * 2**20)}Ki"},
                    "allocatable": {"cpu": alloc_cpu, "memory": f"{int(alloc_mem_gib * 2**20)}Ki"},
                    "nodeInfo": {"kubeletVersion": version}}}
    if control:
        n["spec"]["taints"] = [{"key": "node-role.kubernetes.io/control-plane", "effect": "NoSchedule"}]
    return n


def main():
    spec = load_spec()
    OUT.mkdir(exist_ok=True)
    (OUT / "secrets-good.json").write_text(json.dumps(good_namespace(spec), indent=1))
    (OUT / "secrets-broken.json").write_text(json.dumps(broken_namespace(spec), indent=1))
    # Exactly the guide's minimum: 3 x 4 vCPU / 8 GiB. Allocatable figures are what
    # EKS reports for a 4 vCPU / 8 GiB node (c5.xlarge): 3920m CPU, ~6.9 GiB memory.
    minimum = {"items": [node(f"ip-10-0-1-{i}", 4, 8, "3920m", 6.9) for i in range(3)]}
    (OUT / "nodes-guide-minimum.json").write_text(json.dumps(minimum, indent=1))
    roomy = {"items": [node("cp-0", 2, 4, "1900m", 3.3, "v1.30.2", control=True)]
             + [node(f"worker-{i}", 8, 32, "7910m", 29.4, "v1.30.2") for i in range(3)]}
    (OUT / "nodes-roomy.json").write_text(json.dumps(roomy, indent=1))
    (OUT / "storageclass-none.json").write_text(json.dumps(
        {"items": [{"metadata": {"name": "gp2", "annotations": {}}}]}, indent=1))
    (OUT / "storageclass-default.json").write_text(json.dumps(
        {"items": [{"metadata": {"name": "gp3", "annotations": {
            "storageclass.kubernetes.io/is-default-class": "true"}}}]}, indent=1))
    print("wrote", ", ".join(sorted(p.name for p in OUT.glob("*.json"))))


if __name__ == "__main__":
    main()
