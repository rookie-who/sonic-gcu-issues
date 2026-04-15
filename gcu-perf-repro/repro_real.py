#!/usr/bin/env python3
"""
GCU performance reproducer using REAL libyang v1 (same as SONiC).

Measures actual parse_data_mem timing with real YANG models and leafref
resolution to prove the O(N²) scaling on high-radix configs.

Usage inside container:
    python3 repro_real.py [--ports N] [--full-sort]
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
                "mode": "dynamic",
                "size": "12766208",
                "type": "ingress"
            },
            "egress_lossy_pool": {
                "mode": "dynamic",
                "size": "7326924",
                "type": "egress"
            },
            "egress_lossless_pool": {
                "mode": "dynamic",
                "size": "0",
                "type": "egress"
            }
        },
        "BUFFER_PROFILE": {
            "ingress_lossy_profile": {
                "pool": "ingress_lossless_pool",
                "size": "0",
                "dynamic_th": "3"
            },
            "egress_lossy_profile": {
                "pool": "egress_lossy_pool",
                "size": "1518",
                "dynamic_th": "3"
            },
            "egress_lossless_profile": {
                "pool": "egress_lossless_pool",
                "size": "0",
                "dynamic_th": "7"
            }
        },
        "BUFFER_PG": {},
        "BUFFER_QUEUE": {},
        "CABLE_LENGTH": {
            "AZURE": {}
        },
        "DEVICE_METADATA": {
            "localhost": {
                "hwsku": "HighRadixSKU",
                "platform": "x86_64-test",
                "mac": "00:11:22:33:44:55",
                "type": "LeafRouter",
                "hostname": "test-switch"
            }
        },
        "ACL_TABLE": {},
        "POLICER": {},
        "MIRROR_SESSION": {},
    }

    for i in range(num_ports):
        port_name = f"Ethernet{i * 4}"
        config["PORT"][port_name] = {
            "admin_status": "up",
            "alias": f"etp{i + 1}",
            "index": str(i),
            "lanes": f"{i * 4},{i * 4 + 1},{i * 4 + 2},{i * 4 + 3}",
            "mtu": "9100",
            "speed": "400000"
        }

        # BUFFER_PG entries referencing PORT via leafref
        for pg in ["0", "3-4"]:
            config["BUFFER_PG"][f"{port_name}|{pg}"] = {
                "profile": "ingress_lossy_profile"
            }

        # BUFFER_QUEUE entries referencing PORT
        for q in ["0-2", "3-4", "5-6"]:
            config["BUFFER_QUEUE"][f"{port_name}|{q}"] = {
                "profile": "egress_lossy_profile"
            }

        config["CABLE_LENGTH"]["AZURE"][port_name] = "5m"

    return config


def test_parse_data_mem_scaling():
    """
    Core test: measure parse_data_mem time for varying config sizes.
    This directly measures the O(N²) leafref resolution cost.
    """
    import yang as ly

    print("=" * 70)
    print("TEST 1: parse_data_mem scaling with config size")
    print("=" * 70)

    # Load YANG models once
    ctx = ly.Context(YANG_DIR)
    yang_files = [f for f in os.listdir(YANG_DIR) if f.endswith('.yang')]
    for yf in sorted(yang_files):
        try:
            ctx.parse_module_path(os.path.join(YANG_DIR, yf), ly.LYS_IN_YANG)
        except Exception as e:
            pass  # Some models may have missing deps, skip

    print(f"Loaded {len(yang_files)} YANG models")

    # Import sonic_yang for proper config translation
    sys.path.insert(0, '/sonic')
    from sonic_yang import SonicYang

    results = []
    for num_ports in [8, 16, 32, 64, 128, 256, 512]:
        config = generate_config(num_ports)

        total_entries = (len(config.get("PORT", {})) +
                        len(config.get("BUFFER_PG", {})) +
                        len(config.get("BUFFER_QUEUE", {})))

        sy = SonicYang(YANG_DIR, print_log_enabled=False)
        sy.loadYangModel()

        # Measure loadData time (which calls parse_data_mem internally)
        times = []
        for trial in range(3):
            start = time.time()
            try:
                sy.loadData(config)
                elapsed = time.time() - start
                times.append(elapsed)
            except Exception as e:
                elapsed = time.time() - start
                times.append(elapsed)
                print(f"  [WARN] ports={num_ports}: loadData failed ({elapsed:.3f}s): {e}")
                break

        avg_time = sum(times) / len(times) if times else 0
        results.append((num_ports, total_entries, avg_time))
        print(f"  ports={num_ports:>4}, entries={total_entries:>6}, "
              f"loadData avg={avg_time:.3f}s (trials: {[f'{t:.3f}' for t in times]})")

        # Stop if getting too slow
        if avg_time > 30:
            print(f"  Stopping scaling test - too slow")
            break

    print(f"\nScaling summary:")
    print(f"{'Ports':>6} {'Entries':>8} {'loadData':>10} {'Ratio':>8}")
    print(f"{'-'*6} {'-'*8} {'-'*10} {'-'*8}")
    base_time = results[0][2] if results else 1
    for ports, entries, t in results:
        ratio = t / base_time if base_time > 0 else 0
        print(f"{ports:>6} {entries:>8} {t:>9.3f}s {ratio:>7.1f}x")


def test_split_vs_combined_parsing():
    """
    Test: parse modules individually vs combined to measure cross-ref overhead.
    """
    import yang as ly

    print("\n" + "=" * 70)
    print("TEST 2: Split vs Combined module parsing")
    print("=" * 70)

    sys.path.insert(0, '/sonic')
    from sonic_yang import SonicYang

    num_ports = 128
    config = generate_config(num_ports)

    # Tables with leafref cross-references
    cross_ref_tables = ["PORT", "BUFFER_PG", "BUFFER_QUEUE"]

    # Full config parse
    sy = SonicYang(YANG_DIR, print_log_enabled=False)
    sy.loadYangModel()

    start = time.time()
    try:
        sy.loadData(config)
    except Exception:
        pass
    combined_time = time.time() - start
    print(f"Combined parse (all tables): {combined_time:.3f}s")

    # Parse each cross-ref table individually
    total_split_time = 0
    for table in cross_ref_tables:
        split_config = {table: config[table]}
        # Include DEVICE_METADATA as it's often required
        if "DEVICE_METADATA" in config:
            split_config["DEVICE_METADATA"] = config["DEVICE_METADATA"]
        # Include referenced tables (profiles, pools)
        if table.startswith("BUFFER"):
            split_config["BUFFER_POOL"] = config.get("BUFFER_POOL", {})
            split_config["BUFFER_PROFILE"] = config.get("BUFFER_PROFILE", {})
        if table == "BUFFER_PG" or table == "BUFFER_QUEUE":
            split_config["PORT"] = config["PORT"]

        sy2 = SonicYang(YANG_DIR, print_log_enabled=False)
        sy2.loadYangModel()
        start = time.time()
        try:
            sy2.loadData(split_config)
        except Exception:
            pass
        split_time = time.time() - start
        total_split_time += split_time
        print(f"  {table}: {split_time:.3f}s")

    print(f"Total split parse: {total_split_time:.3f}s")
    print(f"Cross-ref overhead: {combined_time - total_split_time:.3f}s "
          f"({(combined_time - total_split_time)/combined_time*100:.0f}% of total)")


def test_validation_options():
    """
    Test: LYD_OPT_CONFIG vs LYD_OPT_CONFIG|LYD_OPT_STRICT
    """
    import yang as ly

    print("\n" + "=" * 70)
    print("TEST 3: Impact of LYD_OPT_STRICT on parse time")
    print("=" * 70)

    sys.path.insert(0, '/sonic')
    from sonic_yang import SonicYang
    from json import dumps

    num_ports = 128
    config = generate_config(num_ports)

    sy = SonicYang(YANG_DIR, print_log_enabled=False)
    sy.loadYangModel()

    # Translate config to YANG format
    sy.jIn = copy.deepcopy(config)
    sy.xlateJson = dict()
    sy.tablesWithOutYang = dict()
    sy._cropConfigDB()
    sy._xlateConfigDB()
    yang_json_str = dumps(sy.xlateJson)

    # Test with CONFIG|STRICT (default - what GCU uses)
    start = time.time()
    try:
        root = sy.ctx.parse_data_mem(yang_json_str,
                                      ly.LYD_JSON,
                                      ly.LYD_OPT_CONFIG | ly.LYD_OPT_STRICT)
    except Exception as e:
        print(f"  CONFIG|STRICT failed: {e}")
    strict_time = time.time() - start
    print(f"LYD_OPT_CONFIG | LYD_OPT_STRICT: {strict_time:.3f}s")

    # Test with CONFIG only (no strict leafref validation)
    start = time.time()
    try:
        root = sy.ctx.parse_data_mem(yang_json_str,
                                      ly.LYD_JSON,
                                      ly.LYD_OPT_CONFIG)
    except Exception as e:
        print(f"  CONFIG only failed: {e}")
    config_time = time.time() - start
    print(f"LYD_OPT_CONFIG only:              {config_time:.3f}s")

    if strict_time > 0 and config_time > 0:
        print(f"Speedup from dropping STRICT:     {strict_time/config_time:.1f}x")


def test_sort_step_simulation():
    """
    Simulate the GCU sort step with real libyang validation.
    This is the closest to what actually happens on a device.
    """
    import yang as ly

    print("\n" + "=" * 70)
    print("TEST 4: Simulated sort step with real YANG validation")
    print("=" * 70)

    sys.path.insert(0, '/sonic')
    from sonic_yang import SonicYang

    num_ports = 64  # Start small to be practical
    config = generate_config(num_ports)

    # The patch: add a policer
    patch = {
        "POLICER": {
            "policer_dscp": {
                "meter_type": "bytes",
                "mode": "sr_tcm",
                "cir": "12500000",
                "cbs": "12500000",
                "red_packet_action": "drop"
            }
        }
    }

    target_config = copy.deepcopy(config)
    target_config["POLICER"] = patch["POLICER"]

    sy = SonicYang(YANG_DIR, print_log_enabled=False)
    sy.loadYangModel()

    # Simulate N validation calls (like the sort step would do)
    num_simulated_validations = 20
    print(f"Running {num_simulated_validations} simulated validation calls "
          f"(like sort step with {num_ports} ports)...")

    times = []
    for i in range(num_simulated_validations):
        # Alternate between current and target config (simulates different moves)
        test_config = target_config if i % 2 == 0 else config

        start = time.time()
        try:
            sy.loadData(test_config)
        except Exception:
            pass
        elapsed = time.time() - start
        times.append(elapsed)

    avg = sum(times) / len(times)
    total = sum(times)
    print(f"  Average loadData time: {avg:.3f}s")
    print(f"  Total for {num_simulated_validations} validations: {total:.1f}s")
    print(f"\n  Projected for real sort scenarios:")

    for label, num_vals in [("Simple patch (24 validations)", 24),
                            ("Medium patch (100 validations)", 100),
                            ("Complex patch (1000 validations)", 1000)]:
        projected = num_vals * avg
        print(f"    {label}: {projected:.0f}s ({projected/60:.1f} min)")

    # Now project for larger port counts using the scaling ratio
    print(f"\n  Projected across port counts (100 validations):")
    for ports in [64, 128, 256, 512]:
        entries = ports * 6  # PORT + BUFFER_PG*2 + BUFFER_QUEUE*3
        # O(N²) model: time ∝ entries²
        base_entries = num_ports * 6
        scale = (entries / base_entries) ** 2
        projected_per_val = avg * scale
        projected_total = 100 * projected_per_val
        print(f"    {ports} ports: ~{projected_per_val:.1f}s/validation, "
              f"~{projected_total:.0f}s ({projected_total/60:.1f} min) total")


def main():
    parser = argparse.ArgumentParser(description='GCU perf repro with real libyang')
    parser.add_argument('--ports', type=int, default=128,
                        help='Number of ports for scaling test')
    parser.add_argument('--test', choices=['all', 'scaling', 'split', 'options', 'sort'],
                        default='all', help='Which test to run')
    args = parser.parse_args()

    print(f"GCU Performance Reproducer - Real libyang v1.0-r4")
    print(f"Python: {sys.version}")

    # Verify libyang is available
    try:
        import yang as ly
        print(f"libyang loaded successfully")
    except ImportError as e:
        print(f"ERROR: Cannot import libyang: {e}")
        print("Run this inside the Docker container!")
        sys.exit(1)

    if args.test in ('all', 'scaling'):
        test_parse_data_mem_scaling()

    if args.test in ('all', 'split'):
        test_split_vs_combined_parsing()

    if args.test in ('all', 'options'):
        test_validation_options()

    if args.test in ('all', 'sort'):
        test_sort_step_simulation()

    print("\n" + "=" * 70)
    print("DONE")
    print("=" * 70)


if __name__ == '__main__':
    main()
