import pytest

from baic_preflight.guide import (FIXED, GUIDE_URL, OPTIONAL, REQUIRED, Spec, fetch_guide,
                                  lint, load_spec, parse_guide)

MINI = """
The `uniphore-baic` chart bundles 2 BAIC microservices.
Default resource footprint: **~2 vCPU, ~3 GiB memory**.

| Kubernetes version | 1.28 |
| Worker node count | 2 |
| Per-node capacity | 2 vCPU / 4 GiB RAM |
| Aggregate capacity | 4 vCPU / 8 GiB RAM |
| PostgreSQL | **17.4** | a |
| Redis (TLS, with auth token) | **7.1.0** | b |

There are **2 Secrets** in total.

```sh
IMAGES=(
  "svc-a:v-1"
  "svc-b:v-2"
)
CHARTS=(
  "uniphore-baic:0.1.2"
  "svc-a:0.1.0-vabc"
)
```

### `db` — Postgres

```sh
kubectl -n $NAMESPACE create secret generic db \\
  --from-literal=host='<YOUR-HOST>' \\
  --from-literal=database='xforge' \\
  --from-literal=api='<KEY-OR-EMPTY>' \\
  --from-literal=extra='' \\
  --from-file=PEM=/tmp/k.pem
kubectl -n $NAMESPACE create secret docker-registry docker-config \\
  --docker-server=registry.example.com
```

```sh
kubectl -n $NAMESPACE get secret \\
  docker-config db
```
"""


def test_parse_mini_guide():
    s = parse_guide(MINI)
    assert s.images == {"svc-a": "v-1", "svc-b": "v-2"}
    assert s.charts == {"uniphore-baic": "0.1.2", "svc-a": "0.1.0-vabc"}
    kinds = {k.name: (k.kind, k.value) for k in s.secrets["db"].keys}
    assert kinds == {"host": (REQUIRED, "<YOUR-HOST>"), "database": (FIXED, "xforge"),
                     "api": (OPTIONAL, "<KEY-OR-EMPTY>"), "extra": (OPTIONAL, ""),
                     "PEM": (REQUIRED, "")}
    assert s.secrets["docker-config"].type == "docker-registry"
    assert s.secrets["db"].section == "`db` — Postgres"
    assert s.verify_list == ["docker-config", "db"]
    assert (s.min_k8s, s.min_nodes, s.min_node_cpu, s.min_total_mem_gib) == ("1.28", 2, 2.0, 8.0)
    assert (s.footprint_cpu, s.footprint_mem_gib) == (2.0, 3.0)
    assert (s.min_postgres, s.min_redis) == ("17.4", "7.1.0")
    assert lint(s) == []


def test_lint_catches_drift():
    broken = MINI.replace("There are **2 Secrets**", "There are **3 Secrets**").replace(
        "  docker-config db", "  docker-config db ghost")
    issues = lint(parse_guide(broken))
    assert "prose says 3 secrets, commands define 2" in issues
    assert "secret ghost is in the verify command but never created" in issues


def test_bundled_spec_round_trips(spec):
    assert Spec.from_json(spec.to_json()) == spec
    assert len(spec.images) == 14 and len(spec.charts) == 15 and len(spec.secrets) == 15
    assert spec.secrets["xforge-db"].key("database").value == "xforge"


@pytest.mark.online
def test_bundled_spec_matches_pinned_guide(spec, tmp_path):
    try:
        text = fetch_guide(GUIDE_URL)
    except OSError as e:
        pytest.skip(f"offline: {e}")
    live = parse_guide(text, source=GUIDE_URL)
    assert live == spec
    assert lint(live) == []
    assert live.stated == {"images": 14, "charts": 15, "secrets": 15, "services": 13}


def test_load_spec_from_markdown(tmp_path):
    p = tmp_path / "guide.md"
    p.write_text(MINI)
    assert load_spec(str(p)).images["svc-b"] == "v-2"
