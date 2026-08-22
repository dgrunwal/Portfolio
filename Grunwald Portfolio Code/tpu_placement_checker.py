#!/usr/bin/env python3
"""
tpu_placement_checker.py
------------------------
An automated sanity-checker for the "getting the right jobs onto the right
chips is a packing problem" issue from the TPU/GPU scheduling puzzle.

This code is a teaching tool that simulates how AI training/serving jobs get placed onto TPU/GPU hardware, 
and specifically demonstrates a common scheduling failure: having enough free chips but not enough adjacent ones.

It answers two questions before you ever apply a manifest:

  1. GPU / memory-ceiling check  -- can each single-chip job actually fit on
     a device whose HBM ceiling is >= the job's requested memory?
  2. TPU rectangle / topology check -- can a gang job's requested topology
     (e.g. 2x2, 2x4) be placed as a CONTIGUOUS rectangle on the mesh, given
     what the serving jobs already occupy?

This is a teaching model, not a production scheduler. It reproduces the
*specific* failure the puzzle is about: free chips that are not adjacent.

Simplifications (deliberate):
  * The mesh is treated as a PLAIN grid. Real TPU v5e/v6e slices are a 2-D
    torus (edge chips wrap to the opposite edge), and v4/v5p/Ironwood are
    3-D tori. A block spanning an edge can be contiguous on real hardware
    but is missed here. This only matters on slices larger than one host.
  * The "Warning FailedScheduling ... no placement" wording below is the
    puzzle simulator's stylised string, NOT a literal Kubernetes message.
    Real GKE output counts nodes and names the reason, e.g.:
    "0/5 nodes are available: 3 Insufficient google.com/tpu."

Docs:
  TPU v5e architecture & topologies : https://docs.cloud.google.com/tpu/docs/v5e
  About TPUs in GKE                 : https://docs.cloud.google.com/kubernetes-engine/docs/concepts/tpus
  Gang scheduling (multislice)      : https://cloud.google.com/kubernetes-engine/docs/tutorials/tpu-multislice-kueue
  Spot VMs                          : https://cloud.google.com/compute/docs/instances/spot
"""

from dataclasses import dataclass, field
from itertools import product
from typing import Optional


# --------------------------------------------------------------------------
# 1. THE MESH  (a single TPU host, e.g. ct5lp-hightpu-8t = 8 chips as 2x4)
# --------------------------------------------------------------------------
@dataclass
class Mesh:
    rows: int
    cols: int
    # occupied[(r, c)] = name of the job holding that chip, or None if free
    occupied: dict = field(default_factory=dict)

    def __post_init__(self):
        for r, c in product(range(self.rows), range(self.cols)):
            self.occupied.setdefault((r, c), None)

    def free_cells(self):
        return [rc for rc, who in self.occupied.items() if who is None]

    def occupy(self, cells, name):
        for rc in cells:
            self.occupied[rc] = name

    def free_count(self):
        return len(self.free_cells())

    def render(self):
        """ASCII picture of the mesh. '.' = free, other = first letter of job."""
        out = []
        for r in range(self.rows):
            row = []
            for c in range(self.cols):
                who = self.occupied[(r, c)]
                row.append("." if who is None else who[0].upper())
            out.append(" ".join(row))
        return "\n".join(out)


def find_rectangle(mesh: Mesh, h: int, w: int) -> Optional[list]:
    """
    Return the cells of the first free h x w (or w x h) rectangle, or None.
    This is the crux: a gang job needs contiguous chips, not just enough chips.
    """
    for (rr, cc) in ((h, w), (w, h)):          # try both orientations
        for r0 in range(mesh.rows - rr + 1):
            for c0 in range(mesh.cols - cc + 1):
                block = [(r0 + dr, c0 + dc)
                         for dr in range(rr) for dc in range(cc)]
                if all(mesh.occupied[rc] is None for rc in block):
                    return block
    return None


def parse_topology(topo: str):
    """'2x2' -> (2, 2). Also accepts 3-tuples like '2x2x1' (product of dims)."""
    dims = [int(x) for x in topo.lower().split("x")]
    if len(dims) == 2:
        return dims[0], dims[1]
    # collapse a 3-tuple to a 2-D footprint for this single-host teaching model
    area = 1
    for d in dims:
        area *= d
    # represent as 1 x area (caller can override for real 3-D meshes)
    return 1, area


# --------------------------------------------------------------------------
# 2. THE DEVICES  (GPUs with individual HBM ceilings)
# --------------------------------------------------------------------------
@dataclass
class GpuDevice:
    name: str
    hbm_gib: int                 # memory ceiling of THIS device
    holder: Optional[str] = None # guaranteed job currently on it
    spot_holder: Optional[str] = None  # spot job (reclaimable)


def place_gpu_job(devices, req_mem_gib, job_name, is_spot=False):
    """
    Fit a single-chip GPU job onto the first device whose ceiling can hold it.
    A guaranteed job may reclaim a device from a spot holder.
    Returns the device name it landed on, or None (== FailedScheduling).
    """
    for d in devices:
        if d.hbm_gib < req_mem_gib:
            continue                      # memory ceiling turns it away
        if d.holder is None:
            if is_spot and d.spot_holder is None:
                d.spot_holder = job_name
                return d.name
            if not is_spot:
                if d.spot_holder is not None:
                    d.spot_holder = None  # reclaim spot capacity
                d.holder = job_name
                return d.name
    return None


# --------------------------------------------------------------------------
# 3. THE CHECKER
# --------------------------------------------------------------------------
def check_gang_job(mesh: Mesh, topology: str, name: str):
    h, w = parse_topology(topology)
    need = h * w
    have = mesh.free_count()
    block = find_rectangle(mesh, h, w)
    ok = block is not None
    reason = ""
    if not ok:
        if have < need:
            reason = f"only {have} free chips, need {need}"
        else:
            reason = (f"{have} free chips but no contiguous {h}x{w} rectangle "
                      f"-- the slice is fragmented")
    return {"ok": ok, "job": name, "need": need, "have": have,
            "block": block, "reason": reason}


def demo():
    """
    Reproduce the Level-2 style failure: enough free chips, no rectangle.
    Host: ct5lp-hightpu-8t modelled as a 2x4 mesh (8 chips).
    """
    mesh = Mesh(2, 4)
    # Scatter three 1-chip serving jobs so they FRAGMENT the slice:
    mesh.occupy([(0, 0)], "serve-embed")
    mesh.occupy([(0, 2)], "serve-route")
    mesh.occupy([(1, 1)], "serve-rank")

    print("Mesh after serving jobs (scattered):")
    print(mesh.render())
    print(f"\nFree chips: {mesh.free_count()} of 8\n")

    result = check_gang_job(mesh, "2x2", "train-llm")
    if result["ok"]:
        print(f"OK  train-llm can take rectangle {result['block']}")
    else:
        print(f"FAIL train-llm: {result['reason']}")
        print("     -> (simulator) Warning FailedScheduling: no placement.")

    # Now show the FIX: seat the same three jobs on one edge, keeping a block.
    print("\n" + "-" * 52)
    mesh2 = Mesh(2, 4)
    mesh2.occupy([(0, 3)], "serve-embed")
    mesh2.occupy([(1, 3)], "serve-route")
    mesh2.occupy([(0, 2)], "serve-rank")   # all pushed to the right columns
    print("Mesh after serving jobs (packed to one side):")
    print(mesh2.render())
    result2 = check_gang_job(mesh2, "2x2", "train-llm")
    print()
    if result2["ok"]:
        print(f"OK  train-llm takes contiguous rectangle {result2['block']}")
    else:
        print(f"FAIL train-llm: {result2['reason']}")


if __name__ == "__main__":
    demo()
