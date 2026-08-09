#!/usr/bin/env python3
"""Regenerate k8s ConfigMaps from deploy/, and stamp config hashes.

Run from the repo root after editing anything under deploy/:

    python k8s/generate.py

Two jobs:

1. Rebuild each ConfigMap from the real file in deploy/ (via `kubectl create
   configmap --dry-run=client`), so the committed YAML can never drift from
   the config the compose stack uses.

2. Stamp a hash of that content into the consuming workload's POD TEMPLATE
   annotations.

Why (2) matters — this was a real bug, not theory: updating a ConfigMap does
NOT change a Deployment's spec, so Kubernetes has no reason to roll the pods,
and processes that read their config once at startup (Grafana provisioning,
Prometheus, Loki, Tempo, Alloy) keep running the old config forever. ArgoCD
happily reports Synced the whole time. Putting the content hash in the pod
template means a config change *is* a spec change, which triggers a normal
rolling update. This is exactly what Helm's `checksum/config` annotation does;
we do it by hand because these are deliberately raw manifests.
"""

import hashlib
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
K8S = REPO / "k8s"
NS = "argus"

# configmap name -> {key in configmap: source file under deploy/}
CONFIGMAPS = {
    "argus-fleet-config": {"fleet.yaml": "fleet.yaml"},
    "argus-prometheus-config": {"prometheus.yml": "prometheus/prometheus.yml"},
    "argus-mimir-config": {"mimir.yaml": "mimir/mimir.yaml"},
    "argus-loki-config": {"loki.yaml": "loki/loki.yaml"},
    "argus-tempo-config": {"tempo.yaml": "tempo/tempo.yaml"},
    # k8s uses the kubernetes-discovery variant, mounted under the same name
    "argus-alloy-config": {"config.alloy": "alloy/config-k8s.alloy"},
    "argus-grafana-datasources": {
        "datasources.yaml": "grafana/provisioning/datasources/datasources.yaml"
    },
    "argus-grafana-dashboard-provider": {
        "dashboards.yaml": "grafana/provisioning/dashboards/dashboards.yaml"
    },
    "argus-grafana-dashboards": {
        "argus-fleet.json": "grafana/dashboards/argus-fleet.json",
        "argus-instance.json": "grafana/dashboards/argus-instance.json",
    },
}

# output filename under k8s/configmaps/
CM_FILE = {
    "argus-fleet-config": "fleet-config.yaml",
    "argus-prometheus-config": "prometheus-config.yaml",
    "argus-mimir-config": "mimir-config.yaml",
    "argus-loki-config": "loki-config.yaml",
    "argus-tempo-config": "tempo-config.yaml",
    "argus-alloy-config": "alloy-config.yaml",
    "argus-grafana-datasources": "grafana-datasources.yaml",
    "argus-grafana-dashboard-provider": "grafana-dashboard-provider.yaml",
    "argus-grafana-dashboards": "grafana-dashboards.yaml",
}

# workload manifest -> the configmaps whose content it actually reads
CONSUMERS = {
    "collector.yaml": ["argus-fleet-config"],
    "agent.yaml": ["argus-fleet-config"],
    "prometheus.yaml": ["argus-prometheus-config"],
    "mimir.yaml": ["argus-mimir-config"],
    "loki.yaml": ["argus-loki-config"],
    "tempo.yaml": ["argus-tempo-config"],
    "alloy.yaml": ["argus-alloy-config"],
    "grafana.yaml": [
        "argus-grafana-datasources",
        "argus-grafana-dashboard-provider",
        "argus-grafana-dashboards",
    ],
}

HASH_RE = re.compile(r'(argus\.dev/config-hash:\s*")([0-9a-f]*)(")')


def build_configmap(name: str, keys: dict[str, str]) -> str:
    cmd = ["kubectl", "create", "configmap", name]
    for key, rel in keys.items():
        src = REPO / "deploy" / rel
        if not src.exists():
            sys.exit(f"missing source file: {src}")
        cmd.append(f"--from-file={key}={src}")
    cmd += ["-n", NS, "--dry-run=client", "-o", "yaml"]
    done = subprocess.run(cmd, capture_output=True, text=True)
    if done.returncode != 0:
        sys.exit(f"kubectl failed for {name}:\n{done.stderr}")
    return done.stdout


def content_hash(keys: dict[str, str]) -> str:
    h = hashlib.sha256()
    for key in sorted(keys):
        h.update(key.encode())
        h.update((REPO / "deploy" / keys[key]).read_bytes())
    return h.hexdigest()[:16]


def main() -> None:
    hashes: dict[str, str] = {}

    for name, keys in CONFIGMAPS.items():
        out = K8S / "configmaps" / CM_FILE[name]
        out.write_text(build_configmap(name, keys), encoding="utf-8")
        hashes[name] = content_hash(keys)
        print(f"configmap  {name:34} -> {out.relative_to(REPO)}")

    for manifest, names in CONSUMERS.items():
        path = K8S / manifest
        text = path.read_text(encoding="utf-8")
        combined = hashlib.sha256(
            "".join(hashes[n] for n in sorted(names)).encode()
        ).hexdigest()[:16]

        if not HASH_RE.search(text):
            sys.exit(
                f"{manifest} has no argus.dev/config-hash annotation to stamp — "
                "add one to its pod template metadata.annotations first."
            )
        new_text, n = HASH_RE.subn(rf"\g<1>{combined}\g<3>", text)
        if n != 1:
            sys.exit(f"{manifest}: expected exactly 1 hash annotation, found {n}")
        if new_text != text:
            path.write_text(new_text, encoding="utf-8")
            print(f"hash       {manifest:34} -> {combined} (changed)")
        else:
            print(f"hash       {manifest:34} -> {combined}")


if __name__ == "__main__":
    main()
