# Kubernetes Storage & Network Incident-Response Lab

A self-contained incident-response exercise: provision a small Kubernetes
cluster, inject a deliberate **CSI storage** failure and a **DNS-egress**
failure, diagnose each with `kubectl` / Linux / DNS / packet tooling, and
capture a timestamped evidence bundle.

Runs two ways from the same design:

| Variant | Cost | Purpose |
|---|---|---|
| **minikube** | free | Rehearse the full loop locally |
| **AKS** | ~a few $ | Run once on real cloud storage for portfolio evidence |

The diagnosis skills are identical across both. What differs is the storage
substrate and a few extra cloud failure modes — documented in the training docs
and in each script's header.

## Repo layout

```
minikube_provision_lab.py     # start minikube (Calico) + CSI + workload
minikube_inject_fault.py      # inject/revert storage|network fault (+ --verify)
minikube_collect_evidence.py  # capture kubectl/linux/dns/packet bundle
aks_provision_lab.py          # quota pre-check + single-node AKS + workload
aks_inject_fault.py           # AKS storage (real SKU attach) | network fault
aks_collect_evidence.py       # AKS bundle incl. CSI controller logs
```

## Quick start (minikube)

```bash
# 0. One-time perf setup on Windows: see "Performance setup" below.
python minikube_provision_lab.py --profile irlab --nodes 1 --cni calico

# seed + confirm baseline (Windows: DOUBLE quotes around the redirect)
kubectl -n irlab exec web-0 -- sh -c "echo lab-ok > /usr/share/nginx/html/index.html"
kubectl -n irlab exec web-0 -- cat /usr/share/nginx/html/index.html   # -> lab-ok

# Fault A: storage
python minikube_inject_fault.py storage
kubectl -n irlab get pvc                       # broken-data = Pending
kubectl -n irlab describe pvc broken-data      # Events: storageclass ... not found
python minikube_collect_evidence.py --out ./evidence
python minikube_inject_fault.py --revert storage

# Fault B: DNS egress
python minikube_inject_fault.py network
python minikube_inject_fault.py network --verify   # busybox lookup: [1] times out, [2] resolves
python minikube_collect_evidence.py --out ./evidence
python minikube_inject_fault.py --revert network

# tear down
minikube delete -p irlab
```

## Performance setup (Windows / Docker Desktop)

minikube runs on Docker Desktop's WSL2 VM. The default 2 CPU / 3 GB allocation
is **too small** for a Calico cluster and will starve the Docker daemon
(`docker ps` hangs 15s+, `minikube start` takes 30+ minutes). Fix it once:

1. Create `%UserProfile%\.wslconfig` (save as `.wslconfig`, **not** `.wslconfig.txt`):
   ```ini
   [wsl2]
   memory=8GB
   processors=4
   swap=2GB
   ```
2. `wsl --shutdown`, then restart Docker Desktop.
3. Verify: `docker info --format "{{.NCPU}} CPUs, {{.MemTotal}} bytes"` → 4 CPU / ~8 GB.
4. Use `--nodes 1` (default). One node reproduces both faults; a second only
   adds load. Set memory ≤ half your physical RAM.

## Corrections baked in (from a real run)

- **DNS verification uses `busybox:1.28`, not the nginx pod.** `nginx:1.27` has
  no `nslookup`, so `kubectl exec web-0 -- nslookup ...` returns *"executable
  not found"* (exit 127) — a missing tool, **not** the DNS block. The `--verify`
  path and the evidence collector run the lookup from a busybox pod that has the
  tool, and add an **unlabeled comparison pod** that keeps resolving — proving
  the block is scoped to `app=web`.
- **Windows shell quoting:** commands with `>` / `|` / single quotes must use
  **double** quotes from `cmd.exe`, or be run inside the pod (`exec -it ... sh`).
- **Evidence robustness:** PVC state is also saved as YAML so `.status`/events
  survive when `describe` truncates.

## What this demonstrates (portfolio)

Kubernetes · CSI persistent volumes · NetworkPolicy / CNI · DNS · packet
analysis · Azure AKS · Python automation · SRE-style incident runbooks.

The storage fault is where AKS teaches the most: minikube's hostpath rejects the
claim at the control plane (`StorageClass not found`), while AKS fails at the
real **attach** stage against the Azure API (`FailedAttachVolume`), whose root
cause lives in the `csi-azuredisk-controller` logs — captured by
`aks_collect_evidence.py`.

---
© 2026 David Grunwald.
