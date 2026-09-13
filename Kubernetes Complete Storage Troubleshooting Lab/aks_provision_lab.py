#!/usr/bin/env python3
"""
provision_lab.py (AKS variant) — Stand up a trial-friendly AKS incident lab.

Creates a resource group and a single-node AKS cluster with the Azure Disk CSI
driver, then deploys a StatefulSet mounting a CSI-provisioned PVC plus a
ClusterIP service. Defaults are tuned to fit inside an Azure Free Trial vCPU
quota: 1 node on a burstable Standard_B2s (2 vCPU).

  WHY THESE DEFAULTS (vs. the minikube lab)
  -----------------------------------------
  minikube runs nodes as local containers/VMs with no cloud quota. AKS nodes are
  real Azure VMs that consume your subscription's regional vCPU quota. Free Trial
  subscriptions cannot raise quota, so this script defaults to the smallest
  workable footprint and runs a quota pre-check before creating anything.

Usage:
    # 1. See your vCPU headroom in the target region first:
    python provision_lab.py --check-quota --location eastus

    # 2. Provision (single B2s node fits a trial):
    python provision_lab.py --resource-group aks-irlab-rg --cluster aks-irlab \\
        --location eastus --node-count 1 --node-size Standard_B2s

Requirements: az CLI (logged in), kubectl. Pass --dry-run to print commands.
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
import textwrap

# Standard_B2s / DS2_v2 both support Premium storage (the 's').
MANIFEST = textwrap.dedent(
    """\
    apiVersion: v1
    kind: Namespace
    metadata:
      name: irlab
    ---
    # Healthy CSI class. Premium_LRS requires a premium-capable VM size (B2s ok).
    # If you switch to a non-premium SKU, change this to StandardSSD_LRS.
    apiVersion: storage.k8s.io/v1
    kind: StorageClass
    metadata:
      name: irlab-csi-disk
    provisioner: disk.csi.azure.com
    parameters:
      skuName: Premium_LRS
    reclaimPolicy: Delete
    volumeBindingMode: WaitForFirstConsumer
    allowVolumeExpansion: true
    ---
    apiVersion: v1
    kind: Service
    metadata:
      name: web
      namespace: irlab
    spec:
      selector:
        app: web
      ports:
        - port: 80
          targetPort: 80
      type: ClusterIP
    ---
    apiVersion: apps/v1
    kind: StatefulSet
    metadata:
      name: web
      namespace: irlab
    spec:
      serviceName: web
      replicas: 1
      selector:
        matchLabels:
          app: web
      template:
        metadata:
          labels:
            app: web
        spec:
          containers:
            - name: nginx
              image: nginx:1.27
              ports:
                - containerPort: 80
              volumeMounts:
                - name: data
                  mountPath: /usr/share/nginx/html
      volumeClaimTemplates:
        - metadata:
            name: data
          spec:
            accessModes: ["ReadWriteOnce"]
            storageClassName: irlab-csi-disk
            resources:
              requests:
                storage: 5Gi
    """
)


def run(cmd: list[str], dry_run: bool) -> None:
    print("+ " + " ".join(cmd))
    if dry_run:
        return
    subprocess.run(cmd, check=True)


def require(binary: str) -> None:
    if shutil.which(binary) is None:
        sys.exit(f"error: '{binary}' not found on PATH")


def check_quota(location: str, node_size: str, node_count: int) -> None:
    """Report regional vCPU headroom and warn if the request won't fit.

    minikube has no equivalent — local nodes don't draw against a cloud quota.
    On a Free Trial you cannot raise this limit, so check before creating.
    """
    require("az")
    print(f"# checking vCPU quota in {location} ...")
    out = subprocess.run(
        ["az", "vm", "list-usage", "--location", location, "-o", "json"],
        capture_output=True, text=True, check=True,
    ).stdout
    usage = json.loads(out)

    def find(name_contains: str):
        for u in usage:
            local = u.get("localName", "") or u.get("name", {}).get("localizedValue", "")
            if name_contains.lower() in local.lower():
                return u
        return None

    total = find("Total Regional vCPUs")
    if total:
        cur = int(total["currentValue"])
        lim = int(total["limit"])
        print(f"  Total Regional vCPUs: {cur} used / {lim} limit "
              f"({lim - cur} free)")
        need = 2 * node_count if "B2s" in node_size or "DS2" in node_size else node_count
        if lim - cur < need:
            print(f"  WARNING: request needs ~{need} vCPUs but only "
                  f"{lim - cur} are free. Try 1 node, a smaller SKU, or "
                  f"another region. Free Trial cannot raise this limit.")
        else:
            print(f"  OK: ~{need} vCPUs needed, {lim - cur} free.")
    else:
        print("  Could not read Total Regional vCPUs; run "
              f"`az vm list-usage --location {location} -o table` manually.")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--resource-group", default="aks-irlab-rg")
    p.add_argument("--cluster", default="aks-irlab")
    p.add_argument("--location", default="eastus")
    p.add_argument("--node-count", type=int, default=1)
    p.add_argument("--node-size", default="Standard_B2s")
    p.add_argument("--check-quota", action="store_true",
                   help="report regional vCPU headroom and exit")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()

    if args.check_quota:
        check_quota(args.location, args.node_size, args.node_count)
        return 0

    for b in ("az", "kubectl"):
        require(b)

    # Pre-flight the quota so a doomed create fails fast with a clear message.
    if not args.dry_run:
        check_quota(args.location, args.node_size, args.node_count)

    run(["az", "group", "create", "--name", args.resource_group,
         "--location", args.location], args.dry_run)

    run(["az", "aks", "create",
         "--resource-group", args.resource_group,
         "--name", args.cluster,
         "--node-count", str(args.node_count),
         "--node-vm-size", args.node_size,
         "--network-plugin", "azure",
         "--generate-ssh-keys"], args.dry_run)

    run(["az", "aks", "get-credentials",
         "--resource-group", args.resource_group,
         "--name", args.cluster, "--overwrite-existing"], args.dry_run)

    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as fh:
        fh.write(MANIFEST)
        manifest_path = fh.name
    print(f"# wrote manifest to {manifest_path}")

    run(["kubectl", "apply", "-f", manifest_path], args.dry_run)
    run(["kubectl", "-n", "irlab", "rollout", "status",
         "statefulset/web", "--timeout=300s"], args.dry_run)

    print("\nLab is up. Seed the volume with:")
    print("  kubectl -n irlab exec web-0 -- sh -c "
          "'echo lab-ok > /usr/share/nginx/html/index.html'")
    print("\nWhen finished, delete everything to stop spend:")
    print(f"  az group delete --name {args.resource_group} --yes --no-wait")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
