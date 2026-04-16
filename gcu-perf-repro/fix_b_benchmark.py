#!/usr/bin/env python3
"""
Fix B benchmark: Share SonicYang instance between validators.

Compares:
  BEFORE: Two independent loadData() calls per move (FullConfigMoveValidator + NoDependencyMoveValidator)
  AFTER:  One shared loadData() call per move (shared SonicYang instance)

Usage inside container:
    python3 fix_b_benchmark.py [--ports N]
"""

import argparse
import copy
import json
import time
import sys
import os

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
            "ingress_lossless_pool": {
                "mode": "dynamic", "size": "12766208", "type": "ingress"
            },
            "egress_lossy_pool": {
                "mode": "dynamic", "size": "7326924", "type": "egress"
            },
            "egress_lossless_pool": {
                "mode": "dynamic", "size": "0", "type": "egress"
            }
        },
        "BUFFER_PROFILE": {
            "ingress_lossy_profile": {
                "pool": "ingress_lossless_pool", "size": "0", "dynamic_th": "3"
            },
            "egress_lossy_profile": {
                "pool": "egress_lossy_pool", "size": "1518", "dynamic_th": "3"
            },
            "egress_lossless_profile": {
                "pool": "egress_lossless_pool", "size": "0", "dynamic_th": "7"
            }
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
            config["BUFFER_PG"][f"{port_name}|{pg}"] = {
                "profile": "ingress_lossy_profile"
            }
        for q in ["0-2", "3-4", "5-6"]:
            config["BUFFER_QUEUE"][f"{port_name}|{q}"] = {
                "profile": "egress_lossy_profile"
            }
        config["CABLE_LENGTH"]["AZURE"][port_name] = "5m"

    return config


def run_benchmark(num_ports, num_validations):
    sys.path.insert(0, '/sonic')
    from sonic_yang import SonicYang

    config = generate_config(num_ports)
    target_config = copy.deepcopy(config)
    target_config.setdefault("POLICER", {})["policer_dscp"] = {
        "meter_type": "bytes", "mode": "sr_tcm",
        "cir": "12500000", "cbs": "12500000",
        "red_packet_action": "drop"
    }

    total_entries = (len(config["PORT"]) + len(config["BUFFER_PG"]) +
                     len(config["BUFFER_QUEUE"]))

    print(f"\n{'='*70}")
    print(f"Fix B Benchmark: {num_ports} ports, {total_entries} entries, "
          f"{num_validations} moves")
    print(f"{'='*70}")

    # ── BEFORE: Two independent loadData calls per move ──────────────
    print(f"\n[BEFORE] Two loadData() calls per move "
          f"(FullConfigMoveValidator + NoDependencyMoveValidator)")

    before_times = []
    for i in range(num_validations):
        test_config = target_config if i % 2 == 0 else config

        # First validator: FullConfigMoveValidator.validate()
        sy1 = SonicYang(YANG_DIR, print_log_enabled=False)
        sy1.loadYangModel()
        start = time.time()
        try:
            sy1.loadData(test_config)
        except Exception:
            pass
        t1 = time.time() - start

        # Second validator: NoDependencyMoveValidator via find_ref_paths()
        sy2 = SonicYang(YANG_DIR, print_log_enabled=False)
        sy2.loadYangModel()
        start = time.time()
        try:
            sy2.loadData(test_config)
        except Exception:
            pass
        t2 = time.time() - start

        before_times.append(t1 + t2)

    before_avg = sum(before_times) / len(before_times)
    before_total = sum(before_times)

    print(f"  Per-move avg: {before_avg:.4f}s "
          f"(2 × loadData + 2 × loadYangModel)")
    print(f"  Total:        {before_total:.2f}s")

    # ── AFTER: One shared loadData call per move ─────────────────────
    print(f"\n[AFTER]  One shared loadData() call per move "
          f"(shared SonicYang instance)")

    # Pre-load YANG models once (shared instance)
    sy_shared = SonicYang(YANG_DIR, print_log_enabled=False)
    sy_shared.loadYangModel()

    after_times = []
    for i in range(num_validations):
        test_config = target_config if i % 2 == 0 else config

        # Single loadData — shared between both validators
        start = time.time()
        try:
            sy_shared.loadData(test_config)
        except Exception:
            pass
        elapsed = time.time() - start

        after_times.append(elapsed)

    after_avg = sum(after_times) / len(after_times)
    after_total = sum(after_times)

    print(f"  Per-move avg: {after_avg:.4f}s "
          f"(1 × loadData, reuse loadYangModel)")
    print(f"  Total:        {after_total:.2f}s")

    # ── Summary ──────────────────────────────────────────────────────
    speedup = before_total / after_total if after_total > 0 else float('inf')
    saved_pct = (1 - after_total / before_total) * 100 if before_total > 0 else 0

    print(f"\n{'─'*70}")
    print(f"  SPEEDUP:  {speedup:.2f}x")
    print(f"  SAVED:    {saved_pct:.0f}%")
    print(f"  BEFORE:   {before_total:.2f}s")
    print(f"  AFTER:    {after_total:.2f}s")
    print(f"  SAVED:    {before_total - after_total:.2f}s")
    print(f"{'─'*70}")

    return before_total, after_total


def main():
    parser = argparse.ArgumentParser(description='Fix B benchmark')
    parser.add_argument('--ports', type=int, default=256,
                        help='Number of ports')
    parser.add_argument('--moves', type=int, default=20,
                        help='Number of simulated moves')
    args = parser.parse_args()

    print("Fix B: Share SonicYang instance between validators")
    print(f"Python: {sys.version}")

    try:
        import yang as ly
        print(f"libyang: loaded")
    except ImportError as e:
        print(f"ERROR: {e}")
        sys.exit(1)

    results = []
    for ports in [64, 128, 256, 512]:
        if ports > args.ports:
            break
        b, a = run_benchmark(ports, args.moves)
        results.append((ports, b, a))

    print(f"\n{'='*70}")
    print(f"SUMMARY — Fix B across port counts ({args.moves} moves each)")
    print(f"{'='*70}")
    print(f"{'Ports':>6} {'Before':>10} {'After':>10} {'Speedup':>8} {'Saved':>8}")
    print(f"{'─'*6} {'─'*10} {'─'*10} {'─'*8} {'─'*8}")
    for ports, b, a in results:
        speedup = b / a if a > 0 else 0
        saved = (1 - a / b) * 100 if b > 0 else 0
        print(f"{ports:>6} {b:>9.2f}s {a:>9.2f}s {speedup:>7.2f}x {saved:>6.0f}%")

    # Project to real scenarios
    if results:
        _, b512, a512 = results[-1]
        scale = b512 / args.moves  # per-move before time
        scale_a = a512 / args.moves  # per-move after time
        print(f"\n  Projected for 1000 moves at {results[-1][0]} ports:")
        print(f"    Before: {1000 * scale:.0f}s ({1000 * scale / 60:.1f} min)")
        print(f"    After:  {1000 * scale_a:.0f}s ({1000 * scale_a / 60:.1f} min)")


if __name__ == '__main__':
    main()
