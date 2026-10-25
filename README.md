# baic-preflight

A preflight check for self-hosted installs of the Uniphore Business AI Cloud (BAIC). Run it before `helm install` and it tells you what will break, by name.

It reads the public install guide ([uniphore/baic-docs](https://github.com/uniphore/baic-docs), `index.md`), pulls out every fact a check needs and tests your environment against them:

| Check | Input | What it catches |
|---|---|---|
| `secrets` | `kubectl get secrets -n baic -o json` (live or saved) | any of the 15 Secrets missing; missing keys; required keys empty or still holding `<PLACEHOLDER>` text; `docker-config` with Harbor creds instead of your mirror's; LaunchDarkly key empty (the guide says 3 services won't start); `tag-db.jdbc_url` disagreeing with `host`/`port`; JWT PEMs that aren't PEMs |
| `cluster` | `kubectl get nodes/storageclass -o json`, `kubectl version` | Kubernetes below 1.27; fewer than 3 schedulable workers; allocatable CPU/memory under the ~12 vCPU / ~19 GiB footprint; no default StorageClass |
| `registry` | your image and chart prefixes | every one of the 14 image tags and 15 chart versions, checked with a `HEAD` on the OCI manifest (Basic and Bearer token auth, so Harbor, ECR, GHCR, Artifactory all work) |
| `postgres` | a DSN for the role BAIC will use | server below 17.4; role without `CREATEDB`, which the bundled Liquibase job needs |

Output names Secrets and keys, never values, so you can paste it into a support ticket. It's also an MCP server, so an agent walking a customer through the guide can call the same checks.

## Why this matters to Uniphore

The guide is careful. It lists the failure modes itself: `CreateContainerConfigError` from a missing key, `ImagePullBackOff` from a wrong pull secret, the LaunchDarkly startup block, Pending pods from capacity. But a customer only finds them after `helm install --atomic --timeout 20m` rolls back. Each of those is a support ticket and a slower proof of concept.

Every check here is derived from the guide, so when a release changes the image list, re-run `baic-preflight spec --write spec/baic-<ver>.json` and the checks follow.

## Quickstart

```sh
uv sync
uv run baic-preflight spec                      # parse + lint the pinned guide
uv run baic-preflight check --namespace baic \  # live, from your kubectl context
  --kubectl \
  --images registry.example.com/baic \
  --charts oci://registry.example.com/baic-charts \
  --pg-dsn postgresql://baic@pg.internal:5432/postgres
```

Registry credentials come from `BAIC_REGISTRY_USER` / `BAIC_REGISTRY_PASSWORD`, the Postgres password from `PGPASSWORD`. Exit code is 2 when there's a blocker, so it drops into CI. `--json` for machine output, `--quiet-ok` to hide passes.

As an MCP server (stdio, stdlib only, no SDK needed):

```json
{ "mcpServers": { "baic-preflight": { "command": "uv", "args": ["run", "--directory", "/path/to/baic-preflight", "baic-preflight", "mcp"] } } }
```

Tools: `baic_install_spec`, `baic_check_secrets`, `baic_check_cluster`, `baic_check_registry`, `baic_check_postgres`.

## Demo runs

### Live: a real registry and a real Postgres

`scripts/demo-local.sh` starts `registry:2` and `postgres:16-alpine` in podman, runs the guide's Step 1 mirror loop into the registry with one image left out (`xforge-db-migrations`, the one image with no row in the "What you'll deploy" table), pushes all 15 charts as OCI artifacts, and creates a `baic` role without `CREATEDB`. The images are busybox re-tagged under the guide's names and tags, and the charts are placeholder `.tgz` files; the check only looks at whether each `name:tag` exists.

```
$ baic-preflight check --images 127.0.0.1:5055/baic --charts oci://127.0.0.1:5055/baic-charts --plain-http \
    --pg-dsn postgresql://baic@127.0.0.1:5455/postgres --quiet-ok
[registry]
   x BLOCKER image baic/xforge-db-migrations:v-1afaab368b: HTTP 404: not in your mirror (the values overlay references tags exactly, so the pod will hit ImagePullBackOff)
   x BLOCKER images: 13/14 present with the exact tags from the guide

[postgres]
   x BLOCKER version: server is 16.15, the guide requires 17.4+
   x BLOCKER role: role lacks CREATEDB; the bundled Liquibase job can't create the xforge and forge-tag databases (ALTER ROLE ... CREATEDB, or set xforge-db.enabled: false and pre-migrate)

4 blockers, 0 warnings, 1 ok
verdict: NOT READY for helm install
exit code: 2
```

### Fixtures: Secrets and cluster

I don't have a BAIC tenant or a cluster here, so the Secrets and node data are synthetic `kubectl -o json` fixtures (`scripts/make_fixtures.py`). The broken namespace plants one mistake per rule. The node fixture is exactly the guide's minimum: 3 nodes of 4 vCPU / 8 GiB, with the allocatable numbers EKS reports for that size.

```
$ baic-preflight check --secrets-file fixtures/secrets-broken.json --pull-host registry.acme.example.com \
    --nodes-file fixtures/nodes-guide-minimum.json --storageclass-file fixtures/storageclass-none.json --quiet-ok
[secrets]
   x BLOCKER docker-config: holds credentials for registry.uniphore.com; the cluster pulls from your mirror, so this Secret needs your registry's credentials
   x BLOCKER docker-config: has no auth for registry.acme.example.com (has: registry.uniphore.com); expect ImagePullBackOff on every pod
   x BLOCKER miscellaneous: missing keys: agentic_search_bucket
   x BLOCKER rackspace-secret: missing; pods that mount it stay in CreateContainerConfigError (the chart references every Secret, even optional ones)
   x BLOCKER launchdarkly: SDK key is empty in both Secrets; the guide says pegasus-inference-api, guardrails-service, forge-question-answer fail to start today with an empty key
   x BLOCKER tag-db.jdbc_url: points at pg-old.acme.internal:5432/forge-tag but host/port/database say pg.acme.internal:5432/forge-tag
   x BLOCKER forge-user-management.APP_JWT_PUBLIC_KEY: is not a PEM key (or is still the guide's template)

[cluster]
   ! WARN    capacity: 11.8 vCPU / 20.7 GiB allocatable, under BAIC's default footprint of ~12 vCPU / ~19 GiB; expect some pods to stay Pending
   ! WARN    storageclass: no default StorageClass; only matters if you enable the in-chart Postgres/Redis subcharts

7 blockers, 2 warnings, 14 ok
verdict: NOT READY for helm install
exit code: 2
```

That capacity warning came out of the guide itself. Its minimum cluster (3 x 4 vCPU) has 12 vCPU of *capacity*, but kubelet reserves some, so allocatable is about 11.8, a bit under the ~12 vCPU default footprint. The footprint is approximate, so this is a warning, but I'd probably bump the minimum to 4 nodes or say "12 vCPU allocatable".

### The guide parses clean

```
$ baic-preflight spec
guide: https://raw.githubusercontent.com/uniphore/baic-docs/827cf4da3c665a97ba1414403355df1db5b6f437/index.md
  14 images, 15 charts, 15 secrets (65 keys)
  k8s >= 1.27, 3+ nodes, postgres >= 17.4, redis >= 7.1.0, footprint ~12 vCPU / ~19 GiB
  lint: guide is self-consistent
```

`spec` also lints the guide against itself: the counts in the prose ("14 container images", "There are **15 Secrets**") against the lists in the commands, and the Secrets created against the Secrets in the verify command. All consistent at this commit.

## Tests

```
$ uv run pytest -q
............................                                             [100%]
28 passed in 4.80s
```

The registry tests run against a real local HTTP server that does Harbor-style Bearer token auth. Two tests are marked `online`: one checks the bundled spec still matches the pinned guide on GitHub, the other runs the Bearer flow against Docker Hub. That second one caught a real bug: Docker Hub sends `www-authenticate` in lowercase and my first version looked the header up case-sensitively.

## Not covered (yet)

- Redis version, TLS and auth token (needs a small RESP client).
- The LaunchDarkly streaming probe: preflight tells you to run the guide's `ld-probe` pod, it doesn't run it.
- `helm template` of your values overlay (the guide's "expect 13 Deployments" check) and `forge-ui.vite.*` URLs still pointing at `localhost` after Ingress.
- Live `--namespace` / `--kubectl` reads are wired but I've only run them against saved JSON, since I don't have a cluster here.

Built by Devesh B against the public install guide. Not affiliated with Uniphore.
