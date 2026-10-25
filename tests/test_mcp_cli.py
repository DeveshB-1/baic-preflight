import json
import subprocess
import sys
from pathlib import Path

from baic_preflight import cli

ROOT = Path(__file__).resolve().parent.parent
FIX = ROOT / "fixtures"


def rpc(lines):
    p = subprocess.run([sys.executable, "-m", "baic_preflight.cli", "mcp"], cwd=ROOT,
                       input="\n".join(json.dumps(m) for m in lines) + "\n",
                       capture_output=True, text=True, timeout=30)
    assert p.returncode == 0, p.stderr
    return [json.loads(x) for x in p.stdout.splitlines()]


def test_mcp_session():
    out = rpc([
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                    "clientInfo": {"name": "t", "version": "0"}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {
            "name": "baic_check_secrets",
            "arguments": {"secrets_file": str(FIX / "secrets-broken.json"),
                          "images": "registry.acme.example.com/baic"}}},
        {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {
            "name": "baic_check_cluster", "arguments": {"nodes_file": str(FIX / "nodes-roomy.json")}}},
        {"jsonrpc": "2.0", "id": 5, "method": "tools/call",
         "params": {"name": "baic_install_spec", "arguments": {}}},
        {"jsonrpc": "2.0", "id": 6, "method": "tools/call", "params": {"name": "nope"}},
        {"jsonrpc": "2.0", "id": 7, "method": "tools/call",
         "params": {"name": "baic_check_secrets", "arguments": {}}},
    ])
    assert [m["id"] for m in out] == [1, 2, 3, 4, 5, 6, 7]  # notification got no reply
    assert out[0]["result"]["serverInfo"]["name"] == "baic-preflight"
    names = [t["name"] for t in out[1]["result"]["tools"]]
    assert names == ["baic_install_spec", "baic_check_secrets", "baic_check_cluster",
                     "baic_check_registry", "baic_check_postgres"]
    secrets = json.loads(out[2]["result"]["content"][0]["text"])
    assert secrets["summary"]["BLOCKER"] == 7
    assert "registry" not in {f["check"] for f in secrets["findings"]}
    cluster = json.loads(out[3]["result"]["content"][0]["text"])
    assert cluster["summary"]["BLOCKER"] == 0
    specd = json.loads(out[4]["result"]["content"][0]["text"])
    assert specd["minimums"]["postgres"] == "17.4" and len(specd["images"]) == 14
    assert out[5]["error"]["code"] == -32602
    assert out[6]["result"]["isError"] is True


def test_cli_exit_codes(capsys):
    assert cli.main(["check", "--nodes-file", str(FIX / "nodes-roomy.json")]) == 0
    assert "ready for helm install" in capsys.readouterr().out
    code = cli.main(["check", "--secrets-file", str(FIX / "secrets-broken.json"),
                     "--pull-host", "registry.acme.example.com", "--json"])
    assert code == 2
    data = json.loads(capsys.readouterr().out)
    assert data["summary"]["BLOCKER"] == 7


def test_cli_needs_something_to_check():
    import pytest
    with pytest.raises(SystemExit):
        cli.main(["check"])
