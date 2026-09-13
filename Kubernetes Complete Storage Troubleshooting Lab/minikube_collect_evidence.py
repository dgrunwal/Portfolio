#!/usr/bin/env python3
"""
collect_evidence.py (minikube) — Capture a diagnosis evidence bundle.

Snapshots every diagnostic layer into a timestamped directory. Run it WHILE a
fault is active — it captures whatever is true at that moment. Run it once per
fault (a fresh timestamp each time, so bundles are not overwritten).

  CORRECTIONS FROM THE FIRST RUN
  ------------------------------
  * DNS is captured from a busybox:1.28 helper pod that HAS nslookup, instead of
    exec-ing into nginx (which does not) — so the bundle records a real timeout
    for the network fault, not 'executable not found'.
  * PVC/pod evidence also saved as YAML so the .status and events survive even
    when `describe` truncates them.
  * A policy-scoped comparison lookup (unlabeled pod) is captured for the network
    fault, proving the block is scoped to app=web.

  CLOUD DIFFERENCE
  ----------------
  On AKS add a csi/ layer capturing csi-azuredisk-controller logs (the real
  AttachVolume API error). Hostpath has no such controller call, so that layer
  only exists on the cloud variant.

Layers: kubectl (state/events/describe/yaml, CSI pods), linux (mount/df/ps),
dns (nslookup from busybox + comparison, resolv.conf), packet (tcpdump on 53).

Usage:
    python collect_evidence.py --out ./evidence
    python collect_evidence.py --skip packet

Requirements: kubectl pointed at the minikube profile.
"""
from __future__ import annotations

import argparse
import datetime as dt
import pathlib
import subprocess

NS = "irlab"
POD = "web-0"
SVC_FQDN = "web.irlab.svc.cluster.local"

# Plain kubectl captures (layer, slug, argv-after-kubectl).
COMMANDS: list[tuple[str, str, list[str]]] = [
    ("kubectl", "get-pods", ["get", "pods", "-n", NS, "-o", "wide"]),
    ("kubectl", "get-pvc", ["get", "pvc", "-n", NS]),
    ("kubectl", "get-events", ["get", "events", "-n", NS,
                               "--sort-by=.lastTimestamp"]),
    ("kubectl", "describe-pvc", ["describe", "pvc", "-n", NS]),
    ("kubectl", "describe-pods", ["describe", "pods", "-n", NS]),
    ("kubectl", "pvc-yaml", ["get", "pvc", "-n", NS, "-o", "yaml"]),
    ("kubectl", "netpol", ["get", "networkpolicy", "-n", NS, "-o", "wide"]),
    ("kubectl", "csi-driver-pods", ["get", "pods", "-n", "kube-system",
                                    "-l", "app.kubernetes.io/name=csi-hostpathplugin",
                                    "-o", "wide"]),
    ("linux", "df", ["exec", "-n", NS, POD, "--", "df", "-hT"]),
    ("linux", "mounts", ["exec", "-n", NS, POD, "--", "sh", "-c",
                         "mount | grep -E 'nginx|pvc' || mount"]),
    ("linux", "proc", ["exec", "-n", NS, POD, "--", "ps", "-ef"]),
    ("dns", "resolv-conf", ["exec", "-n", NS, POD, "--",
                            "cat", "/etc/resolv.conf"]),
]


def ts() -> str:
    return dt.datetime.now().strftime("%Y%m%d-%H%M%S")


def write(dest: pathlib.Path, slug: str, cmd: list[str],
          header: str = "") -> None:
    dest.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(cmd, capture_output=True, text=True)
    (dest / f"{slug}.txt").write_text(
        f"$ {' '.join(cmd)}\n\n{header}"
        f"--- stdout ---\n{proc.stdout}\n"
        f"--- stderr ---\n{proc.stderr}\n"
        f"(exit {proc.returncode})\n"
    )


def capture_dns_lookup(outdir: pathlib.Path) -> None:
    """Run the service lookup from busybox:1.28 (has a working nslookup).

    Captures both the policy-selected pod (app=web -> may time out) and an
    unlabeled comparison pod (-> should resolve). This is the corrected DNS
    evidence that the nginx exec could never produce.
    """
    dest = outdir / "dns"
    dest.mkdir(parents=True, exist_ok=True)

    web = ["kubectl", "-n", NS, "run", "eviddns-web", "--rm", "-i",
           "--restart=Never", "--labels=app=web", "--image=busybox:1.28",
           "--", "nslookup", SVC_FQDN]
    print("+ [dns] nslookup-from-app-web (busybox:1.28)")
    write(dest, "nslookup-app-web", web,
          header="# app=web pod: subject to the NetworkPolicy.\n"
                 "# Times out when the DNS-egress fault is active.\n\n")

    free = ["kubectl", "-n", NS, "run", "eviddns-free", "--rm", "-i",
            "--restart=Never", "--image=busybox:1.28",
            "--", "nslookup", SVC_FQDN]
    print("+ [dns] nslookup-from-unlabeled (comparison)")
    write(dest, "nslookup-comparison", free,
          header="# unlabeled pod: NOT selected by the policy.\n"
                 "# Should resolve even while app=web is blocked -> proves\n"
                 "# the block is policy-scoped, not a broken resolver.\n\n")


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
    write(dest, "tcpdump-port-53", debug)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--out", default="./evidence")
    p.add_argument("--skip", action="append", default=[],
                   choices=["kubectl", "linux", "dns", "packet"])
    args = p.parse_args()

    outdir = pathlib.Path(args.out) / ts()
    outdir.mkdir(parents=True, exist_ok=True)
    print(f"# evidence -> {outdir}")

    for layer, slug, argv in COMMANDS:
        if layer in args.skip:
            continue
        print(f"+ [{layer}] {slug}")
        write(outdir / layer, slug, ["kubectl", *argv])

    if "dns" not in args.skip:
        capture_dns_lookup(outdir)
    if "packet" not in args.skip:
        capture_packet(outdir)

    print(f"\nDone. Bundle written to {outdir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
