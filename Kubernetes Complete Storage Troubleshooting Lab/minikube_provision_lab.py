#!/usr/bin/env python3
"""
provision_lab.py (minikube) — Stand up a local Kubernetes incident-response lab.

Starts a single-node minikube cluster with the Calico CNI (required so
NetworkPolicy is actually enforced), enables the CSI hostpath driver and
volumesnapshots addons, then deploys a StatefulSet that mounts a CSI-provisioned
PVC plus a ClusterIP service. This is the free, local rehearsal for the AKS lab.

  PERFORMANCE / SETUP REQUIREMENTS (read before running)
  ------------------------------------------------------
  minikube on Windows runs on Docker Desktop's WSL2 VM. Two-node Calico clusters
  on the default 2 CPU / 3 GB allocation reliably starve the Docker daemon
  (symptom: `docker ps` hangs for 15+ seconds, `minikube start` takes 30+ min).
  Fixes baked into the defaults here, plus manual setup you must do once:

    1. Single node by default (--nodes 1). Both faults reproduce identically on
       one node; a second node only adds load with no teaching value here.
    2. Give WSL2 room. Create %UserProfile%\\.wslconfig with:
           [wsl2]
           memory=8GB
           processors=4
           swap=2GB
       Save as .wslconfig (NOT .wslconfig.txt), then `wsl --shutdown` and
       restart Docker Desktop. Verify with:
           docker info --format "{{.NCPU}} CPUs, {{.MemTotal}} bytes"
    3. Calico is mandatory. Without a policy-enforcing CNI the DNS fault
       silently does nothing.

  CLOUD/BARE-METAL DIFFERENCE
  ---------------------------
  On AKS/EKS/GKE or bare metal you would NOT run `minikube start` — you would
  provision real nodes and the platform CSI driver (disk.csi.azure.com,
  ebs.csi.aws.com, pd.csi.storage.gke.io, or Portworx / Ceph-RBD). The hostpath
  driver here writes to the node's local filesystem instead of attaching a real
  block volume, so it exercises the control-plane provisioning path but not a
  true attach/detach. Everything downstream (kubectl, Linux, DNS, packet
  analysis) is identical.

Usage:
    python provision_lab.py --profile irlab --nodes 1 --cni calico
    python provision_lab.py --dry-run          # print commands only

Requirements: minikube, kubectl, Docker Desktop running. Python 3.9+.
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
import textwrap

MANIFEST = textwrap.dedent(
    """\
    apiVersion: v1
    kind: Namespace
    metadata:
      name: irlab
    ---
    # Healthy CSI StorageClass backed by the minikube hostpath CSI driver.
    # CLOUD: replace provisioner with the platform driver and real parameters.
    apiVersion: storage.k8s.io/v1
    kind: StorageClass
    metadata:
      name: irlab-csi-disk
    provisioner: hostpath.csi.k8s.io
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
                storage: 1Gi
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


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--profile", default="irlab")
    p.add_argument("--nodes", type=int, default=1,
                   help="1 is recommended; 2 needs 8GB+ WSL2 memory")
    p.add_argument("--cni", default="calico",
                   help="must enforce NetworkPolicy (calico/cilium)")
    p.add_argument("--driver", default=None)
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()

    for b in ("minikube", "kubectl"):
        require(b)

    start = ["minikube", "start", "-p", args.profile,
             "--nodes", str(args.nodes), "--cni", args.cni]
    if args.driver:
        start += ["--driver", args.driver]
    run(start, args.dry_run)

    run(["minikube", "-p", args.profile, "addons", "enable",
         "volumesnapshots"], args.dry_run)
    run(["minikube", "-p", args.profile, "addons", "enable",
         "csi-hostpath-driver"], args.dry_run)

    run(["kubectl", "config", "use-context", args.profile], args.dry_run)

    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as fh:
        fh.write(MANIFEST)
        manifest_path = fh.name
    print(f"# wrote manifest to {manifest_path}")

    run(["kubectl", "apply", "-f", manifest_path], args.dry_run)
    run(["kubectl", "-n", "irlab", "rollout", "status",
         "statefulset/web", "--timeout=180s"], args.dry_run)

    print("\nLab is up. Seed the volume (note the DOUBLE quotes on Windows):")
    print('  kubectl -n irlab exec web-0 -- '
          'sh -c "echo lab-ok > /usr/share/nginx/html/index.html"')
    print("  kubectl -n irlab exec web-0 -- cat /usr/share/nginx/html/index.html")
    print("\nWhen finished with the whole lab, tear it down:")
    print(f"  minikube delete -p {args.profile}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
