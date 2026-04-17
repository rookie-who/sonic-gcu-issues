# GCU `apply-patch` Sort Step Performance Reproducer

Reproduces the O(N²) performance degradation in SONiC's `config apply-patch` command on high-radix systems (256+ ports). Uses the **real libyang v1.0-r4** — the same version SONiC ships.

## Quick Start

```bash
cd gcu-perf-repro
docker build -t gcu-perf-repro .

# O(N²) scaling test
docker run --rm gcu-perf-repro python3 repro_real.py --test scaling

# Accurate benchmark (mirrors real validator code path)
docker run --rm -v $(pwd)/benchmark_accurate.py:/sonic/benchmark_accurate.py \
  gcu-perf-repro python3 /sonic/benchmark_accurate.py --ports 512 --moves 10
```

## Problem

On high-radix SONiC systems (512+ ports), `config apply-patch` can take **hours** for simple patches. The bottleneck is the **sort step** in `patch_sorter.py`, which determines the safe order to apply config changes.

The sort step calls `SonicYang.loadData()` to validate each candidate move. Each `loadData()` call invokes libyang v1's `parse_data_mem()`, which runs `resolve_unres_data()` to resolve leafref cross-references. This resolution is **O(N²)** in the number of config entries with leafrefs.

### Root Cause

Multiple validators call `loadData()` independently on the same shared SonicYang singleton per candidate move:

| Validator | ADD | REMOVE | REPLACE |
|-----------|-----|--------|---------|
| FullConfigMoveValidator | 1× simulated | 1× simulated | 1× simulated |
| NoDependencyMoveValidator | 1× simulated | 1× current | 1× current + 1× simulated |
| RemoveCreateOnlyDependencyMoveValidator | 1× simulated | 1× simulated | 1× simulated |
| **Total loadData per move** | **3** | **3** | **4** |

**Key architectural fact:** All validators share a **single SonicYang instance** via `ConfigWrapper.create_sonic_yang_with_loaded_models()` (a cached singleton). The cost is in repeated `loadData()` calls, not in creating separate instances.

### Code Path

```
MoveWrapper.validate(move, diff)                  # patch_sorter.py:469
  simulated_config = move.apply(diff.current_config)  # new object each time
  for validator in move_validators:
    validator.validate(move, diff, simulated_config)

  → FullConfigMoveValidator.validate()              
    → ConfigWrapper.validate_config_db_config(simulated_config)
      → sy.loadData(simulated_config)               # loadData #1

  → NoDependencyMoveValidator.validate()
    For ADD:  find_ref_paths(path, simulated_config)  # loadData #2
    For REMOVE: find_ref_paths(path, current_config)  # loadData #2
    For REPLACE:
      find_ref_paths(deleted, current_config)         # loadData #2
      find_ref_paths(added, simulated_config)          # loadData #3

  → RemoveCreateOnlyDependencyMoveValidator.validate()
    → find_ref_paths(member, simulated_config)         # loadData #3 or #4
```

All `find_ref_paths` calls go through `PathAddressing.find_ref_paths()` → same shared `sy.loadData(config)`.

## Results

### O(N²) Scaling Confirmed

| Ports | Config Entries | loadData Time | Ratio vs 8-port |
|------:|---------------:|--------------:|-----------------:|
| 8     | 48             | 0.002s        | 1.0x             |
| 64    | 384            | 0.014s        | 6.8x             |
| 128   | 768            | 0.037s        | 18.3x            |
| 256   | 1,536          | 0.128s        | 62.4x            |
| 512   | 3,072          | 0.456s        | 223.0x           |

Doubling ports → ~4x parse time at scale. Classic O(N²).

### Hash Cache Optimization (content-hash to skip redundant loadData)

Tested with accurate benchmark mirroring real validator chain (shared singleton, exact call pattern):

**At 512 ports, 10 moves:**

| Op Type | No Cache | Hash Cache | Loads | Skips | Speedup |
|---------|----------|------------|-------|-------|---------|
| ADD     | 14.3s    | 5.1s       | 10    | 20    | **2.8x** |
| REMOVE  | 14.1s    | 15.2s      | 30    | 0     | 1.0x    |
| REPLACE | 19.0s    | 15.0s      | 30    | 10    | **1.3x** |

**Why REMOVE gets no benefit:** Validators alternate between `simulated_config` (FullConfig, RemoveCreateOnlyDep) and `current_config` (NoDependency). Single-entry cache always misses.

**Projected for 100 moves at 512 ports:**
- ADD: 156s → 52s (save 104s)
- REMOVE: 158s → 160s (no improvement)
- REPLACE: 213s → 162s (save 51s)

### LYD_OPT_STRICT Has No Impact

| Flag | Time (128 ports) |
|------|-----:|
| LYD_OPT_CONFIG \| LYD_OPT_STRICT | 0.035s |
| LYD_OPT_CONFIG only | 0.034s |

The O(N²) cost is in `resolve_unres_data()` which runs as part of basic parsing, not optional strict validation.

## Fix Options

Ordered by expected impact:

| Fix | Description | Impact | Effort |
|-----|-------------|--------|--------|
| **A** | Pass pre-loaded SonicYang through validator chain | All ops → 1 loadData/move | Medium |
| **B** | Content-hash cache (current PR [#4466](https://github.com/sonic-net/sonic-utilities/pull/4466)) | ADD: 67%, REPLACE: 25%, REMOVE: 0% | Low |
| **C** | Validate only changed YANG modules, not full config | ~70-90% per loadData | Medium |
| **D** | Smarter DFS pruning / move ordering | Fewer total moves evaluated | Medium |
| **E** | Incremental validation (edit data tree in-place) | ~90%+ | High |
| **F** | Upgrade to libyang v2 | Unknown (stalled [#22385](https://github.com/sonic-net/sonic-buildimage/pull/22385)) | Very High |

### Fix A: Restructure Validator Chain (Best Next Step)

Instead of each validator independently calling `loadData()`, load data **once** in `MoveWrapper.validate()` and pass the loaded SonicYang instance to all validators. This reduces ALL operation types to 1 loadData per move — a 3-4x reduction.

### Fix B: Hash Cache (Current PR)

Track loaded config via `id()` + MD5 content hash in `ConfigWrapper`. Skip `loadData` when same content is already loaded. Works well for ADD (3→1 loads) but can't help REMOVE (alternating configs).

PR: https://github.com/sonic-net/sonic-utilities/pull/4466

## Files

- `repro_real.py` — O(N²) scaling test with real libyang v1
- `benchmark_accurate.py` — Accurate per-op-type benchmark mirroring real validator chain
- `fix_b_benchmark.py` — **DEPRECATED** (incorrect benchmark, see below)
- `Dockerfile` — Docker build with libyang v1.0-r4 + 136 YANG models

### ⚠️ fix_b_benchmark.py is Wrong

The original benchmark created 2 separate SonicYang instances with 2× `loadYangModel()`. Real code uses a shared singleton. The reported "2x speedup" was from eliminating phantom `loadYangModel()` overhead that doesn't exist in production.

## Environment

- libyang: v1.0-r4 (1.0.73) — built from source
- YANG models: 140 models from sonic-buildimage master
- Python: 3.9
- Base image: Debian Bullseye (slim)

## Related

- [sonic-net/sonic-utilities PR #4466](https://github.com/sonic-net/sonic-utilities/pull/4466) — Hash cache fix (current)
- [sonic-net/sonic-buildimage#22385](https://github.com/sonic-net/sonic-buildimage/pull/22385) — libyang v2 upgrade (stalled)
