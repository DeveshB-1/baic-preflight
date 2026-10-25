"""Turn the public BAIC install guide into a machine-checkable spec.

The guide (github.com/uniphore/baic-docs, index.md) already carries every fact a
preflight needs: the image and chart lists in the mirror loops, the Secret names
and keys in the `kubectl create secret` commands, and the minimums in the
prerequisite tables. This module reads them out so the checks never drift from
the doc a customer is following.
"""

from __future__ import annotations

import json
import re
import urllib.request
from dataclasses import asdict, dataclass, field
from pathlib import Path

GUIDE_REPO = "uniphore/baic-docs"
GUIDE_SHA = "827cf4da3c665a97ba1414403355df1db5b6f437"  # 2026-07-01, uniphore-baic 0.1.2
GUIDE_URL = f"https://raw.githubusercontent.com/{GUIDE_REPO}/{GUIDE_SHA}/index.md"

# Value kinds for a Secret key, read from the placeholder the guide uses.
REQUIRED = "required"      # '<YOUR-POSTGRES-PASSWORD>' style: must be non-empty
OPTIONAL = "optional"      # '' or '<...-OR-EMPTY>': may be empty, key must exist
FIXED = "fixed"            # 'xforge', '5432': the guide's literal value


@dataclass
class SecretKey:
    name: str
    kind: str
    value: str = ""  # literal for FIXED keys, placeholder text otherwise


@dataclass
class SecretSpec:
    name: str
    type: str  # "generic" | "docker-registry"
    section: str  # heading the command sits under
    keys: list[SecretKey] = field(default_factory=list)

    def key(self, name: str) -> SecretKey | None:
        return next((k for k in self.keys if k.name == name), None)


@dataclass
class Spec:
    source: str
    images: dict[str, str]  # name -> tag
    charts: dict[str, str]  # name -> version
    secrets: dict[str, SecretSpec]
    verify_list: list[str]  # names in the "Verify all N Secrets" command
    min_k8s: str
    min_nodes: int
    min_node_cpu: float
    min_node_mem_gib: float
    min_total_cpu: float
    min_total_mem_gib: float
    footprint_cpu: float
    footprint_mem_gib: float
    min_postgres: str
    min_redis: str
    stated: dict[str, int]  # counts the prose claims ("14 container images")

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2)

    @classmethod
    def from_json(cls, text: str) -> "Spec":
        d = json.loads(text)
        d["secrets"] = {
            n: SecretSpec(
                name=s["name"], type=s["type"], section=s["section"],
                keys=[SecretKey(**k) for k in s["keys"]],
            )
            for n, s in d["secrets"].items()
        }
        return cls(**d)


def _bash_array(text: str, var: str) -> dict[str, str]:
    m = re.search(rf"{var}=\(\s*(.*?)\)", text, re.S)
    if not m:
        raise ValueError(f"guide has no {var}=( ... ) array")
    out: dict[str, str] = {}
    for item in re.findall(r'"([^"]+)"', m.group(1)):
        name, _, tag = item.partition(":")
        out[name] = tag
    return out


def _shell_commands(text: str):
    """Yield (heading, command) for each shell command in ```sh blocks,
    with backslash line continuations joined."""
    heading = ""
    in_sh = False
    buf = ""
    for raw in text.splitlines():
        line = raw.strip()
        if not in_sh and line.startswith("#") and not line.startswith("#!"):
            heading = line.lstrip("#").strip()
            continue
        if line.startswith("```"):
            in_sh = (not in_sh) and line[3:].strip() in ("sh", "bash", "shell")
            buf = ""
            continue
        if not in_sh:
            continue
        if line.endswith("\\"):
            buf += line[:-1] + " "
            continue
        cmd = (buf + line).strip()
        buf = ""
        if cmd:
            yield heading, cmd


def _key_kind(value: str) -> str:
    if value == "" or value.endswith("-OR-EMPTY>"):
        return OPTIONAL
    if "<" in value and ">" in value:
        return REQUIRED
    return FIXED


def _secrets(text: str) -> dict[str, SecretSpec]:
    secrets: dict[str, SecretSpec] = {}
    pat = re.compile(r"create secret (generic|docker-registry)\s+([a-z0-9-]+)(.*)")
    for heading, cmd in _shell_commands(text):
        m = pat.search(cmd)
        if not m:
            continue
        stype, name, rest = m.groups()
        spec = SecretSpec(name=name, type=stype, section=heading)
        for src, key, val in re.findall(
            r"--from-(literal|file)=([A-Za-z0-9_.]+)=('[^']*'|\S*)", rest
        ):
            val = val.strip("'")
            kind = REQUIRED if src == "file" else _key_kind(val)
            spec.keys.append(SecretKey(key, kind, "" if src == "file" else val))
        if stype == "docker-registry":
            spec.keys = [SecretKey(".dockerconfigjson", REQUIRED, "")]
        secrets[name] = spec
    return secrets


def _verify_list(text: str) -> list[str]:
    for _, cmd in _shell_commands(text):
        if re.search(r"get secret\s", cmd) and "-o" not in cmd.split("get secret")[1][:4]:
            names = cmd.split("get secret", 1)[1].split()
            return [n for n in names if re.fullmatch(r"[a-z0-9-]+", n)]
    return []


def _num(pattern: str, text: str, cast=float):
    m = re.search(pattern, text)
    if not m:
        raise ValueError(f"guide is missing {pattern!r}")
    return cast(m.group(1))


def parse_guide(text: str, source: str = "local") -> Spec:
    images = _bash_array(text, "IMAGES")
    charts = _bash_array(text, "CHARTS")
    stated = {}
    for label, pat in {
        "images": r"(\d+) container images",
        "charts": r"(\d+) Helm charts",
        "secrets": r"There are \*\*(\d+) Secrets\*\*",
        "services": r"bundles (\d+) BAIC microservices",
    }.items():
        m = re.search(pat, text)
        if m:
            stated[label] = int(m.group(1))
    return Spec(
        source=source,
        images=images,
        charts=charts,
        secrets=_secrets(text),
        verify_list=_verify_list(text),
        min_k8s=_num(r"Kubernetes version \| ([\d.]+)", text, str),
        min_nodes=_num(r"Worker node count \| (\d+)", text, int),
        min_node_cpu=_num(r"Per-node capacity \| ([\d.]+) vCPU", text),
        min_node_mem_gib=_num(r"Per-node capacity \| [\d.]+ vCPU / ([\d.]+) GiB", text),
        min_total_cpu=_num(r"Aggregate capacity \| ([\d.]+) vCPU", text),
        min_total_mem_gib=_num(r"Aggregate capacity \| [\d.]+ vCPU / ([\d.]+) GiB", text),
        footprint_cpu=_num(r"footprint: \*\*~([\d.]+) vCPU", text),
        footprint_mem_gib=_num(r"footprint: \*\*~[\d.]+ vCPU, ~([\d.]+) GiB", text),
        min_postgres=_num(r"PostgreSQL \| \*\*([\d.]+)\*\*", text, str),
        min_redis=_num(r"Redis[^|]*\| \*\*([\d.]+)\*\*", text, str),
        stated=stated,
    )


def lint(spec: Spec) -> list[str]:
    """Places where the guide disagrees with itself."""
    issues = []
    counts = {"images": len(spec.images), "charts": len(spec.charts),
              "secrets": len(spec.secrets)}
    for label, n in counts.items():
        if label in spec.stated and spec.stated[label] != n:
            issues.append(f"prose says {spec.stated[label]} {label}, commands define {n}")
    created, verified = set(spec.secrets), set(spec.verify_list)
    for n in sorted(created - verified):
        issues.append(f"secret {n} is created but missing from the verify command")
    for n in sorted(verified - created):
        issues.append(f"secret {n} is in the verify command but never created")
    for name in spec.charts:
        if name not in spec.images and name not in ("uniphore-baic", "xforge-db"):
            issues.append(f"chart {name} has no image of the same name")
    return issues


def fetch_guide(url: str = GUIDE_URL, cache: Path | None = None) -> str:
    if cache and cache.exists():
        return cache.read_text()
    with urllib.request.urlopen(url, timeout=20) as r:
        text = r.read().decode()
    if cache:
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(text)
    return text


BUNDLED_SPEC = Path(__file__).resolve().parent.parent / "spec" / "baic-0.1.2.json"


def load_spec(path: str | None = None) -> Spec:
    p = Path(path) if path else BUNDLED_SPEC
    if p.suffix == ".md":
        return parse_guide(p.read_text(), source=str(p))
    return Spec.from_json(p.read_text())
