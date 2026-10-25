"""The same checks as MCP tools, so an agent walking a customer through the
install guide can ask "is this cluster ready?" and get findings back.

Stdio transport, newline-delimited JSON-RPC 2.0, stdlib only. Tool results
carry Secret and key names, never values.
"""

from __future__ import annotations

import argparse
import json

from . import checks
from .guide import load_spec

PROTOCOL = "2025-06-18"

_COMMON = {"spec": {"type": "string", "description": "spec JSON or guide .md; default bundled 0.1.2"}}

TOOLS = [
    {
        "name": "baic_install_spec",
        "description": "What the BAIC 0.1.2 install guide requires: images and tags, chart versions, "
                       "Secret names and keys, cluster and database minimums.",
        "inputSchema": {"type": "object", "properties": dict(_COMMON)},
    },
    {
        "name": "baic_check_secrets",
        "description": "Check the 15 prerequisite Secrets in a namespace (live via kubectl, or a "
                       "saved `kubectl get secrets -o json` file). Returns names only, no values.",
        "inputSchema": {"type": "object", "properties": {
            **_COMMON,
            "namespace": {"type": "string"},
            "secrets_file": {"type": "string"},
            "images": {"type": "string", "description": "image prefix, used to check docker-config auth"},
        }},
    },
    {
        "name": "baic_check_cluster",
        "description": "Check Kubernetes version, node count, capacity and default StorageClass.",
        "inputSchema": {"type": "object", "properties": {
            **_COMMON,
            "live": {"type": "boolean", "description": "read from the current kubectl context"},
            "nodes_file": {"type": "string"},
            "storageclass_file": {"type": "string"},
        }},
    },
    {
        "name": "baic_check_registry",
        "description": "Check every image tag and chart version from the guide exists in your mirror.",
        "inputSchema": {"type": "object", "properties": {
            **_COMMON,
            "images": {"type": "string"},
            "charts": {"type": "string"},
            "plain_http": {"type": "boolean"},
        }},
    },
    {
        "name": "baic_check_postgres",
        "description": "Check Postgres version and that the role can CREATE DATABASE. Put the "
                       "password in PGPASSWORD rather than the DSN.",
        "inputSchema": {"type": "object", "properties": {**_COMMON, "dsn": {"type": "string"}},
                        "required": ["dsn"]},
    },
]


def _ns(spec=None, namespace=None, secrets_file=None, kubectl=False, nodes_file=None,
        storageclass_file=None, images=None, charts=None, plain_http=False, pg_dsn=None,
        pull_host=None):
    return argparse.Namespace(**locals())


def call_tool(name: str, a: dict) -> dict:
    from . import cli  # late import keeps cli <-> mcp acyclic

    spec = a.get("spec")
    if name == "baic_install_spec":
        s = load_spec(spec)
        return {"images": s.images, "charts": s.charts,
                "secrets": {n: [k.name for k in sec.keys] for n, sec in s.secrets.items()},
                "minimums": {"kubernetes": s.min_k8s, "nodes": s.min_nodes,
                             "postgres": s.min_postgres, "redis": s.min_redis,
                             "footprint": f"~{s.footprint_cpu:g} vCPU / ~{s.footprint_mem_gib:g} GiB"}}
    if name == "baic_check_secrets":
        if a.get("secrets_file"):
            data = cli._load(a["secrets_file"])
        elif a.get("namespace"):
            data = cli.kubectl_json("get", "secrets", "-n", a["namespace"])
        else:
            raise ValueError("pass namespace or secrets_file")
        host = checks._split_ref(a["images"])[0] if a.get("images") else None
        found = checks.check_secrets(data, load_spec(spec), registry_host=host)
    elif name == "baic_check_cluster":
        found = cli.run(_ns(spec, kubectl=bool(a.get("live")), nodes_file=a.get("nodes_file"),
                            storageclass_file=a.get("storageclass_file")))
    elif name == "baic_check_registry":
        found = cli.run(_ns(spec, images=a.get("images"), charts=a.get("charts"),
                            plain_http=bool(a.get("plain_http"))))
    elif name == "baic_check_postgres":
        found = cli.run(_ns(spec, pg_dsn=a.get("dsn")))
    else:
        raise KeyError(name)
    return {"summary": checks.summarize(found), "findings": [f.as_dict() for f in found]}


def handle(msg: dict) -> dict | None:
    mid, method = msg.get("id"), msg.get("method")
    if mid is None:  # notification
        return None
    if method == "initialize":
        result = {"protocolVersion": msg.get("params", {}).get("protocolVersion", PROTOCOL),
                  "capabilities": {"tools": {}},
                  "serverInfo": {"name": "baic-preflight", "version": "0.1.0"}}
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {"tools": TOOLS}
    elif method == "tools/call":
        p = msg.get("params", {})
        try:
            data = call_tool(p.get("name"), p.get("arguments") or {})
            result = {"content": [{"type": "text", "text": json.dumps(data, indent=2)}],
                      "isError": False}
        except KeyError:
            return {"jsonrpc": "2.0", "id": mid,
                    "error": {"code": -32602, "message": f"unknown tool {p.get('name')}"}}
        except (SystemExit, Exception) as e:  # report check failures to the agent, keep serving
            result = {"content": [{"type": "text", "text": f"error: {e}"}], "isError": True}
    else:
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"no method {method}"}}
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def serve(stdin, stdout) -> None:
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            reply = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}}
        else:
            reply = handle(msg)
        if reply is not None:
            stdout.write(json.dumps(reply) + "\n")
            stdout.flush()
