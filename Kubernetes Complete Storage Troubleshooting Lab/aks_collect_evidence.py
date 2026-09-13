#!/usr/bin/env python3
"""
collect_evidence.py (AKS variant) — Capture diagnosis evidence for the AKS lab.

Same evidence layers as the minikube lab, plus the AKS-only signals that hostpath
cannot produce: the Azure Disk CSI *controller* logs showing the attach call to
the Azure API, and the node CSI driver pods (csi-azuredisk-node).

  WHAT'S DIFFERENT FROM MINIKUBE
  ------------------------------
  - csi-driver-pods targets csi-azuredisk-node (not the hostpath plugin).
  - A new 'csi' layer pulls csi-azuredisk-controller logs. On a real attach
    failure these logs contain the AttachVolume call and the Azure API error
    (SKU/zone/quota). This is the genuine root-cause evidence and has NO
    minikube equivalent, because hostpath never attaches an external disk.

Layers captured:
  kubectl  — cluster/pod/pvc/event state, CSI node pods, describe output
  csi      — csi-azuredisk-controller logs (attach call + Azure API error)
  linux    — in-pod mount table, df, process list
  dns      — nslookup against the ClusterIP service, resolv.conf
  packet   — tcpdump on UDP/TCP 53 from a netshoot ephemeral debug pod

Usage:
    python collect_evidence.py --out ./evidence
    python collect_evidence.py --skip packet

Requirements: kubectl configured against the AKS cluster.
"""
from __future__ import annotations

import argparse
import datetime as dt
import pathlib
import subprocess

NS = "irlab"
POD = "web-0"
SVC_FQDN = "web.irlab.svc.cluster.local"

COMMANDS: list[tuple[str, str, list[str]]] = [
    ("kubectl", "get-pods", ["get", "pods", "-n", NS, "-o", "wide"]),
    ("kubectl", "get-pvc", ["get", "pvc", "-n", NS]),
    ("kubectl", "get-events", ["get", "events", "-n", NS,
                               "--sort-by=.lastTimestamp"]),
    ("kubectl", "describe-pod", ["describe", "pod", POD, "-n", NS]),
    ("kubectl", "describe-pvc", ["describe", "pvc", "-n", NS]),
    ("kubectl", "csi-node-pods", ["get", "pods", "-n", "kube-system",
                                  "-l", "app=csi-azuredisk-node", "-o", "wide"]),
    ("linux", "mounts", ["exec", "-n", NS, POD, "--", "sh", "-c",
                         "mount | grep -E 'nginx|pvc' || mount"]),
    ("linux", "df", ["exec", "-n", NS, POD, "--", "df", "-hT"]),
    ("linux", "proc", ["exec", "-n", NS, POD, "--", "ps", "-ef"]),
    ("dns", "resolv-conf", ["exec", "-n", NS, POD, "--",
                            "cat", "/etc/resolv.conf"]),
]


def ts() -> str:
    return dt.datetime.now().strftime("%Y%m%d-%H%M%S")


def capture(outdir: pathlib.Path, layer: str, slug: str,
            argv: list[str]) -> None:
    dest = outdir / layer
    dest.mkdir(parents=True, exist_ok=True)
    path = dest / f"{slug}.txt"
    cmd = ["kubectl", *argv]
    print(f"+ [{layer}] {slug}")
    proc = subprocess.run(cmd, capture_output=True, text=True)
    path.write_text(
        f"$ {' '.join(cmd)}\n\n"
        f"--- stdout ---\n{proc.stdout}\n"
        f"--- stderr ---\n{proc.stderr}\n"
        f"(exit {proc.returncode})\n"
    )


def capture_csi_controller(outdir: pathlib.Path) -> None:
    """Pull the Azure Disk CSI controller logs — the AKS-only attach evidence."""
    dest = outdir / "csi"
    dest.mkdir(parents=True, exist_ok=True)
    cmd = ["kubectl", "-n", "kube-system", "logs",
           "-l", "app=csi-azuredisk-controller",
           "-c", "azuredisk", "--tail=200", "--prefix=true"]
    print("+ [csi] azuredisk-controller-logs")
    proc = subprocess.run(cmd, capture_output=True, text=True)
    (dest / "azuredisk-controller-logs.txt").write_text(
        f"$ {' '.join(cmd)}\n\n"
        "# Look for AttachVolume / attach calls and any Azure API error\n"
        "# (SKU not supported, zone mismatch, quota). This is the root cause\n"
        "# of a real FailedAttachVolume and has no minikube equivalent.\n\n"
        f"--- stdout ---\n{proc.stdout}\n"
        f"--- stderr ---\n{proc.stderr}\n"
        f"(exit {proc.returncode})\n"
    )



def write_cmd(dest, slug, cmd, header=""):
    dest.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(cmd, capture_output=True, text=True)
    (dest / f"{slug}.txt").write_text(
        f"$ {' '.join(cmd)}\n\n{header}"
        f"--- stdout ---\n{proc.stdout}\n"
        f"--- stderr ---\n{proc.stderr}\n"
        f"(exit {proc.returncode})\n"
    )


def capture_dns_lookup(outdir: pathlib.Path) -> None:
    """DNS lookup from busybox:1.28 (has nslookup). nginx does not, so exec-ing
    into web-0 would record 'executable not found', not the real DNS result."""
    dest = outdir / "dns"
    web = ["kubectl", "-n", NS, "run", "eviddns-web", "--rm", "-i",
           "--restart=Never", "--labels=app=web", "--image=busybox:1.28",
           "--", "nslookup", SVC_FQDN]
    print("+ [dns] nslookup-from-app-web (busybox:1.28)")
    write_cmd(dest, "nslookup-app-web", web,
              header="# app=web pod: subject to the NetworkPolicy; times out "
                     "when the DNS-egress fault is active.\n\n")
    free = ["kubectl", "-n", NS, "run", "eviddns-free", "--rm", "-i",
            "--restart=Never", "--image=busybox:1.28",
            "--", "nslookup", SVC_FQDN]
    print("+ [dns] nslookup-from-unlabeled (comparison)")
    write_cmd(dest, "nslookup-comparison", free,
              header="# unlabeled pod: not selected by the policy; should "
                     "resolve -> proves the block is policy-scoped.\n\n")


def capture_packet(outdir: pathlib.Path) -> None:
    dest = outdir / "packet"
    dest.mkdir(parents=True, exist_ok=True)
    debug = [
        "kubectl", "debug", "-n", NS, POD, "-it",
        "--image=nicolaka/netshoot", "--",
        "sh", "-c",
        "timeout 15 tcpdump -n -i any port 53 -c 20 & "
        f"sleep 2; nslookup {SVC_FQDN}; wait",
    ]
    print("+ [packet] tcpdump-port-53")
    proc = subprocess.run(debug, capture_output=True, text=True)
    (dest / "tcpdump-port-53.txt").write_text(
        f"$ {' '.join(debug)}\n\n"
        f"--- stdout ---\n{proc.stdout}\n"
        f"--- stderr ---\n{proc.stderr}\n"
        f"(exit {proc.returncode})\n"
    )


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--out", default="./evidence")
    p.add_argument("--skip", action="append", default=[],
                   choices=["kubectl", "csi", "linux", "dns", "packet"],
                   help="skip a capture layer (repeatable)")
    args = p.parse_args()

    outdir = pathlib.Path(args.out) / ts()
    outdir.mkdir(parents=True, exist_ok=True)
    print(f"# evidence -> {outdir}")

    for layer, slug, argv in COMMANDS:
        if layer in args.skip:
            continue
        capture(outdir, layer, slug, argv)

    if "dns" not in args.skip:
        capture_dns_lookup(outdir)
    if "csi" not in args.skip:
        capture_csi_controller(outdir)
    if "packet" not in args.skip:
        capture_packet(outdir)

    print(f"\nDone. Bundle written to {outdir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
