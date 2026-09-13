#!/usr/bin/env python3
"""
inject_fault.py (minikube) — Inject a storage or network fault into the lab.

  storage : Create a PVC bound to a StorageClass that does not exist, plus a
            consumer pod. The PVC stays Pending (ProvisioningFailed) and the
            pod stays Pending (FailedScheduling: unbound PersistentVolumeClaims).

            CLOUD DIFFERENCE: on AKS/EKS/GKE/Portworx you would instead bind to
            a class with an invalid disk SKU/zone/replication (e.g. UltraSSD_LRS
            on an unsupported node), producing a real FailedAttachVolume at the
            attach stage — a failure hostpath cannot reproduce because it never
            attaches an external disk.

  network : Apply a NetworkPolicy that allows egress on 80/443 but omits UDP/TCP
            53, breaking in-cluster DNS while the pod stays Running. Behaves
            identically on cloud/bare metal (needs a policy-enforcing CNI).

VERIFYING THE DNS FAULT (learned the hard way): the nginx image has no nslookup,
so `kubectl exec web-0 -- nslookup ...` fails with 'executable not found' (exit
127) — that is NOT the DNS block, just a missing tool. Use the built-in
verifier, which runs the lookup from a busybox:1.28 pod that HAS nslookup:

    python inject_fault.py network --verify

--verify with no fault present runs a control lookup (should succeed). Run it
again after injecting to see the timeout. It also runs an UNLABELED comparison
pod (not app=web) that keeps resolving — proving the block is policy-scoped.

Usage:
    python inject_fault.py storage
    python inject_fault.py network
    python inject_fault.py network --verify
    python inject_fault.py --revert network

Requirements: kubectl pointed at the minikube profile. --dry-run prints only.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import textwrap

NAMESPACE = "irlab"
SVC_FQDN = "web.irlab.svc.cluster.local"

STORAGE_BAD_PVC = textwrap.dedent(
    """\
    apiVersion: v1
    kind: PersistentVolumeClaim
    metadata:
      name: broken-data
      namespace: irlab
    spec:
      accessModes: ["ReadWriteOnce"]
      storageClassName: nonexistent-csi-class
      resources:
        requests:
          storage: 1Gi
    """
)

STORAGE_CONSUMER = textwrap.dedent(
    """\
    apiVersion: v1
    kind: Pod
    metadata:
      name: broken-consumer
      namespace: irlab
    spec:
      containers:
        - name: app
          image: busybox:1.36
          command: ["sh", "-c", "sleep 3600"]
          volumeMounts:
            - name: data
              mountPath: /data
      volumes:
        - name: data
          persistentVolumeClaim:
            claimName: broken-data
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
            check: bool = True) -> subprocess.CompletedProcess | None:
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
    apply_manifest(STORAGE_BAD_PVC, dry_run)
    apply_manifest(STORAGE_CONSUMER, dry_run)
    print("\nFault injected. Diagnose with:")
    print("  kubectl -n irlab get pvc")
    print("  kubectl -n irlab describe pvc broken-data")
    print("  kubectl -n irlab describe pod broken-consumer")


def revert_storage(dry_run: bool) -> None:
    kubectl(["-n", NAMESPACE, "delete", "pod", "broken-consumer",
             "--ignore-not-found"], None, dry_run)
    kubectl(["-n", NAMESPACE, "delete", "pvc", "broken-data",
             "--ignore-not-found"], None, dry_run)


def inject_network(dry_run: bool) -> None:
    apply_manifest(DNS_DENY_POLICY, dry_run)
    print("\nFault injected. Verify the DNS block with a tool that HAS nslookup:")
    print("  python inject_fault.py network --verify")


def revert_network(dry_run: bool) -> None:
    kubectl(["-n", NAMESPACE, "delete", "networkpolicy", "deny-dns-egress",
             "--ignore-not-found"], None, dry_run)


def verify_network(dry_run: bool) -> None:
    """Reproduce the DNS result correctly, using busybox:1.28 (has nslookup).

    Two lookups:
      1. A sidecar labelled app=web -> subject to the policy -> should TIME OUT
         once the fault is injected (succeeds if not injected: this is control).
      2. An UNLABELED pod -> not selected by the policy -> should always resolve,
         proving the failure is policy-scoped, not a broken resolver.
    """
    print("# [1] lookup from an app=web pod (policy applies -> expect timeout "
          "when fault active):")
    kubectl(["-n", NAMESPACE, "run", "dns-web", "--rm", "-i", "--restart=Never",
             "--labels=app=web", "--image=busybox:1.28", "--",
             "nslookup", SVC_FQDN], None, dry_run, check=False)
    print("\n# [2] lookup from an UNLABELED pod (policy does NOT apply -> should "
          "resolve):")
    kubectl(["-n", NAMESPACE, "run", "dns-free", "--rm", "-i", "--restart=Never",
             "--image=busybox:1.28", "--",
             "nslookup", SVC_FQDN], None, dry_run, check=False)
    print("\nInterpretation: [1] timing out while [2] resolves proves the "
          "NetworkPolicy (not the resolver) is blocking DNS egress for app=web.")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("scenario", choices=["storage", "network"])
    p.add_argument("--revert", action="store_true")
    p.add_argument("--verify", action="store_true",
                   help="network only: run correct DNS reproduction + comparison")
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
