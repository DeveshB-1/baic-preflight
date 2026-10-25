"""Preflight checks. Each check takes plain data (kubectl JSON, an HTTP
fetcher, a psql runner) so it runs the same against a live cluster or a fixture.

Findings never contain Secret values, only Secret and key names, so the output
is safe to paste into a ticket or hand to an LLM agent.
"""

from __future__ import annotations

import base64
import binascii
import json
import re
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass

from .guide import FIXED, REQUIRED, Spec

BLOCKER, WARN, OK = "BLOCKER", "WARN", "OK"


@dataclass
class Finding:
    level: str
    check: str
    subject: str
    message: str

    def as_dict(self) -> dict:
        return asdict(self)


def _ver(s: str) -> tuple[int, ...]:
    return tuple(int(x) for x in re.findall(r"\d+", s)[:3])


# ---------------------------------------------------------------- secrets

LD_KEYS = (("launchdarkly-uniphore", "sdkKey"), ("miscellaneous", "ld_sdk_key"))
LD_SERVICES = "pegasus-inference-api, guardrails-service, forge-question-answer"


def _decode(data: dict, key: str) -> str | None:
    if key not in data:
        return None
    try:
        return base64.b64decode(data[key] or "").decode("utf-8", "replace")
    except (binascii.Error, ValueError):
        return ""


def check_secrets(secret_list: dict, spec: Spec, registry_host: str | None = None) -> list[Finding]:
    """secret_list is `kubectl get secrets -n <ns> -o json`."""
    out: list[Finding] = []
    have = {s["metadata"]["name"]: s for s in secret_list.get("items", [])}
    values: dict[tuple[str, str], str | None] = {}

    for name, sspec in spec.secrets.items():
        sec = have.get(name)
        if sec is None:
            out.append(Finding(BLOCKER, "secrets", name,
                               "missing; pods that mount it stay in CreateContainerConfigError "
                               "(the chart references every Secret, even optional ones)"))
            continue
        data = sec.get("data") or {}
        if sspec.type == "docker-registry":
            out.extend(_check_docker_config(sec, registry_host))
            continue
        missing = [k.name for k in sspec.keys if k.name not in data]
        if missing:
            out.append(Finding(BLOCKER, "secrets", name, "missing keys: " + ", ".join(missing)))
        for k in sspec.keys:
            v = _decode(data, k.name)
            values[(name, k.name)] = v
            if v is None:
                continue
            if k.kind == REQUIRED and not v.strip():
                out.append(Finding(BLOCKER, "secrets", f"{name}.{k.name}",
                                   "is empty; the guide requires a value"))
            elif k.kind == REQUIRED and re.fullmatch(r"<[A-Z0-9-]+>", v.strip()):
                out.append(Finding(BLOCKER, "secrets", f"{name}.{k.name}",
                                   "still holds the guide's placeholder text"))
            elif k.kind == FIXED and v != k.value:
                out.append(Finding(WARN, "secrets", f"{name}.{k.name}",
                                   f"differs from the guide's value {k.value!r}"))
        if not missing:
            out.append(Finding(OK, "secrets", name, f"{len(sspec.keys)} keys present"))

    out.extend(_cross_checks(values))
    return out


def _check_docker_config(sec: dict, registry_host: str | None) -> list[Finding]:
    if sec.get("type") != "kubernetes.io/dockerconfigjson":
        return [Finding(BLOCKER, "secrets", "docker-config",
                        f"type is {sec.get('type')!r}, expected kubernetes.io/dockerconfigjson")]
    raw = _decode(sec.get("data") or {}, ".dockerconfigjson") or "{}"
    try:
        hosts = sorted(json.loads(raw).get("auths", {}))
    except json.JSONDecodeError:
        return [Finding(BLOCKER, "secrets", "docker-config", ".dockerconfigjson is not valid JSON")]
    out = []
    if "registry.uniphore.com" in hosts:
        out.append(Finding(BLOCKER, "secrets", "docker-config",
                           "holds credentials for registry.uniphore.com; the cluster pulls from "
                           "your mirror, so this Secret needs your registry's credentials"))
    if registry_host and registry_host not in hosts:
        out.append(Finding(BLOCKER, "secrets", "docker-config",
                           f"has no auth for {registry_host} (has: {', '.join(hosts) or 'none'}); "
                           "expect ImagePullBackOff on every pod"))
    if not out:
        out.append(Finding(OK, "secrets", "docker-config", "auths: " + ", ".join(hosts)))
    return out


def _cross_checks(v: dict) -> list[Finding]:
    out = []
    ld = {f"{s}.{k}": (v.get((s, k)) or "").strip() for s, k in LD_KEYS}
    if all(x == "" for x in ld.values()):
        out.append(Finding(BLOCKER, "secrets", "launchdarkly",
                           f"SDK key is empty in both Secrets; the guide says {LD_SERVICES} "
                           "fail to start today with an empty key"))
    elif any(x == "" for x in ld.values()):
        empty = [k for k, x in ld.items() if x == ""]
        out.append(Finding(WARN, "secrets", "launchdarkly",
                           f"SDK key set in one Secret but empty in {', '.join(empty)}"))
    else:
        out.append(Finding(WARN, "secrets", "launchdarkly",
                           "SDK key is set, so stream.launchdarkly.com must be reachable from pods; "
                           "a HEAD request is not enough, run the guide's ld-probe pod"))

    host, port = v.get(("tag-db", "host")), v.get(("tag-db", "port"))
    jdbc = v.get(("tag-db", "jdbc_url"))
    if host and jdbc:
        m = re.match(r"jdbc:postgresql://([^:/]+)(?::(\d+))?/(.+)", jdbc)
        if not m:
            out.append(Finding(BLOCKER, "secrets", "tag-db.jdbc_url", "is not a jdbc:postgresql:// URL"))
        else:
            jh, jp, jdb = m.group(1), m.group(2) or "5432", m.group(3)
            if jh != host or (port and jp != port) or jdb != (v.get(("tag-db", "database")) or jdb):
                out.append(Finding(BLOCKER, "secrets", "tag-db.jdbc_url",
                                   f"points at {jh}:{jp}/{jdb} but host/port/database say "
                                   f"{host}:{port}/{v.get(('tag-db', 'database'))}"))
    xhost = v.get(("xforge-db", "host"))
    if xhost and host and xhost != host:
        out.append(Finding(WARN, "secrets", "tag-db.host",
                           f"{host!r} differs from xforge-db.host {xhost!r}; the bundled "
                           "Liquibase job creates both databases on one server"))
    rhost, rend = v.get(("xforge-redis", "host")), v.get(("xforge-redis", "endpoint"))
    if rhost and rend and rend.rsplit(":", 1)[0] != rhost:
        out.append(Finding(WARN, "secrets", "xforge-redis.endpoint",
                           f"host part {rend.rsplit(':', 1)[0]!r} differs from host {rhost!r}"))
    for key, marker in (("APP_JWT_PRIVATE_KEY", "PRIVATE KEY-----"),
                        ("APP_JWT_PUBLIC_KEY", "PUBLIC KEY-----")):
        pem = v.get(("forge-user-management", key))
        if pem and (marker not in pem or "UNIPHORE-ISSUED" in pem):
            out.append(Finding(BLOCKER, "secrets", f"forge-user-management.{key}",
                               "is not a PEM key (or is still the guide's template)"))
    return out


# ---------------------------------------------------------------- cluster

_UNITS = {"Ki": 2**10, "Mi": 2**20, "Gi": 2**30, "Ti": 2**40,
          "K": 1e3, "M": 1e6, "G": 1e9, "T": 1e12, "": 1}


def parse_cpu(q: str) -> float:
    return float(q[:-1]) / 1000 if q.endswith("m") else float(q)


def parse_mem_gib(q: str) -> float:
    m = re.fullmatch(r"([\d.]+)([KMGT]i?)?", q)
    if not m:
        raise ValueError(f"bad quantity {q!r}")
    return float(m.group(1)) * _UNITS[m.group(2) or ""] / 2**30


def _schedulable(node: dict) -> bool:
    if node.get("spec", {}).get("unschedulable"):
        return False
    return not any(t.get("effect") == "NoSchedule" for t in node.get("spec", {}).get("taints", []))


def check_cluster(nodes: dict, spec: Spec, storage_classes: dict | None = None,
                  server_version: str | None = None) -> list[Finding]:
    out: list[Finding] = []
    items = nodes.get("items", [])
    workers = [n for n in items if _schedulable(n)]

    ver = server_version or min((n["status"]["nodeInfo"]["kubeletVersion"] for n in items),
                                key=_ver, default="v0")
    if _ver(ver) < _ver(spec.min_k8s):
        out.append(Finding(BLOCKER, "cluster", "kubernetes", f"{ver} is below the guide's minimum {spec.min_k8s}"))
    else:
        out.append(Finding(OK, "cluster", "kubernetes", f"{ver} >= {spec.min_k8s}"))

    level = OK if len(workers) >= spec.min_nodes else WARN
    out.append(Finding(level, "cluster", "nodes",
                       f"{len(workers)} schedulable worker nodes (guide minimum {spec.min_nodes})"))

    for n in workers:
        cap = n["status"]["capacity"]
        cpu, mem = parse_cpu(cap["cpu"]), parse_mem_gib(cap["memory"])
        if cpu < spec.min_node_cpu or mem < spec.min_node_mem_gib * 0.95:
            out.append(Finding(WARN, "cluster", n["metadata"]["name"],
                               f"{cpu:g} vCPU / {mem:.1f} GiB is under the per-node minimum "
                               f"{spec.min_node_cpu:g} vCPU / {spec.min_node_mem_gib:g} GiB"))

    alloc_cpu = sum(parse_cpu(n["status"]["allocatable"]["cpu"]) for n in workers)
    alloc_mem = sum(parse_mem_gib(n["status"]["allocatable"]["memory"]) for n in workers)
    summary = f"{alloc_cpu:.1f} vCPU / {alloc_mem:.1f} GiB allocatable"
    short = min(alloc_cpu / spec.footprint_cpu, alloc_mem / spec.footprint_mem_gib)
    if short < 1:
        # The guide gives the footprint as "~", so a small shortfall is a warning.
        out.append(Finding(BLOCKER if short < 0.9 else WARN, "cluster", "capacity",
                           f"{summary}, under BAIC's default footprint of ~{spec.footprint_cpu:g} vCPU "
                           f"/ ~{spec.footprint_mem_gib:g} GiB; expect some pods to stay Pending"))
    elif alloc_cpu < spec.min_total_cpu or alloc_mem < spec.min_total_mem_gib:
        out.append(Finding(WARN, "cluster", "capacity",
                           f"{summary}; fits BAIC's requests but is under the guide's aggregate "
                           f"minimum {spec.min_total_cpu:g} vCPU / {spec.min_total_mem_gib:g} GiB "
                           "(no room for system pods or restarts)"))
    else:
        out.append(Finding(OK, "cluster", "capacity", summary))

    if storage_classes is not None:
        default = [s["metadata"]["name"] for s in storage_classes.get("items", [])
                   if (s["metadata"].get("annotations") or {}).get(
                       "storageclass.kubernetes.io/is-default-class") == "true"]
        if default:
            out.append(Finding(OK, "cluster", "storageclass", f"default: {default[0]}"))
        else:
            out.append(Finding(WARN, "cluster", "storageclass",
                               "no default StorageClass; only matters if you enable the "
                               "in-chart Postgres/Redis subcharts"))
    return out


# ---------------------------------------------------------------- registry

ACCEPT = ", ".join([
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.docker.distribution.manifest.v2+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
])


class Registry:
    """Minimal OCI distribution client: HEAD a manifest, with Basic auth and the
    Bearer token flow Harbor, GHCR and Docker Hub use."""

    def __init__(self, base_url: str, username: str | None = None, password: str | None = None,
                 opener=urllib.request.urlopen):
        self.base = base_url.rstrip("/")
        self.user, self.password = username, password
        self.open = opener
        self.tokens: dict[str, str] = {}

    def _basic(self) -> str | None:
        if self.user is None:
            return None
        raw = f"{self.user}:{self.password or ''}".encode()
        return "Basic " + base64.b64encode(raw).decode()

    def _request(self, url: str, auth: str | None, method: str = "HEAD"):
        req = urllib.request.Request(url, method=method, headers={"Accept": ACCEPT})
        if auth:
            req.add_header("Authorization", auth)
        try:
            with self.open(req, timeout=15) as r:
                return r.status, r.headers
        except urllib.error.HTTPError as e:
            return e.code, e.headers  # case-insensitive lookup; Docker Hub sends lowercase names

    def _token(self, challenge: str, repo: str) -> str | None:
        params = dict(re.findall(r'(\w+)="([^"]*)"', challenge))
        if "realm" not in params:
            return None
        q = {"scope": f"repository:{repo}:pull"}
        if "service" in params:
            q["service"] = params["service"]
        url = params["realm"] + "?" + urllib.parse.urlencode(q)
        req = urllib.request.Request(url)
        if self._basic():
            req.add_header("Authorization", self._basic())
        try:
            with self.open(req, timeout=15) as r:
                body = json.loads(r.read() or b"{}")
        except urllib.error.HTTPError:
            return None
        return body.get("token") or body.get("access_token")

    def manifest_status(self, repo: str, tag: str) -> int:
        url = f"{self.base}/v2/{repo}/manifests/{tag}"
        auth = self.tokens.get(repo) or self._basic()
        status, headers = self._request(url, auth)
        challenge = headers.get("WWW-Authenticate") or ""
        if status == 401 and challenge.lower().startswith("bearer"):
            tok = self._token(challenge, repo)
            if tok:
                self.tokens[repo] = "Bearer " + tok
                status, _ = self._request(url, self.tokens[repo])
        return status


def _split_ref(ref: str) -> tuple[str, str]:
    """'oci://host:5000/a/b' or 'host/a' -> ('https://host:5000', 'a/b')."""
    ref = re.sub(r"^(oci|https?)://", "", ref).rstrip("/")
    host, _, path = ref.partition("/")
    return host, path


def check_registry(spec: Spec, images_ref: str | None, charts_ref: str | None,
                   registry_factory, ) -> list[Finding]:
    """images_ref like 'registry.example.com/baic', charts_ref like
    'oci://registry.example.com/baic-charts'. registry_factory(host) -> Registry."""
    out: list[Finding] = []
    jobs = []
    if images_ref:
        jobs += [("image", images_ref, n, t) for n, t in spec.images.items()]
    if charts_ref:
        # Helm stores a chart version as an OCI tag, with '+' replaced by '_'.
        jobs += [("chart", charts_ref, n, v.replace("+", "_")) for n, v in spec.charts.items()]
    clients: dict[str, Registry] = {}
    counts = {"image": [0, 0], "chart": [0, 0]}
    for kind, ref, name, tag in jobs:
        host, path = _split_ref(ref)
        reg = clients.setdefault(host, registry_factory(host))
        repo = f"{path}/{name}" if path else name
        try:
            status = reg.manifest_status(repo, tag)
        except (urllib.error.URLError, OSError) as e:
            out.append(Finding(BLOCKER, "registry", f"{host}", f"unreachable: {e}"))
            return out
        counts[kind][1] += 1
        if status == 200:
            counts[kind][0] += 1
        elif status in (401, 403):
            out.append(Finding(BLOCKER, "registry", f"{kind} {repo}:{tag}",
                               f"HTTP {status}: credentials can't pull this repository"))
        else:
            out.append(Finding(BLOCKER, "registry", f"{kind} {repo}:{tag}",
                               f"HTTP {status}: not in your mirror (the values overlay "
                               "references tags exactly, so the pod will hit ImagePullBackOff)"
                               if kind == "image" else
                               f"HTTP {status}: chart version not in your mirror; helm install fails"))
    for kind, (ok, total) in counts.items():
        if total:
            out.append(Finding(OK if ok == total else BLOCKER, "registry", f"{kind}s",
                               f"{ok}/{total} present with the exact tags from the guide"))
    return out


# ---------------------------------------------------------------- postgres

PG_SQL = ("select current_setting('server_version'), rolcreatedb, rolsuper "
          "from pg_roles where rolname = current_user")


def run_psql(dsn: str, sql: str, psql: str = "psql") -> str:
    r = subprocess.run([*psql.split(), dsn, "-XAtqc", sql], capture_output=True, text=True, timeout=20)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip().splitlines()[-1] if r.stderr.strip() else "psql failed")
    return r.stdout.strip()


def check_postgres(dsn: str, spec: Spec, runner=run_psql) -> list[Finding]:
    try:
        row = runner(dsn, PG_SQL)
    except (RuntimeError, OSError, subprocess.TimeoutExpired) as e:
        return [Finding(BLOCKER, "postgres", "connect", f"cannot connect: {e}")]
    version, createdb, superuser = row.split("|")
    out = []
    if _ver(version) < _ver(spec.min_postgres):
        out.append(Finding(BLOCKER, "postgres", "version",
                           f"server is {version.split()[0]}, the guide requires {spec.min_postgres}+"))
    else:
        out.append(Finding(OK, "postgres", "version", f"{version.split()[0]} >= {spec.min_postgres}"))
    if createdb == "t" or superuser == "t":
        out.append(Finding(OK, "postgres", "role", "can create databases (needed by the xforge-db Liquibase job)"))
    else:
        out.append(Finding(BLOCKER, "postgres", "role",
                           "role lacks CREATEDB; the bundled Liquibase job can't create the "
                           "xforge and forge-tag databases (ALTER ROLE ... CREATEDB, or set "
                           "xforge-db.enabled: false and pre-migrate)"))
    return out


def summarize(findings: list[Finding]) -> dict:
    return {lvl: sum(f.level == lvl for f in findings) for lvl in (BLOCKER, WARN, OK)}
