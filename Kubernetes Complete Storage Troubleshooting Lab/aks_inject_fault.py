#!/usr/bin/env python3
"""
inject_fault.py — Inject a deliberate storage or network fault into the IR lab.

Two reproducible scenarios for training:

  storage : Rebind the PVC to a StorageClass whose disk SKU is invalid for the
            node size, forcing a CSI attach/provision failure. The pod goes to
            ContainerCreating and stays there; `kubectl describe` surfaces the
            FailedAttachVolume / FailedMount events for diagnosis.

  network : Apply a NetworkPolicy that denies egress DNS (UDP/TCP 53) from the
            web pods, so in-cluster name resolution of the ClusterIP service
            breaks while the pod itself stays Running. Diagnosis flows through
            nslookup/dig from a debug pod and packet capture on port 53.

Usage:
    python inject_fault.py storage
    python inject_fault.py network
    python inject_fault.py --revert network

Requirements: kubectl configured against the lab cluster. --dry-run prints only.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import tempfile
import textwrap

NAMESPACE = "irlab"
SVC_FQDN = "web.irlab.svc.cluster.local"

STORAGE_BAD_SC = textwrap.dedent(
    """\
    apiVersion: storage.k8s.io/v1
    kind: StorageClass
    metadata:
      name: irlab-csi-broken
    provisioner: disk.csi.azure.com
    parameters:
      skuName: UltraSSD_LRS
      cachingmode: ReadOnly
    reclaimPolicy: Delete
    volumeBindingMode: Immediate
    """
)

DNS_DENY_POLICY = textwrap.dedent(
    """\
    apiVersion: networking.k8s.io/v1
    kind: NetworkPolicy
    metadata:
      name: deny-dns-egress
      namespace: irlab
    spec:
      podSelector:
        matchLabels:
          app: web
      policyTypes:
        - Egress
      egress:
        - to:
            - ipBlock:
                cidr: 0.0.0.0/0
          ports:
            - protocol: TCP
              port: 80
            - protocol: TCP
              port: 443
    """
)


def kubectl(args: list[str], stdin: str | None, dry_run: bool,
            check: bool = True):
    cmd = ["kubectl", *args]
    print("+ " + " ".join(cmd))
    if dry_run:
        if stdin:
            print(textwrap.indent(stdin, "  | "))
        return None
    return subprocess.run(cmd, input=stdin, text=True, check=check)


def apply_manifest(manifest: str, dry_run: bool) -> None:
    kubectl(["apply", "-f", "-"], manifest, dry_run)


def inject_storage(dry_run: bool) -> None:
    apply_manifest(STORAGE_BAD_SC, dry_run)
    # Recreate the PVC bound to the broken class to trigger attach failure.
    with tempfile.NamedTemporaryFile("w", suffix=".yaml"):
        pass
    pvc = textwrap.dedent(
        """\
        apiVersion: v1
        kind: PersistentVolumeClaim
        metadata:
          name: broken-data
          namespace: irlab
        spec:
          accessModes: ["ReadWriteOnce"]
          storageClassName: irlab-csi-broken
          resources:
            requests:
              storage: 5Gi
        """
    )
    apply_manifest(pvc, dry_run)
    print("\nFault injected. Observe with:")
    print("  kubectl -n irlab get pvc broken-data")
    print("  kubectl -n irlab describe pvc broken-data")


def revert_storage(dry_run: bool) -> None:
    kubectl(["-n", NAMESPACE, "delete", "pvc", "broken-data",
             "--ignore-not-found"], None, dry_run)
    kubectl(["delete", "storageclass", "irlab-csi-broken",
             "--ignore-not-found"], None, dry_run)


def inject_network(dry_run: bool) -> None:
    apply_manifest(DNS_DENY_POLICY, dry_run)
    print("\nFault injected. Verify with a tool that HAS nslookup:")
    print("  python aks_inject_fault.py network --verify")


def revert_network(dry_run: bool) -> None:
    kubectl(["-n", NAMESPACE, "delete", "networkpolicy", "deny-dns-egress",
             "--ignore-not-found"], None, dry_run)



def verify_network(dry_run: bool) -> None:
    """Reproduce the DNS result with busybox:1.28 (has nslookup).

    On AKS the nginx image likewise lacks nslookup, so exec-ing into web-0 gives
    'executable not found' (exit 127), NOT the DNS block. Use these instead:
      [1] app=web sidecar -> policy applies -> times out when fault active.
      [2] unlabeled pod   -> policy does not apply -> resolves (proves scope).
    """
    print("# [1] lookup from an app=web pod (expect timeout when fault active):")
    kubectl(["-n", NAMESPACE, "run", "dns-web", "--rm", "-i", "--restart=Never",
             "--labels=app=web", "--image=busybox:1.28", "--",
             "nslookup", SVC_FQDN], None, dry_run, check=False)
    print("\n# [2] lookup from an UNLABELED pod (should resolve):")
    kubectl(["-n", NAMESPACE, "run", "dns-free", "--rm", "-i", "--restart=Never",
             "--image=busybox:1.28", "--",
             "nslookup", SVC_FQDN], None, dry_run, check=False)
    print("\nInterpretation: [1] timing out while [2] resolves proves the "
          "NetworkPolicy blocks DNS egress for app=web, not a broken resolver.")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("scenario", choices=["storage", "network"])
    p.add_argument("--revert", action="store_true",
                   help="undo the named scenario instead of injecting it")
    p.add_argument("--verify", action="store_true",
                   help="network only: correct DNS reproduction + comparison")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()

    if args.verify and args.scenario == "network" and not args.revert:
        verify_network(args.dry_run)
        return 0

    handlers = {
        ("storage", False): inject_storage,
        ("storage", True): revert_storage,
        ("network", False): inject_network,
        ("network", True): revert_network,
    }
    handlers[(args.scenario, args.revert)](args.dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(main())
