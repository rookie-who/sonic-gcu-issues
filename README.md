# sonic-gcu-issues

Reproducers and analysis for SONiC Generic Config Updater (GCU) performance issues.

## Issue: `apply-patch` Sort Step O(N²) Degradation

On high-radix SONiC systems (512+ ports), `config apply-patch` takes **minutes to hours** for simple patches due to O(N²) leafref resolution in libyang v1, called repeatedly by multiple validators per candidate move.

**Root cause:** Each `SonicYang.loadData()` call triggers `resolve_unres_data()` in libyang v1, which is O(N²) in config entry count. The sort step calls `loadData()` 3-4 times per candidate move across 3 validators (FullConfigMoveValidator, NoDependencyMoveValidator, RemoveCreateOnlyDependencyMoveValidator), all sharing a single SonicYang singleton.

See [`gcu-perf-repro/README.md`](gcu-perf-repro/README.md) for full analysis, benchmark results, and fix options.

## Contents

- [`gcu-perf-repro/`](gcu-perf-repro/) — Docker-based reproducer with real libyang v1.0-r4
  - `repro_real.py` — O(N²) scaling proof
  - `benchmark_accurate.py` — Per-op-type benchmark mirroring real validator chain
  - `fix_b_benchmark.py` — ⚠️ DEPRECATED (incorrect benchmark)

## Related PRs

- [sonic-net/sonic-utilities #4466](https://github.com/sonic-net/sonic-utilities/pull/4466) — Hash cache optimization (ADD: 2.8x, REPLACE: 1.3x, REMOVE: no change)
