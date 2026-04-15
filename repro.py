#!/usr/bin/env python3
"""
GCU apply-patch sort step performance reproducer.

Simulates the patch_sorter behavior on high-radix configs without needing
a SONiC device. We mock the YANG validation to measure:
  1. How many moves are generated for a given config size
  2. How many validations are performed
  3. How the sort time scales with port count

Usage:
    python3 repro.py [--ports N] [--validation-delay SECONDS]

The --validation-delay flag simulates the time libyang's parse_data_mem
takes per validation call. On real high-radix systems this is 3-10s.
"""

import argparse
import copy
import json
import time
import sys
import os

# Add sonic-utilities to path
SONIC_UTILS_DIR = os.path.join(os.path.dirname(__file__), '..', 'sonic-utilities.msft-202412')
sys.path.insert(0, SONIC_UTILS_DIR)

# We need to mock sonic_yang and related modules before importing patch_sorter
import unittest.mock as mock

# Create mock modules
mock_sonic_yang = mock.MagicMock()
mock_sonic_yang.SonicYang = mock.MagicMock()

class MockSonicYangException(Exception):
    pass

mock_sonic_yang.SonicYangException = MockSonicYangException
mock_sonic_yang.SonicYang.configdb_path_split = lambda path: [t for t in path.split('/') if t]
mock_sonic_yang.SonicYang.configdb_path_join = lambda tokens: '/' + '/'.join(tokens)

sys.modules['sonic_yang'] = mock_sonic_yang
sys.modules['sonic_yang_ext'] = mock.MagicMock()
sys.modules['sonic_yang_ext'].SonicYangException = MockSonicYangException
sys.modules['sonic_py_common'] = mock.MagicMock()
sys.modules['sonic_py_common.multi_asic'] = mock.MagicMock()
sys.modules['sonic_py_common.multi_asic'].DEFAULT_NAMESPACE = ''
sys.modules['swsscommon'] = mock.MagicMock()
sys.modules['swsscommon.swsscommon'] = mock.MagicMock()
sys.modules['sonic_py_common.device_info'] = mock.MagicMock()
sys.modules['utilities_common'] = mock.MagicMock()


def generate_high_radix_config(num_ports=256):
    """Generate a realistic high-radix SONiC config with the tables that cause issues."""
    config = {
        "PORT": {},
        "BUFFER_PG": {},
        "BUFFER_QUEUE": {},
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
            }
        },
        "QUEUE": {},
        "ACL_TABLE": {
            "EVERFLOW": {
                "type": "MIRROR",
                "policy_desc": "EVERFLOW",
                "ports": []
            },
            "EVERFLOW_DSCP": {
                "type": "MIRROR_DSCP",
                "policy_desc": "EVERFLOW_DSCP",
                "ports": []
            }
        },
        "ACL_RULE": {},
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

        # Buffer PG entries (typically 2-3 per port)
        for pg in ["0", "3-4"]:
            config["BUFFER_PG"][f"{port_name}|{pg}"] = {
                "profile": "ingress_lossy_profile"
            }

        # Buffer queue entries (typically 8 per port)
        for q in range(8):
            config["BUFFER_QUEUE"][f"{port_name}|{q}"] = {
                "profile": "egress_lossy_profile"
            }

        # Queue entries
        for q in range(8):
            config["QUEUE"][f"{port_name}|{q}"] = {
                "scheduler": f"scheduler.{q}"
            }

        # ACL ports
        config["ACL_TABLE"]["EVERFLOW"]["ports"].append(port_name)
        config["ACL_TABLE"]["EVERFLOW_DSCP"]["ports"].append(port_name)

    return config


def generate_patch(config, num_ports):
    """Generate a patch that simulates adding monitor config (like the GCU test)."""
    # Simulate the test_monitor_config patch: add POLICER + MIRROR_SESSION + ACL entries
    patch = [
        {
            "op": "add",
            "path": "/POLICER/policer_dscp",
            "value": {
                "meter_type": "bytes",
                "mode": "sr_tcm",
                "cir": "12500000",
                "cbs": "12500000",
                "red_packet_action": "drop"
            }
        },
        {
            "op": "add",
            "path": "/MIRROR_SESSION/mirror_dscp",
            "value": {
                "dscp": "8",
                "dst_ip": "2.2.2.2",
                "gre_type": "0x6558",
                "policer": "policer_dscp",
                "queue": "0",
                "src_ip": "1.1.1.1",
                "ttl": "32",
                "type": "ERSPAN"
            }
        },
        {
            "op": "add",
            "path": "/ACL_TABLE/EVERFLOW_DSCP",
            "value": {
                "type": "MIRROR_DSCP",
                "policy_desc": "EVERFLOW_DSCP",
                "ports": [f"Ethernet{i * 4}" for i in range(num_ports)]
            }
        },
        {
            "op": "add",
            "path": "/ACL_RULE/EVERFLOW_DSCP|RULE_1",
            "value": {
                "DSCP": "8",
                "MIRROR_ACTION": "mirror_dscp",
                "PRIORITY": "9999"
            }
        }
    ]
    return patch


class ValidationCounter:
    """Wraps validation to count calls and optionally add delay."""
    def __init__(self, delay=0.0):
        self.count = 0
        self.delay = delay
        self.total_time = 0.0

    def validate(self, config):
        self.count += 1
        if self.delay > 0:
            time.sleep(self.delay)
            self.total_time += self.delay
        return True, None


class MoveCounter:
    """Counts moves generated and validated."""
    def __init__(self):
        self.generated = 0
        self.validated = 0
        self.accepted = 0


def run_sort_simulation(num_ports, validation_delay=0.0):
    """
    Simulate the DFS sort algorithm with move generation and validation counting.
    This doesn't use the actual patch_sorter (which needs sonic_yang),
    but replicates the algorithmic behavior.
    """
    import jsonpatch

    config = generate_high_radix_config(num_ports)
    patch_ops = generate_patch(config, num_ports)
    patch = jsonpatch.JsonPatch(patch_ops)

    target_config = patch.apply(copy.deepcopy(config))

    counter = MoveCounter()
    validator = ValidationCounter(delay=validation_delay)

    # Count the diff entries (this is what generates moves)
    diff_tables = set()
    for op in patch_ops:
        table = op["path"].split("/")[1]
        diff_tables.add(table)

    # Estimate moves: each diff entry generates low-level moves
    # For each table entry change, we get ADD/REMOVE/REPLACE moves
    # Plus extended moves (parent replace, delete-instead-of-replace, etc.)
    num_diff_entries = len(patch_ops)

    # Count actual entries in config that would generate moves
    total_entries = sum(
        len(v) if isinstance(v, dict) else 1
        for v in config.values()
    )

    # Estimate the number of leaf-level diffs
    leaf_diffs = 0
    for op in patch_ops:
        if isinstance(op.get("value"), dict):
            leaf_diffs += len(op["value"])
        else:
            leaf_diffs += 1

    print(f"\n{'='*60}")
    print(f"GCU Sort Performance Simulation")
    print(f"{'='*60}")
    print(f"Ports:              {num_ports}")
    print(f"PORT entries:       {len(config['PORT'])}")
    print(f"BUFFER_PG entries:  {len(config['BUFFER_PG'])}")
    print(f"BUFFER_QUEUE:       {len(config['BUFFER_QUEUE'])}")
    print(f"QUEUE entries:      {len(config['QUEUE'])}")
    print(f"Total config entries: {total_entries}")
    print(f"Patch operations:   {len(patch_ops)}")
    print(f"Leaf-level diffs:   {leaf_diffs}")
    print(f"Validation delay:   {validation_delay}s per call")
    print(f"{'='*60}")

    # Simulate move generation counts based on actual generator behavior:
    #
    # Non-extendable generators (run first):
    #   BulkKeyLevelMoveGenerator: groups keys by table, generates bulk moves
    #   KeyLevelMoveGenerator: one move per key-level diff
    #   BulkKeyGroupLowLevelMoveGenerator: groups low-level by key
    #   BulkLowLevelMoveGenerator: groups low-level moves together
    #
    # Extendable generators:
    #   RemoveCreateOnlyDependencyMoveGenerator
    #   LowLevelMoveGenerator: one move per leaf diff
    #
    # Extenders (applied to each extendable move):
    #   RequiredValueMoveExtender
    #   UpperLevelMoveExtender: tries parent path
    #   DeleteInsteadOfReplaceMoveExtender
    #   DeleteRefsMoveExtender

    # Key-level moves (one per table entry in diff)
    key_level_moves = num_diff_entries
    # Low-level moves (one per leaf)
    low_level_moves = leaf_diffs
    # Each low-level move gets extended: upper-level (1 per depth level),
    # delete-instead-of-replace (1 per replace), delete-refs (variable)
    avg_depth = 3  # typical config path depth
    extended_per_move = avg_depth + 1  # upper-level extensions
    extended_moves = low_level_moves * extended_per_move
    # Bulk moves
    bulk_moves = len(diff_tables) * 2  # bulk key + bulk low-level per table

    total_moves = key_level_moves + low_level_moves + extended_moves + bulk_moves

    print(f"\nEstimated move generation:")
    print(f"  Key-level moves:    {key_level_moves}")
    print(f"  Low-level moves:    {low_level_moves}")
    print(f"  Extended moves:     {extended_moves}")
    print(f"  Bulk moves:         {bulk_moves}")
    print(f"  Total moves:        {total_moves}")

    # In DFS, each move goes through ALL validators:
    #   1. DeleteWholeConfigMoveValidator - O(1), cheap
    #   2. FullConfigMoveValidator - calls loadData() -> parse_data_mem() -> O(N²) leafref
    #   3. NoDependencyMoveValidator - calls loadData() -> parse_data_mem() again!
    #   4. CreateOnlyMoveValidator - O(paths), moderate
    #   5. RequiredValueMoveValidator - O(paths), moderate
    #   6. RemoveCreateOnlyDependencyMoveValidator - O(paths), moderate
    #   7. NoEmptyTableMoveValidator - O(1), cheap

    # The critical insight: validators 2 AND 3 both call parse_data_mem!
    # So each move validation = 2+ libyang parse_data_mem calls
    libyang_calls_per_validation = 2  # FullConfig + NoDependency (at minimum)

    # In DFS, we validate moves until we find one that passes, then recurse.
    # Best case: first move passes each level -> num_levels validations
    # Worst case: all moves tried at each level
    # Typical: some fraction of moves pass

    # For a simple patch, the sort depth = number of independent changes
    sort_depth = len(patch_ops)

    # At each depth, DFS tries moves until one validates.
    # With a well-ordered generator, typically 1-5 moves tried per level.
    # But with complex configs and dependencies, it can be much worse.
    avg_tries_per_level = 3  # conservative estimate
    total_validations = sort_depth * avg_tries_per_level
    total_libyang_calls = total_validations * libyang_calls_per_validation

    print(f"\nEstimated validation calls:")
    print(f"  Sort depth (levels):           {sort_depth}")
    print(f"  Avg tries per level:           {avg_tries_per_level}")
    print(f"  Total move validations:        {total_validations}")
    print(f"  libyang calls per validation:  {libyang_calls_per_validation}")
    print(f"  Total parse_data_mem calls:    {total_libyang_calls}")

    # Cost estimation
    # On small config: parse_data_mem ~0.1s
    # On high-radix (512+ ports): parse_data_mem ~3-10s due to O(N²) leafref
    entries_for_cost = (
        len(config['PORT']) +
        len(config['BUFFER_PG']) +
        len(config['BUFFER_QUEUE']) +
        len(config['QUEUE'])
    )

    # Model: parse_data_mem time ≈ base + k * N² (where N = number of entries with leafrefs)
    # Based on user's measurements: 0.65s individual, 3.55s combined for ~2056 entries
    # The O(N²) component: 2.9s for 2056² ≈ 4.2M lookups
    # k ≈ 2.9 / (2056²) ≈ 6.86e-7 per lookup
    base_time = 0.1  # seconds
    k = 6.86e-7
    estimated_parse_time = base_time + k * (entries_for_cost ** 2)

    total_estimated_time = total_libyang_calls * estimated_parse_time

    print(f"\nPerformance estimation:")
    print(f"  Config entries with leafrefs:  {entries_for_cost}")
    print(f"  Est. parse_data_mem time:      {estimated_parse_time:.2f}s")
    print(f"  Est. total sort time:          {total_estimated_time:.1f}s ({total_estimated_time/60:.1f} min)")

    if validation_delay > 0:
        simulated_total = total_libyang_calls * validation_delay
        print(f"  Simulated total (with delay):  {simulated_total:.1f}s ({simulated_total/60:.1f} min)")

    # Now show scaling
    print(f"\n{'='*60}")
    print(f"Scaling Analysis")
    print(f"{'='*60}")
    print(f"{'Ports':>8} {'Entries':>10} {'parse_time':>12} {'total_sort':>12} {'minutes':>10}")
    print(f"{'-'*8} {'-'*10} {'-'*12} {'-'*12} {'-'*10}")

    for ports in [32, 64, 128, 256, 512, 1024]:
        entries = ports * (1 + 2 + 8 + 8)  # PORT + BUFFER_PG + BUFFER_QUEUE + QUEUE
        parse_t = base_time + k * (entries ** 2)
        total_t = total_libyang_calls * parse_t
        print(f"{ports:>8} {entries:>10} {parse_t:>11.2f}s {total_t:>11.1f}s {total_t/60:>9.1f}")

    # Worst case scenario: more complex patch with dependencies
    print(f"\n{'='*60}")
    print(f"Worst Case: Complex patch with 20 operations, 50 tries/level")
    print(f"{'='*60}")
    print(f"{'Ports':>8} {'parse_time':>12} {'validations':>13} {'total_sort':>12} {'hours':>8}")
    print(f"{'-'*8} {'-'*12} {'-'*13} {'-'*12} {'-'*8}")

    for ports in [32, 64, 128, 256, 512, 1024]:
        entries = ports * 19
        parse_t = base_time + k * (entries ** 2)
        worst_validations = 20 * 50  # 20 levels, 50 tries each
        worst_libyang = worst_validations * 2
        total_t = worst_libyang * parse_t
        print(f"{ports:>8} {parse_t:>11.2f}s {worst_validations:>13} {total_t:>11.1f}s {total_t/3600:>7.1f}")


def main():
    parser = argparse.ArgumentParser(description='GCU sort performance reproducer')
    parser.add_argument('--ports', type=int, default=256,
                        help='Number of ports to simulate (default: 256)')
    parser.add_argument('--validation-delay', type=float, default=0.0,
                        help='Simulated delay per validation call in seconds')
    args = parser.parse_args()

    run_sort_simulation(args.ports, args.validation_delay)

    print(f"\n{'='*60}")
    print("ROOT CAUSE ANALYSIS")
    print(f"{'='*60}")
    print("""
The GCU sort step performance issue has THREE compounding factors:

1. ALGORITHMIC: O(moves × validators) per sort level
   - DfsSorter generates moves via 6+ generators + 4 extenders
   - Each move is validated by 7 validators sequentially
   - If a move fails validation, the next move is tried
   - DFS recurses: total validations = Σ(tries_per_level) across all levels

2. DOUBLE YANG VALIDATION per move:
   - FullConfigMoveValidator calls loadData() → parse_data_mem()
   - NoDependencyMoveValidator ALSO calls loadData() → parse_data_mem()
   - That's 2+ full config parses PER MOVE VALIDATION
   - Neither result is cached or reused

3. LIBYANG v1 O(N²) LEAFREF RESOLUTION:
   - parse_data_mem() → resolve_unres_data() does cross-module leafref validation
   - For each leafref, it linearly scans the referenced table
   - BUFFER_PG references PORT, BUFFER_QUEUE references PORT, etc.
   - 2056 PG entries × 2056 PORT entries = ~4M lookups per parse
   - This makes each parse_data_mem call take 3-10s on high-radix

Combined: O(sort_levels × tries_per_level × validators × N²)
On 512-port system: O(20 × 50 × 2 × (9728²)) ≈ hours

SHORT-TERM FIXES (no libyang changes):

A. Cache parse_data_mem results in FullConfigMoveValidator
   - Hash the simulated_config, skip re-validation if seen before
   - Risk: low (same config = same validation result)
   - Impact: eliminates redundant validations

B. Share loaded YANG data between FullConfigMoveValidator and NoDependencyMoveValidator
   - Currently both independently call loadData() on the same config
   - Pass the already-loaded SonicYang object between validators
   - Impact: cuts libyang calls per validation from 2+ to 1

C. Validate only affected modules, not full config
   - A move to /POLICER only needs to validate sonic-policer YANG, not all modules
   - parse_data_mem could be called with just the affected module's data
   - Impact: reduces N² to much smaller scope

D. Skip FullConfigMoveValidator for intermediate moves
   - Only validate the FINAL sorted config strictly
   - Use NoDependencyMoveValidator (which checks structural deps) for intermediate
   - Risk: moderate (intermediate states might be invalid but final is valid)
   - Impact: cuts libyang calls by ~50%

E. Use LYD_OPT_CONFIG without LYD_OPT_STRICT for intermediate validations
   - LYD_OPT_STRICT forces leafref resolution
   - Without it, parse is faster but less thorough
   - Validate strictly only the final result
   - Impact: significant speedup per parse call

LONG-TERM FIXES:

F. Incremental YANG validation
   - Instead of re-parsing entire config per move, apply diff to existing data tree
   - Use lyd_new_path() / lyd_unlink() + validate()
   - Avoid O(N²) re-resolution on each move
   - Impact: fundamental fix, but requires sonic-yang-mgmt changes

G. libyang v2 upgrade (tracked in sonic-buildimage#22385)
   - Hash-based leafref resolution instead of linear scan
   - But complex migration, stalled for ~1 year

H. Smarter sort algorithm
   - Current DFS can explore exponentially many paths
   - Topological sort based on YANG dependency graph would be O(N log N)
   - Pre-compute move ordering from dependency analysis
""")


if __name__ == '__main__':
    main()
