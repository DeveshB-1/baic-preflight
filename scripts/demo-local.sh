#!/usr/bin/env bash
# Stand up a real local mirror registry and a real Postgres, mirror the guide's
# artifacts into the registry the way a customer would (with one image
# forgotten), then run baic-preflight against both.
#
# Needs podman. The images are busybox re-tagged under the guide's names and
# tags, and the charts are placeholder .tgz files pushed as OCI artifacts: the
# preflight only checks that each name:tag exists, which is the part customers
# get wrong.
set -euo pipefail
cd "$(dirname "$0")/.."

REG_PORT=${REG_PORT:-5055}
PG_PORT=${PG_PORT:-5455}
REG="127.0.0.1:${REG_PORT}"
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT

cleanup() { podman rm -f baic-demo-registry baic-demo-pg >/dev/null 2>&1 || true; }
if [[ "${1:-}" == "--down" ]]; then cleanup; exit 0; fi
cleanup

podman run -d --name baic-demo-registry -p "${REG}:5000" docker.io/library/registry:2 >/dev/null
podman run -d --name baic-demo-pg -p "127.0.0.1:${PG_PORT}:5432" \
  -e POSTGRES_PASSWORD=demo docker.io/library/postgres:16-alpine >/dev/null

# The mirror loop from Step 1, with xforge-db-migrations left out of the list
# (it is the one image with no matching service in the "What you'll deploy" table).
python3 - <<'EOF' > "$WORK/lists"
import json
s = json.load(open("spec/baic-0.1.2.json"))
for n, t in s["images"].items():
    if n != "xforge-db-migrations":
        print("image", n, t)
for n, v in s["charts"].items():
    print("chart", n, v)
EOF

while read -r kind name tag; do
  if [[ $kind == image ]]; then
    podman tag docker.io/library/busybox:1.36 "${REG}/baic/${name}:${tag}"
    podman push -q --tls-verify=false "${REG}/baic/${name}:${tag}"
    podman rmi -f "${REG}/baic/${name}:${tag}" >/dev/null
  else
    mkdir -p "$WORK/$name"
    printf 'apiVersion: v2\nname: %s\nversion: %s\n' "$name" "$tag" > "$WORK/$name/Chart.yaml"
    tar -C "$WORK" -czf "$WORK/${name}-${tag}.tgz" "$name"
    podman artifact add --type application/vnd.cncf.helm.config.v1+json \
      "${REG}/baic-charts/${name}:${tag}" "$WORK/${name}-${tag}.tgz" >/dev/null
    podman artifact push -q --tls-verify=false "${REG}/baic-charts/${name}:${tag}" >/dev/null
    podman artifact rm "${REG}/baic-charts/${name}:${tag}" >/dev/null
  fi
done < "$WORK/lists"

# Postgres: the role the customer created, minus CREATEDB (a common miss).
for _ in $(seq 30); do
  PGPASSWORD=demo psql -h 127.0.0.1 -p "$PG_PORT" -U postgres -XAtqc 'select 1' >/dev/null 2>&1 && break
  sleep 1
done
PGPASSWORD=demo psql -h 127.0.0.1 -p "$PG_PORT" -U postgres -XAtqc \
  "create role baic login password 'demo'" >/dev/null

echo "\$ baic-preflight check --images ${REG}/baic --charts oci://${REG}/baic-charts --plain-http \\"
echo "    --pg-dsn postgresql://baic@127.0.0.1:${PG_PORT}/postgres --quiet-ok"
set +e
PGPASSWORD=demo uv run --quiet baic-preflight check \
  --images "${REG}/baic" --charts "oci://${REG}/baic-charts" --plain-http \
  --pg-dsn "postgresql://baic@127.0.0.1:${PG_PORT}/postgres" --quiet-ok
echo "exit code: $?"
