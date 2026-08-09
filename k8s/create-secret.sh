#!/usr/bin/env bash
# Creates/updates the argus-cloud-secrets Secret from the existing local
# .env — the 5 hosted-target DSNs plus the agent's GEMINI_API_KEY (not STORE_DSN, which
# is a leftover unused var; not committed anywhere, ever).
#
# Run from the repo root, after `kind create cluster` and
# `kubectl apply -f k8s/namespace.yaml`:
#
#   bash k8s/create-secret.sh
#
# Safe to re-run — dry-run|apply makes this create-or-update, not
# create-or-fail.

set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

# A real temp file, not process substitution: kubectl here is a native
# Windows binary and can't open Git Bash's /proc/*/fd/* substitution paths.
tmp_env="$(mktemp)"
trap 'rm -f "$tmp_env"' EXIT
grep -E '^(NEON_DSN|SUPA_DSN|MONGO_URI|REDIS_CACHE_URL|REDIS_SESSION_URL|GEMINI_API_KEY)=' .env > "$tmp_env"

kubectl create secret generic argus-cloud-secrets \
  --from-env-file="$tmp_env" \
  -n argus \
  --dry-run=client -o yaml | kubectl apply -f -

echo "argus-cloud-secrets updated. Keys:"
kubectl get secret argus-cloud-secrets -n argus -o jsonpath='{.data}' | tr ',' '\n' | grep -o '"[A-Z_]*"' | tr -d '"'
