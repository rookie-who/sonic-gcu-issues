#!/usr/bin/env python3
"""
Benchmark PR #4476 changes in isolation.

Simulates the exact caching strategy from PR #4476:
1. Result cache in validate_config_db_config (skip loadData on same config)
2. Shared sy-object cache in find_ref_paths (reuse sy loaded by validate_config_db_config)

Tests REMOVE operation (the PR's target use case: 512 REMOVE moves from ACL_TABLE).
"""

import copy
import hashlib
import json
import time
import sys
import argparse

YANG_DIR = "/sonic/yang-models"


def generate_config(num_ports):
    config = {
        "PORT": {},
        "LOOPBACK_INTERFACE": {"Loopback0": {}, "Loopback0|10.1.0.1/32": {}},
        "BUFFER_POOL": {
            "ingress_lossless_pool": {"mode": "dynamic", "size": "12766208", "type": "ingress"},
            "egress_lossy_pool": {"mode": "dynamic", "size": "7326924", "type": "egress"},
            "egress_lossless_pool": {"mode": "dynamic", "size": "0", "type": "egress"}
        },
        "BUFFER_PROFILE": {
            "ingress_lossy_profile": {"pool": "ingress_lossless_pool", "size": "0", "dynamic_th": "3"},
            "egress_lossy_profile": {"pool": "egress_lossy_pool", "size": "1518", "dynamic_th": "3"},
            "egress_lossless_profile": {"pool": "egress_lossless_pool", "size": "0", "dynamic_th": "7"}
        },
        "BUFFER_PG": {},
        "BUFFER_QUEUE": {},
        "CABLE_LENGTH": {"AZURE": {}},
        "DEVICE_METADATA": {
            "localhost": {"hwsku": "HighRadixSKU", "platform": "x86_64-test",
                          "mac": "00:11:22:33:44:55", "type": "LeafRouter", "hostname": "test-switch"}
        },
        "ACL_TABLE": {
            "EVERFLOW": {
                "type": "MIRROR",
                "policy_desc": "EVERFLOW",
                "ports": []
            }
        },
    }
    port_names = []
    for i in range(num_ports):
        p = f"Ethernet{i * 4}"
        config["PORT"][p] = {"admin_status": "up", "alias": f"etp{i+1}", "index": str(i),
                             "lanes": f"{i*4},{i*4+1},{i*4+2},{i*4+3}", "mtu": "9100", "speed": "400000"}
        for pg in ["0", "3-4"]:
            config["BUFFER_PG"][f"{p}|{pg}"] = {"profile": "ingress_lossy_profile"}
        for q in ["0-2", "3-4", "5-6"]:
            config["BUFFER_QUEUE"][f"{p}|{q}"] = {"profile": "egress_lossy_profile"}
        config["CABLE_LENGTH"]["AZURE"][p] = "5m"
        port_names.append(p)
    config["ACL_TABLE"]["EVERFLOW"]["ports"] = port_names
    return config


def compute_hash(config):
    return hashlib.md5(json.dumps(config, sort_keys=True).encode()).hexdigest()


def run_baseline(num_ports, num_moves, sy):
    """No cache - mirrors current code."""
    current_config = generate_config(num_ports)
    total_time = 0.0
    load_count = 0

    def do_load(config):
        nonlocal total_time, load_count
        start = time.time()
        try:
            sy.loadData(config)
        except Exception:
            pass
        total_time += time.time() - start
        load_count += 1

    for i in range(num_moves):
        # Simulate DFS: each accepted move changes current_config
        # simulated = current minus one port from ACL
        simulated = copy.deepcopy(current_config)
        if len(simulated["ACL_TABLE"]["EVERFLOW"]["ports"]) > 0:
            simulated["ACL_TABLE"]["EVERFLOW"]["ports"] = simulated["ACL_TABLE"]["EVERFLOW"]["ports"][1:]

        # REMOVE move validation:
        # 1. FullConfigMoveValidator: loadData(simulated)
        do_load(simulated)
        # 2. NoDependencyMoveValidator: find_ref_paths(path, current_config)
        do_load(current_config)

        # Move accepted -> advance DFS (current_config becomes simulated)
        current_config = simulated

    return total_time, load_count


def run_pr4476(num_ports, num_moves, sy):
    """PR #4476 caching strategy."""
    current_config = generate_config(num_ports)
    total_time = 0.0
    load_count = 0
    skip_count = 0

    # PR #4476 caches
    validate_config_cache = {}  # hash -> (bool, error)
    sy_loaded_cache = {}        # hash -> sy (but it's the singleton!)
    sy_find_cache = {}          # hash -> sy (find_ref_paths local cache)

    def do_load(config, tag):
        nonlocal total_time, load_count
        start = time.time()
        try:
            sy.loadData(config)
        except Exception:
            pass
        total_time += time.time() - start
        load_count += 1

    for i in range(num_moves):
        simulated = copy.deepcopy(current_config)
        if len(simulated["ACL_TABLE"]["EVERFLOW"]["ports"]) > 0:
            simulated["ACL_TABLE"]["EVERFLOW"]["ports"] = simulated["ACL_TABLE"]["EVERFLOW"]["ports"][1:]

        sim_hash = compute_hash(simulated)
        cur_hash = compute_hash(current_config)

        # 1. FullConfigMoveValidator -> validate_config_db_config(simulated)
        if sim_hash in validate_config_cache:
            skip_count += 1
            # No loadData! But sy_loaded_cache[sim_hash] still points to singleton
            # whose data may be stale
        else:
            do_load(simulated, "FullConfig:simulated")
            validate_config_cache[sim_hash] = (True, None)
            sy_loaded_cache[sim_hash] = sy  # cache the singleton ref

        # 2. NoDependencyMoveValidator -> find_ref_paths(path, current_config)
        if cur_hash in sy_find_cache:
            skip_count += 1
            # Reuse cached sy — BUT it's the singleton, data may be wrong
        elif cur_hash in sy_loaded_cache:
            skip_count += 1
            sy_find_cache[cur_hash] = sy_loaded_cache[cur_hash]
            # Cache hit from validate_config_db_config — no loadData
        else:
            do_load(current_config, "NoDep:current")
            sy_find_cache[cur_hash] = sy

        # Move accepted -> advance DFS
        current_config = simulated

    return total_time, load_count, skip_count


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--ports', type=int, default=256)
    parser.add_argument('--moves', type=int, default=20)
    args = parser.parse_args()

    sys.path.insert(0, '/sonic')
    from sonic_yang import SonicYang

    sy = SonicYang(YANG_DIR, print_log_enabled=False)
    sy.loadYangModel()

    print("=" * 90)
    print("PR #4476 Benchmark — REMOVE moves (ACL port removal)")
    print("=" * 90)
    print(f"Shared SonicYang singleton, {args.moves} moves per test")
    print(f"Scenario: Remove ports one-by-one from ACL_TABLE/EVERFLOW/ports\n")

    port_counts = [p for p in [64, 128, 256, 512] if p <= args.ports]

    print(f"{'Ports':>6} | {'Baseline':>12} {'loads':>6} | "
          f"{'PR #4476':>12} {'loads':>6} {'skips':>6} | {'Speedup':>8} {'Saved':>8}")
    print("-" * 90)

    for num_ports in port_counts:
        moves = min(args.moves, num_ports)

        t1, l1 = run_baseline(num_ports, moves, sy)
        t2, l2, s2 = run_pr4476(num_ports, moves, sy)

        sp = t1 / t2 if t2 > 0 else float('inf')
        saved = (1 - t2/t1) * 100 if t1 > 0 else 0
        print(f"{num_ports:>6} | {t1:>10.2f}s {l1:>6} | {t2:>10.2f}s {l2:>6} {s2:>6} | {sp:>7.2f}x {saved:>6.0f}%")

    # Projection
    if port_counts:
        p = port_counts[-1]
        m = min(args.moves, p)
        t1, l1 = run_baseline(p, 5, sy)
        t2, l2, _ = run_pr4476(p, 5, sy)
        print(f"\nProjection for 100 REMOVE moves at {p} ports:")
        print(f"  Baseline:  {100*t1/5:>6.0f}s ({100*l1//5} loadData calls)")
        print(f"  PR #4476:  {100*t2/5:>6.0f}s ({100*l2//5} loadData calls)")


if __name__ == '__main__':
    main()
