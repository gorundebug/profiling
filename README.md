# ServiceLib example profiling

Captures CPU flamegraphs of `orderservice` and `inventoryservice` for each
ServiceLib example port (Go, userver C++, Boost C++, Python, Rust, TypeScript)
and the available native baselines, under the same synthetic load used by
`benchmarks/`. This isolates *where* CPU time actually goes per language,
rather than just comparing aggregate throughput/latency numbers.
Per-request ServiceLib logging is disabled for the Boost profile, matching the
benchmark and native baseline; load failures and profiling failures are still
enforced from the result and retained artifacts.

## How it works

A `profiler` sidecar container shares `orderservice`'s PID namespace
(`pid: "service:orderservice"`), so it can sample the target process
directly by PID:

- **Go, userver C++, Boost C++, Rust** (native binaries with frame pointers): `perf record -g`
  → `perf script` → the classic
  [FlameGraph](https://github.com/brendangregg/FlameGraph) Perl scripts →
  SVG.
- **Python**: [`py-spy`](https://github.com/benfred/py-spy) samples the
  interpreter directly and writes an SVG flamegraph natively — no
  perf/collapse step needed. The default rate is 5 Hz to avoid a sampling
  backlog under full load; override it with `PROFILING_PYSPY_RATE`.
  Sampling is interrupted if it exceeds the requested duration by 30 seconds;
  override that wall-clock limit with `PROFILING_PYSPY_TIMEOUT`.
- **TypeScript/Node.js**: the profiler connects to the Node Inspector and
  records a V8 `.cpuprofile`. Generated JavaScript positions are rewritten
  through the deployed source maps before folded stacks, top tables and SVGs
  are produced, so application and framework frames point to `.ts` sources.
  During the same measured interval it records event-loop delay/utilization,
  process CPU deltas, V8 GC pauses, memory and active resource types in a raw
  `.runtime.json` time series for both framework and native services.
- **Off-CPU call sites**: `--profile-kind offcpu` records
  `sched:sched_switch` with complete user/kernel call chains. It writes a
  separate `.offcpu.flamegraph.svg`, folded stacks and top-frame table so
  futex, epoll, executor and gRPC CompletionQueue blocking sites can be tied
  to the per-thread wait durations from `--profile-kind scheduler`.
- **Allocation call sites**: `--profile-kind allocation` keeps the exact
  allocator-neutral counters and additionally samples one allocation stack per
  4096 calls by default. The profiling-only `LD_PRELOAD` interceptor writes
  fixed binary records without a heap allocation or mutex in the hook. A
  sidecar symbolizes them while the process and its ASLR mappings are still
  available, producing call-count and allocated-byte folded stacks, an SVG
  flamegraph, a top-call-site table and a machine-readable summary. Change the
  interval with `--allocation-stack-sample-every N`.
  TypeScript uses V8 sampling heap profiling instead of `LD_PRELOAD`; it emits
  a source-mapped `.heapprofile`, folded byte-weighted stacks, a top table and
  a flamegraph over the same measured request interval.

`perf_event_paranoid` is a global (non-namespaced) host kernel setting;
Docker Desktop's Linux VM defaults to `2`, which blocks profiling another
process. The normal runner does not change it. On a dedicated profiling host,
`--prepare-host-profiling` explicitly lowers it to `1` once via a throwaway
`--privileged` container, rather than running the profiler container itself
`--privileged`.

`vm.max_map_count` is another global host sysctl and is also left untouched by
default. userver mmaps a stack per coroutine, so a dedicated high-concurrency
comparison host may explicitly pass `--max-map-count 1048576` if its existing
limit is insufficient. Both switches persist outside the profiling process
and must not be enabled implicitly by onboarding or conformance commands.

## Quickstart

Only this repository needs to be cloned by hand. `quickstart.sh` clones the
repositories it depends on into `.dependencies` (if missing) and runs the
profiler with sensible defaults:

```bash
git clone https://github.com/gorundebug/profiling.git
cd profiling
./quickstart.sh
```

The default profiles the canonical `function-call` graph. To profile the same
framework implementations with one `TaskPool`, one `PriorityTaskPool` and
three `ParallelCall` links, select the generated `current` profile:

```bash
./quickstart.sh --profile current
./quickstart.sh --profile current -- --language rust --duration 20s
```

The benchmark-style spelling is an exact alias for the same generated pooled
profile:

```bash
./quickstart.sh -- call-semantics
./quickstart.sh -- call-semantics --language rust --duration 20s
```

`call-semantics` is consumed by `quickstart.sh`; it is not forwarded as a
positional argument to `examples/run.py`.

The profiled examples are disposable copies; managed canonical checkouts are
not modified. Switching profiles clears incompatible profiling artifacts, and
the selected graph profile is recorded in `run-manifest.json`.

Requires `git`, `docker` (with the `compose` plugin) and `python3`. Extra
arguments after `--` are forwarded to `make profile`, e.g.:

Framework repositories follow their managed `main` checkouts. Native
baselines are restored at the exact release tags recorded by the runner, so a
clean-machine profile does not silently change when a native repository's
`main` branch advances.

```bash
./quickstart.sh -- --language rust --duration 20s
./quickstart.sh -- --language cpp-native --duration 20s
./quickstart.sh -- --language cppboost --duration 20s
./quickstart.sh -- --language cppboost-native --duration 20s
./quickstart.sh -- --language typescript --language typescript-native --duration 20s
```

Run benchmark, profiling and conformance sequentially. They intentionally use
equivalent service ports and Docker resources; a shared tooling lock rejects a
second concurrent run instead of allowing it to corrupt build state or distort
measurements.

Use `./quickstart.sh --clone-only` to just fetch the sibling repos without
running anything.

Delete `examples/.artifacts` before a deliberately clean profiling run. The
quickstart normally refreshes existing Git mirrors before resolving managed
revisions; `--skip-git-mirror-refresh` is only for a known-fresh offline cache,
not a fallback after refresh failure.

### Optional shared package proxy

To route package downloads through the generated shared Nexus proxy, opt in
with one global data directory:

```bash
./quickstart.sh --clone-only
export DEPENDENCY_PROXY_DIR="$HOME/.servicegen/dependency-proxy"
make -C .dependencies/goexample DEPENDENCY_PROXY_ACCEPT_EULA=true dependency-cache-up # first start only
./quickstart.sh
```

The quickstart configures host and Docker consumers automatically, including
Docker Engine on Linux through `host-gateway`. Without the variable it uses
normal upstreams. The persistent proxy data is shared with benchmarks,
conformance and generated projects. It caches package registries, Debian/Ubuntu
APT packages and immutable GitHub/GitLab release archives; the companion Git
mirror caches project clones. Profiler outputs and compiler artifacts remain
separate.

To reuse an existing set of repositories instead of `.dependencies`, pass it
explicitly:

```bash
./quickstart.sh --dependencies-dir /path/to/repos -- --language cppboost
```

A direct `make profile` from the common development workspace remains
supported: when `DEPENDENCIES_DIR` is unset, the profiler looks for
the example repositories next to the `profiling` checkout.

After changing a pinned C++ dependency version, explicitly discard prepared
sources and CMake state while keeping ccache and Nexus data:

```bash
make dependency-source-cache-invalidate
```

## Usage

```bash
make profile
# or directly:
python3 examples/run.py --language rust --duration 20s
python3 examples/run.py --language cppboost --language cppboost-native \
  --profile-kind allocation --profile-kind scheduler \
  --profile-kind offcpu --duration 20s
```

The default load is `256` virtual users with `2` CPU cores per service and
`6` CPU cores for the load generator, matching the comparative benchmark.
Override it with `--vus`, `--cores` or `--loadgen-cores` when profiling a
different operating point.

Profiling follows the same Kafka-free execution mode as the benchmark:
Redpanda is not started, every framework implementation disables the
`orderProcessed` endpoint, and native implementations receive no Kafka
configuration. That remains the default `--scenario normal` behavior.

## Temporal and DurableCall profiling

The normal profiler leaves Cron and Temporal disabled and starts no Automation
Service or Temporal infrastructure. Profile the durable path explicitly with:

```bash
make durable ARGS="--cores 2 --duration 20 --jobs 10000"
```

This runs Go, Python, and TypeScript sequentially, starts the generated
Automation Service with real Temporal/PostgreSQL, pauses periodic admission,
queues a deterministic Schedule backfill, and samples the complete endpoint →
graph → `DurableCall` → result path. Outputs are stored separately under
`examples/.artifacts/durable/`, including folded stacks, flamegraphs, ranked
top tables, and per-language summaries. Native, Rust, and C++ variants are not
listed because they do not have a production Temporal SDK implementation. Use
`make durable-quick` only after the corresponding images and profiler sidecar
have already been built.

TypeScript framework/native CPU profiling also supports explicit failure-path
experiments:

```bash
python3 examples/run.py --language typescript --language typescript-native \
  --scenario timeout
python3 examples/run.py --language typescript --language typescript-native \
  --scenario cancellation
python3 examples/run.py --language typescript --language typescript-native \
  --scenario overload --rate 100000
python3 examples/run.py --language typescript --language typescript-native \
  --scenario kafka-recovery
```

`timeout` pauses Inventory and requires an HTTP `200` whose business status is
`TIMED_OUT`. `cancellation` pauses Inventory, applies a 100 ms client deadline
and requires the corresponding transport timeout. `overload` uses a constant
arrival rate and is accepted only when the load generator records dropped
iterations, proving that the requested rate exceeded capacity. `kafka-recovery`
enables the canonical Order producer and Analytics consumer, stops Redpanda
during measured load, starts it again and waits for cluster health. The latter
also writes a matching `.orchestration.json` timeline. These scenarios are
CPU-only: allocation, scheduler and off-CPU profiles retain the stable normal
workload so unrelated experiment dimensions are not mixed.

Every invocation writes `run-manifest[.<scenario>].json` containing exact
source revisions and dirty state, SHA-256 hashes of runner/load/Compose inputs,
container image IDs/digests, profile kinds, quotas, VUs, duration, warm-up and
scenario expectations. Non-normal artifacts include the scenario key in their
filename and therefore do not overwrite normal profiles.

If the host kernel denies `perf` attachment, prepare a dedicated profiling
host explicitly:

```bash
python3 examples/run.py --prepare-host-profiling --language cppboost
```

Flamegraphs are written to
`.artifacts/<language>.<service>.flamegraph.svg`. Open
directly in a browser — they're interactive (click to zoom, search).

Each run also writes
`.artifacts/<language>.<service>.flamegraph.svg.folded.txt`, the
raw collapsed-stack text (`func1;func2;func3 count` per line) behind the
SVG. The SVG is best for visual/interactive exploration of one language; the
folded text is the better format for scripted, quantitative comparison
across languages (e.g. aggregating self-time per leaf frame), since
comparing SVGs by eye across the different codebases' symbol namespaces
doesn't scale.

Allocation profiles additionally write
`.artifacts/<language>.<service>.allocation-stacks.flamegraph.svg` and the
corresponding `.folded.txt`, `.bytes.folded.txt`, `.top.txt`, `.summary.json`
and `.maps.txt` files. Sampling begins only after warm-up (`SIGUSR1`) and stops
immediately after measured load (`SIGUSR2`), so startup allocations are not
mixed into the attribution. This interceptor is never enabled by benchmark or
production builds.

For `typescript` and `typescript-native`, the equivalent raw artifacts are
`.flamegraph.svg.cpuprofile` for CPU and
`.allocation-stacks.flamegraph.svg.heapprofile` for allocations. Their folded
stacks and top tables use TypeScript source locations when a deployed source
map is available. Every Node CPU and allocation profile also has a matching
`.runtime.json` file containing timestamped event-loop, GC, CPU, memory and
active-resource samples. Framework profiles additionally retain the canonical
Prometheus runtime/pool/task series as
`.artifacts/typescript.<service>.runtime-metrics.json`.

Node allocation runs also write a post-load
`.allocation-stacks.flamegraph.svg.heapsnapshot` using the public Inspector
heap-snapshot format. Sampling is stopped first, a full GC is requested, and
the snapshot is captured only after the measured request window, so snapshot
pause time cannot change the recorded throughput or latency. Open it in Chrome
DevTools to inspect retained objects and dominators; the matching summary
records validated node count, file size and total V8 `self_size` bytes.

Each Node profile has a matching `.summary.json` containing the exact scenario,
quota, VUs, duration, request count, load artifact and quantitative runtime
totals. When framework and native are selected together, the runner also writes
`typescript.framework-native.{cpu,heap}.{json,md}` with RPS and latency from the
same run alongside event-loop utilization/lag, GC pauses and sampled allocation
bytes per request. Short diagnostic runs are not release benchmark results; use
the documented default duration for a meaningful comparison.

The Markdown comparison keeps p50/p95/p99/max from that same measured run and
also reports process CPU time and event-loop idle time. The runner rejects load
artifacts whose scenario, duration, VUs, service/load-generator CPU quotas,
errors or dropped iterations do not match the requested profile, and rejects
empty raw or derived Node artifacts.

The load artifact separately records semantic check failures and raw transport
failures, plus started, completed and interrupted iterations. Expected client
cancellation is therefore measurable without being mislabeled as a bad test,
while a missing cancellation, an unexpected transport error, or any interrupted
iteration still fails the run. The profiler stop marker is written only after
k6 has drained its scenario-specific grace window, so the raw CPU profile covers
the same accepted request window rather than silently ending before slow
timeout responses complete.

For `cppboost`, profiling additionally scrapes the service while load is
active and writes
`.artifacts/cppboost.<service>.runtime-metrics.json`. Each timestamped sample
contains `runtime_active_work`, `runtime_worker_utilization` and
`runtime_event_loop_lag_seconds`. Use this time series together with the
flamegraph and load-generator JSON to distinguish CPU saturation from an idle
or blocked event loop. A missing time series fails the profiling run instead
of silently producing an incomplete artifact set.

Runtime utilization is sampled from the worker threads' CPU clocks and event
loop lag from one periodic Asio timer. The runtime does not wrap every Asio or
gRPC continuation in a tracking executor; diagnostics therefore do not move
handlers or add per-continuation callbacks to the request path.

## CI artifacts

The manual **TypeScript profiling artifacts** GitHub Actions workflow first
runs the framework and native clean-machine package, coverage, event-loop and
diagnostic gates. Only then does it capture the common 2-core, 6-loadgen-core,
256-VU CPU and heap profiles. Raw profiles, source-mapped folded stacks,
flamegraphs, load results, runtime samples and framework/native reports are
uploaded together as the `typescript-profiling-<run-id>` artifact for 30 days.
Local development profiles remain ignored under `.artifacts/`.

```bash
python3 examples/run.py --skip-build   # reuse already-built images
python3 examples/run.py --clean        # tear down and remove artifacts
```
