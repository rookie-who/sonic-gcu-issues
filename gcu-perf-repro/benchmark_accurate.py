#!/usr/bin/env python3
"""
Accurate GCU sort benchmark — mirrors the REAL code path exactly.

Real code path per MoveWrapper.validate(move, diff):
  1. simulated_config = move.apply(diff.current_config)  # new object via jsonpatch
  2. FullConfigMoveValidator.validate(simulated_config)
       -> validate_config_db_config(simulated_config)
       -> sy.loadData(simulated_config)                  # loadData #1
  3. NoDependencyMoveValidator.validate(move, diff, simulated_config)
     For ADD:
       -> find_ref_paths([path], simulated_config)
       -> sy.loadData(simulated_config)                  # loadData #2
     For REMOVE:
       -> find_ref_paths([path], diff.current_config)
       -> sy.loadData(diff.current_config)               # loadData #2
     For REPLACE:
       -> find_ref_paths(deleted, diff.current_config)
       -> sy.loadData(diff.current_config)               # loadData #2
       -> find_ref_paths(added, simulated_config)
       -> sy.loadData(simulated_config)                   # loadData #3
  4. RemoveCreateOnlyDependencyMoveValidator.validate(...)
       -> find_ref_paths(member_path, simulated_config, reload_config=True)  # first member
       -> sy.loadData(simulated_config)                   # loadData #3 or #4
       (reload_config=False for subsequent members)

Key facts:
  - ALL validators share ONE SonicYang instance (singleton via create_sonic_yang_with_loaded_models)
  - loadYangModel() is called ONCE at init, NOT per move
  - simulated_config is a NEW object each move (from jsonpatch.apply deepcopy)
  - diff.current_config is the SAME object across all moves in one DFS level

Usage inside container:
    python3 benchmark_accurate.py [--ports 256] [--moves 20]
"""

import argparse
import copy
import hashlib
import json
import time
import sys

YANG_DIR = "/sonic/yang-models"


def generate_config(num_ports):
    """Generate a realistic high-radix SONiC config."""
    config = {
        "PORT": {},
        "LOOPBACK_INTERFACE": {
            "Loopback0": {},
            "Loopback0|10.1.0.1/32": {}
        },
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
            "localhost": {
                "hwsku": "HighRadixSKU", "platform": "x86_64-test",
                "mac": "00:11:22:33:44:55", "type": "LeafRouter",
                "hostname": "test-switch"
            }
        },
    }
    for i in range(num_ports):
        port_name = f"Ethernet{i * 4}"
        config["PORT"][port_name] = {
            "admin_status": "up", "alias": f"etp{i + 1}",
            "index": str(i),
            "lanes": f"{i * 4},{i * 4 + 1},{i * 4 + 2},{i * 4 + 3}",
            "mtu": "9100", "speed": "400000"
        }
        for pg in ["0", "3-4"]:
            config["BUFFER_PG"][f"{port_name}|{pg}"] = {"profile": "ingress_lossy_profile"}
        for q in ["0-2", "3-4", "5-6"]:
            config["BUFFER_QUEUE"][f"{port_name}|{q}"] = {"profile": "egress_lossy_profile"}
        config["CABLE_LENGTH"]["AZURE"][port_name] = "5m"
    return config


def make_simulated_config(base_config, move_index):
    """Simulate what jsonpatch.apply() does: deepcopy + small modification."""
    sim = copy.deepcopy(base_config)
    # Each move produces a slightly different simulated config
    sim.setdefault("POLICER", {})
    sim["POLICER"][f"policer_{move_index}"] = {
        "meter_type": "bytes", "mode": "sr_tcm",
        "cir": "12500000", "cbs": "12500000",
        "red_packet_action": "drop"
    }
    return sim


def compute_hash(config):
    return hashlib.md5(json.dumps(config, sort_keys=True).encode()).hexdigest()


def run_scenario(label, num_ports, num_moves, op_type, sy, use_cache=False):
    """
    Simulate the loadData call pattern for a given operation type.
    Returns total time spent in loadData calls.
    """
    current_config = generate_config(num_ports)
    total_time = 0.0
    load_count = 0
    skip_count = 0
    cached_hash = None

    def do_load(config, reason):
        nonlocal total_time, load_count, skip_count, cached_hash
        if use_cache:
            h = compute_hash(config)
            if h == cached_hash:
                skip_count += 1
                return
            cached_hash = h

        start = time.time()
        try:
            sy.loadData(config)
        except Exception:
            pass
        elapsed = time.time() - start
        total_time += elapsed
        load_count += 1
        if use_cache:
            cached_hash = compute_hash(config)

    for i in range(num_moves):
        simulated_config = make_simulated_config(current_config, i)

        # 1. FullConfigMoveValidator -> validate_config_db_config(simulated_config)
        do_load(simulated_config, "FullConfigMoveValidator")

        # 2. NoDependencyMoveValidator
        if op_type == "ADD":
            do_load(simulated_config, "NoDep:simulated")
        elif op_type == "REMOVE":
            do_load(current_config, "NoDep:current")
        elif op_type == "REPLACE":
            do_load(current_config, "NoDep:current")
            do_load(simulated_config, "NoDep:simulated")

        # 3. RemoveCreateOnlyDependencyMoveValidator (first member, reload_config=True)
        do_load(simulated_config, "RemoveCreateOnlyDep:simulated")

    return total_time, load_count, skip_count


def main():
    parser = argparse.ArgumentParser(description='Accurate GCU sort benchmark')
    parser.add_argument('--ports', type=int, default=256)
    parser.add_argument('--moves', type=int, default=10)
    args = parser.parse_args()

    sys.path.insert(0, '/sonic')
    from sonic_yang import SonicYang

    print("=" * 75)
    print("GCU Sort Benchmark — Mirrors Real Code Path")
    print("=" * 75)
    print(f"Python: {sys.version}")
    print(f"One shared SonicYang instance (singleton), loadYangModel() once")
    print()

    # Single shared SonicYang instance — exactly like production
    sy = SonicYang(YANG_DIR, print_log_enabled=False)
    sy.loadYangModel()

    port_counts = [p for p in [64, 128, 256, 512] if p <= args.ports]

    for op_type in ["ADD", "REMOVE", "REPLACE"]:
        print(f"\n{'=' * 75}")
        print(f"Operation: {op_type}")
        expected = {"ADD": 3, "REMOVE": 3, "REPLACE": 4}[op_type]
        cached_expected = {"ADD": 1, "REMOVE": 3, "REPLACE": 3}[op_type]
        print(f"loadData calls per move: {expected} (no cache) -> {cached_expected} (hash cache)")
        print(f"{'=' * 75}")
        print(f"{'Ports':>6} | {'No Cache':>12} {'loads':>6} | {'Hash Cache':>12} {'loads':>6} {'skips':>6} | {'Speedup':>8} {'Saved':>8}")
        print(f"{'─' * 6}-+-{'─' * 12}-{'─' * 6}-+-{'─' * 12}-{'─' * 6}-{'─' * 6}-+-{'─' * 8}-{'─' * 8}")

        for num_ports in port_counts:
            # Baseline: no cache
            t_base, loads_base, _ = run_scenario(
                "baseline", num_ports, args.moves, op_type, sy, use_cache=False)

            # With hash cache
            t_cached, loads_cached, skips = run_scenario(
                "cached", num_ports, args.moves, op_type, sy, use_cache=True)

            speedup = t_base / t_cached if t_cached > 0 else float('inf')
            saved_pct = (1 - t_cached / t_base) * 100 if t_base > 0 else 0

            print(f"{num_ports:>6} | {t_base:>10.2f}s {loads_base:>6} | "
                  f"{t_cached:>10.2f}s {loads_cached:>6} {skips:>6} | "
                  f"{speedup:>7.2f}x {saved_pct:>6.0f}%")

    # Project to real scenario
    print(f"\n{'=' * 75}")
    print(f"PROJECTION: 100 moves at {port_counts[-1]} ports")
    print(f"{'=' * 75}")
    for op_type in ["ADD", "REMOVE", "REPLACE"]:
        t_base, _, _ = run_scenario("proj_base", port_counts[-1], 5, op_type, sy, use_cache=False)
        t_cached, _, _ = run_scenario("proj_cache", port_counts[-1], 5, op_type, sy, use_cache=True)
        per_move_base = t_base / 5
        per_move_cached = t_cached / 5
        print(f"  {op_type:>8}: {100 * per_move_base:>6.0f}s -> {100 * per_move_cached:>6.0f}s "
              f"(save {100 * (per_move_base - per_move_cached):>5.0f}s)")


if __name__ == '__main__':
    main()
