# GCU `apply-patch` Sort Step Performance Reproducer

Reproduces the O(N²) performance degradation in SONiC's `config apply-patch` command on high-radix systems (256+ ports). Uses the **real libyang v1.0-r4** — the same version SONiC ships.

## Quick Start

```bash
cd gcu-perf-repro
docker build -t gcu-perf-repro .
docker run --rm gcu-perf-repro

# Run specific tests:
docker run --rm gcu-perf-repro python3 repro_real.py --test scaling
docker run --rm gcu-perf-repro python3 repro_real.py --test sort
docker run --rm gcu-perf-repro python3 repro_real.py --test options
```

## Problem

On high-radix SONiC systems (512+ ports), `config apply-patch` can take **hours** for simple patches. The bottleneck is the **sort step** in `patch_sorter.py`, which determines the safe order to apply config changes.

The sort step calls `SonicYang.loadData()` to validate each candidate move. Each `loadData()` call invokes libyang v1's `parse_data_mem()`, which runs `resolve_unres_data()` to resolve leafref cross-references. This resolution is **O(N²)** in the number of config entries with leafrefs.

### Root Cause: Triple Compounding

1. **Double YANG validation per move** — Both `FullConfigMoveValidator` and `NoDependencyMoveValidator` independently call `loadData()` on the full config. Each move gets validated twice.

2. **O(N²) leafref resolution in libyang v1** — `parse_data_mem()` → `resolve_unres_data()` iterates over all unresolved data nodes, and for each leafref, scans the data tree to find the target. With N entries referencing M targets, this is O(N×M).

3. **DFS explores many moves** — The sort algorithm uses depth-first search, exploring many candidate move orderings. Each candidate triggers the double validation above.

### Code Path

```
MoveWrapper.validate()                           # patch_sorter.py:467
  → FullConfigMoveValidator.validate()            # patch_sorter.py
    → ConfigWrapper.validate_config_db_config()   # gu_common.py:140
      → SonicYang.loadData(config)                # sonic_yang_ext.py
        → ctx.parse_data_mem(json, LYD_JSON,      # sonic_yang_ext.py:1249
            LYD_OPT_CONFIG | LYD_OPT_STRICT)
          → resolve_unres_data()                  # libyang/src/resolve.c  ← O(N²)
  → NoDependencyMoveValidator._validate_paths()   # patch_sorter.py
    → PathAddressing.find_ref_paths()             # gu_common.py:487
      → SonicYang.loadData(config)                # SECOND full parse!
```

## Results

### TEST 1: O(N²) Scaling Confirmed

| Ports | Config Entries | loadData Time | Ratio vs 8-port |
|------:|---------------:|--------------:|-----------------:|
| 8     | 48             | 0.002s        | 1.0x             |
| 16    | 96             | 0.003s        | 1.6x             |
| 32    | 192            | 0.006s        | 2.9x             |
| 64    | 384            | 0.014s        | 6.8x             |
| 128   | 768            | 0.037s        | 18.3x            |
| 256   | 1,536          | 0.128s        | 62.4x            |
| 512   | 3,072          | 0.456s        | 223.0x           |

Doubling ports → ~4x parse time. Classic O(N²).

### TEST 2: Split vs Combined Parsing (128 ports)

| Scenario | Time |
|----------|-----:|
| Combined parse (all tables) | 0.038s |
| PORT only | 0.006s |
| BUFFER_PG (with PORT) | 0.017s |
| BUFFER_QUEUE (with PORT) | 0.023s |

Cross-ref overhead grows with entry count — real production configs with 10,000+ entries see much larger penalties.

### TEST 3: LYD_OPT_STRICT Impact

| Flag | Time (128 ports) |
|------|-----:|
| LYD_OPT_CONFIG \| LYD_OPT_STRICT | 0.035s |
| LYD_OPT_CONFIG only | 0.034s |

**No measurable difference.** The O(N²) cost is in `resolve_unres_data()` which runs as part of basic data parsing, not as an optional strict validation pass.

### TEST 4: Projected Sort Times

For 100 validation calls (typical medium patch):

| Port Count | Per-Validation | Total (100 calls) |
|-----------:|---------------:|-------------------:|
| 64         | 0.015s         | ~1s                |
| 128        | 0.06s          | ~6s                |
| 256        | 0.23s          | ~23s               |
| 512        | 0.93s          | ~93s               |

**Real-world impact:** Production systems with 512+ ports, full config (PORT, BUFFER_PG, BUFFER_QUEUE, QUEUE, ACL_TABLE, VLAN_MEMBER, etc.), and complex patches can see **thousands** of validation calls, pushing total sort time to hours.

## Recommended Fixes

Ordered by impact and implementation difficulty:

| Fix | Description | Expected Reduction | Effort |
|-----|-------------|-------------------|--------|
| **B** | Share SonicYang instance between validators | ~50% (eliminate double parse) | Low |
| **A** | Cache validation results by config hash | ~40-60% (skip repeated configs) | Low |
| **C** | Validate only changed modules, not full config | ~70-90% | Medium |
| **D** | Use `LYD_OPT_CONFIG` without `LYD_OPT_STRICT` | Negligible (disproven by TEST 3) | N/A |
| **E** | Incremental validation (edit data tree in-place) | ~90%+ | High |
| **F** | Upgrade to libyang v2 | Unknown (stalled PR [#22385](https://github.com/sonic-net/sonic-buildimage/pull/22385)) | Very High |

**Best first step:** Combine B + A for ~75% reduction with minimal code changes.

### Fix B Detail

`SortAlgorithmFactory.create()` (patch_sorter.py line ~2107) creates both `FullConfigMoveValidator` and `NoDependencyMoveValidator` independently. Both create their own `SonicYang` instance and call `loadData()` on the same config.

**Solution:** Create one shared `SonicYang` instance, pass it to both validators. `NoDependencyMoveValidator` already has a `reload_config` parameter partially plumbed.

### Fix A Detail

`FullConfigMoveValidator.validate()` is called with the same `simulated_config` multiple times during DFS exploration. Cache results keyed on a hash of the config dict.

## Environment

- libyang: v1.0-r4 (1.0.73) — built from source
- YANG models: 140 models from sonic-buildimage master
- Python: 3.9
- Base image: Debian Bullseye (slim)

## Related Issues

- [sonic-net/sonic-buildimage#24031](https://github.com/sonic-net/sonic-buildimage/issues/24031) — GCU `show policer` namespace bug
- [sonic-net/sonic-buildimage#22385](https://github.com/sonic-net/sonic-buildimage/pull/22385) — libyang v2 upgrade (stalled)
