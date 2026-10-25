"""baic-preflight: check a cluster against the BAIC install guide before `helm install`."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

from . import checks
from .checks import BLOCKER, OK, WARN, Finding
from .guide import GUIDE_URL, fetch_guide, lint, load_spec, parse_guide

MARK = {BLOCKER: "x", WARN: "!", OK: "ok"}


def kubectl_json(*args: str) -> dict:
    r = subprocess.run(["kubectl", *args, "-o", "json"], capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        raise SystemExit(f"kubectl {' '.join(args)} failed: {r.stderr.strip()}")
    return json.loads(r.stdout)


def _load(path: str) -> dict:
    return json.loads(Path(path).read_text())


def registry_factory(plain_http: bool):
    user = os.environ.get("BAIC_REGISTRY_USER")
    password = os.environ.get("BAIC_REGISTRY_PASSWORD")

    def make(host: str) -> checks.Registry:
        scheme = "http" if plain_http else "https"
        return checks.Registry(f"{scheme}://{host}", user, password)
    return make


def run(args) -> list[Finding]:
    spec = load_spec(args.spec)
    found: list[Finding] = []
    ran = False
    if args.secrets_file or args.namespace:
        data = _load(args.secrets_file) if args.secrets_file else \
            kubectl_json("get", "secrets", "-n", args.namespace)
        host = args.pull_host or (checks._split_ref(args.images)[0] if args.images else None)
        found += checks.check_secrets(data, spec, registry_host=host)
        ran = True
    if args.nodes_file or args.kubectl:
        nodes = _load(args.nodes_file) if args.nodes_file else kubectl_json("get", "nodes")
        sc = None
        if args.storageclass_file:
            sc = _load(args.storageclass_file)
        elif args.kubectl:
            sc = kubectl_json("get", "storageclass")
        server = None
        if args.kubectl:
            server = kubectl_json("version").get("serverVersion", {}).get("gitVersion")
        found += checks.check_cluster(nodes, spec, sc, server)
        ran = True
    if args.images or args.charts:
        found += checks.check_registry(spec, args.images, args.charts,
                                       registry_factory(args.plain_http))
        ran = True
    if args.pg_dsn:
        found += checks.check_postgres(args.pg_dsn, spec)
        ran = True
    if not ran:
        raise SystemExit("nothing to check: pass --namespace/--secrets-file, --kubectl/--nodes-file, "
                         "--images/--charts or --pg-dsn (see --help)")
    return found


def render(found: list[Finding], quiet_ok: bool) -> str:
    lines = []
    for check in dict.fromkeys(f.check for f in found):
        shown = sorted((f for f in found if f.check == check and not (quiet_ok and f.level == OK)),
                       key=lambda f: [BLOCKER, WARN, OK].index(f.level))
        if shown:
            lines.append(f"\n[{check}]")
        for f in shown:
            lines.append(f"  {MARK[f.level]:>2} {f.level:<7} {f.subject}: {f.message}")
    s = checks.summarize(found)
    lines.append(f"\n{s[BLOCKER]} blockers, {s[WARN]} warnings, {s[OK]} ok")
    if s[BLOCKER]:
        verdict = "NOT READY for helm install"
    else:
        verdict = "ready for helm install" + (f" (review {s[WARN]} warnings first)" if s[WARN] else "")
    lines.append("verdict: " + verdict)
    return "\n".join(lines)


def cmd_spec(args) -> int:
    if args.guide:
        src = args.guide
        text = Path(src).read_text() if Path(src).exists() else fetch_guide(src)
    else:
        src, text = GUIDE_URL, fetch_guide()
    spec = parse_guide(text, source=src)
    if args.write:
        Path(args.write).write_text(spec.to_json() + "\n")
    issues = lint(spec)
    if args.json:
        print(json.dumps({"source": src, "images": len(spec.images), "charts": len(spec.charts),
                          "secrets": len(spec.secrets), "lint": issues}, indent=2))
    else:
        print(f"guide: {src}")
        print(f"  {len(spec.images)} images, {len(spec.charts)} charts, {len(spec.secrets)} secrets "
              f"({sum(len(s.keys) for s in spec.secrets.values())} keys)")
        print(f"  k8s >= {spec.min_k8s}, {spec.min_nodes}+ nodes, postgres >= {spec.min_postgres}, "
              f"redis >= {spec.min_redis}, footprint ~{spec.footprint_cpu:g} vCPU / "
              f"~{spec.footprint_mem_gib:g} GiB")
        print("  lint: " + ("guide is self-consistent" if not issues else "; ".join(issues)))
    return 1 if issues else 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="baic-preflight", description=__doc__)
    sub = p.add_subparsers(dest="cmd")

    sp = sub.add_parser("spec", help="parse the install guide into a spec and lint it")
    sp.add_argument("--guide", help="guide path or URL (default: pinned baic-docs commit)")
    sp.add_argument("--write", help="write the parsed spec JSON here")
    sp.add_argument("--json", action="store_true")

    cp = sub.add_parser("check", help="run preflight checks")
    cp.add_argument("--spec", help="spec JSON or guide .md (default: bundled 0.1.2 spec)")
    cp.add_argument("--namespace", help="read Secrets live with kubectl from this namespace")
    cp.add_argument("--secrets-file", help="`kubectl get secrets -o json` output")
    cp.add_argument("--pull-host", help="registry host the cluster pulls from (default: host of --images)")
    cp.add_argument("--kubectl", action="store_true", help="read nodes/storageclasses live")
    cp.add_argument("--nodes-file", help="`kubectl get nodes -o json` output")
    cp.add_argument("--storageclass-file", help="`kubectl get storageclass -o json` output")
    cp.add_argument("--images", help="your image prefix, e.g. registry.example.com/baic")
    cp.add_argument("--charts", help="your chart prefix, e.g. oci://registry.example.com/baic-charts")
    cp.add_argument("--plain-http", action="store_true", help="registry speaks http (local mirror)")
    cp.add_argument("--pg-dsn", help="postgres DSN for the role BAIC will use")
    cp.add_argument("--json", action="store_true")
    cp.add_argument("--quiet-ok", action="store_true", help="hide passing checks")

    sub.add_parser("mcp", help="serve the checks as MCP tools over stdio")

    args = p.parse_args(argv)
    if args.cmd == "spec":
        return cmd_spec(args)
    if args.cmd == "check":
        found = run(args)
        if args.json:
            print(json.dumps({"summary": checks.summarize(found),
                              "findings": [f.as_dict() for f in found]}, indent=2))
        else:
            print(render(found, args.quiet_ok).lstrip("\n"))
        return 2 if any(f.level == BLOCKER for f in found) else 0
    if args.cmd == "mcp":
        from .mcp import serve
        serve(sys.stdin, sys.stdout)
        return 0
    p.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
