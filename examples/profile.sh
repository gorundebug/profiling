#!/usr/bin/env bash
set -euo pipefail

# Usage: profile.sh <perf|pyspy|node-cpu|node-heap|scheduler|offcpu> <process-pattern-or-inspector-url> <duration-seconds> <output> [ready-file]
#        profile.sh allocation-stacks <process-pattern> <raw-stack-file> <output-svg> [ready-file]
#
# Runs inside a container that shares the target service's PID namespace
# (compose `pid: "service:<name>"`), so the target process is directly
# visible and addressable by PID from here.
#
# Writes three artifacts next to <output-svg>:
#   - <output-svg>            interactive flamegraph (visual/exploratory use)
#   - <output-svg>.folded.txt collapsed stacks "func1;func2;func3 count"
#     (machine-readable — for scripted self-time/category aggregation across
#     languages, which an SVG alone does not support)
#   - <output-svg>.top.txt    ranked self-time/total-time tables with real
#     percentages of the sampled total, computed from the folded stacks above
#     (see analyze_folded.py) — read this first; it's what actually answers
#     "what's the bottleneck", the SVG is for visual exploration afterward

tool="$1"
pattern="$2"
duration="$3"
output="$4"
ready_file="${5:-}"
stop_file="${6:-}"
folded_output="${output}.folded.txt"
perf_frequency="${PROFILING_PERF_FREQUENCY:-997}"
perf_event="${PROFILING_PERF_EVENT:-}"
perf_period="${PROFILING_PERF_PERIOD:-}"
perf_call_graph="${PROFILING_PERF_CALL_GRAPH:-dwarf,16384}"
perf_offcpu_call_graph="${PROFILING_PERF_OFFCPU_CALL_GRAPH:-fp}"
pyspy_rate="${PROFILING_PYSPY_RATE:-100}"
pyspy_timeout="${PROFILING_PYSPY_TIMEOUT:-}"
pyspy_nonblocking="${PROFILING_PYSPY_NONBLOCKING:-0}"
perf_event_args=()
if [ -n "$perf_event" ]; then
  perf_event_args=(-e "$perf_event")
fi
perf_sampling_args=(-F "$perf_frequency")
if [ -n "$perf_period" ]; then
  perf_sampling_args=(-c "$perf_period")
fi

pid=""
if [ "$tool" = "node-cpu" ] || [ "$tool" = "node-heap" ]; then
  echo "profile.sh: profiling Node inspector at $pattern for ${duration}s via $tool" >&2
else
  deadline=$((SECONDS + 60))
  while [ -z "$pid" ] && [ "$SECONDS" -lt "$deadline" ]; do
    pid="$(pgrep -f "$pattern" | head -n1 || true)"
    if [ -z "$pid" ]; then
      sleep 0.5
    fi
  done
  if [ -z "$pid" ]; then
    echo "profile.sh: process matching '$pattern' not found within 60s" >&2
    exit 1
  fi
  if [ "$tool" = "allocation-stacks" ]; then
    echo "profile.sh: symbolizing allocation stacks for pid $pid ($pattern)" >&2
  else
    echo "profile.sh: profiling pid $pid ($pattern) for ${duration}s via $tool" >&2
  fi
fi

mkdir -p "$(dirname "$output")"

# Keep raw addresses and the exact mapped ELF files before the target exits.
# Do not change host security settings to obtain kernel symbols.
prepare_perf_artifacts() {
  perf_data="${output}.perf.data"
  perf_script="${output}.perf.script"
  perf_symbols="${output}.symbols"
  perf_diagnostics="${output}.symbolization.log"
  target_root="/proc/$pid/root"
  : > "$perf_diagnostics"
  cat "/proc/$pid/maps" > "${output}.maps.before.txt"
  {
    perf version
    uname -a
    printf 'kernel_boot_id=%s\n' "$(cat /proc/sys/kernel/random/boot_id)"
    printf 'pid=%s\ncall_graph=%s\nfrequency=%s\nperiod=%s\nevent=%s\n' \
      "$pid" "$active_call_graph" "$perf_frequency" "$perf_period" "$perf_event"
    printf 'executable=%s\n' "$(readlink "/proc/$pid/exe")"
  } > "${output}.perf.metadata.txt"
}

copy_perf_symbol_file() {
  local relative="$1"
  if [ -f "$target_root$relative" ]; then
    mkdir -p "$(dirname "$perf_symbols$relative")"
    if ! cp -L -- "$target_root$relative" "$perf_symbols$relative"; then
      printf 'Cannot preserve %s\n' "$relative" >> "$perf_diagnostics"
    fi
  fi
}

finish_perf_artifacts() {
  cat "/proc/$pid/maps" > "${output}.maps.after.txt"
  mkdir -p "$perf_symbols"
  # Include mappings from both boundaries. Libraries unloaded between them
  # remain identifiable in perf.data but may require the original image.
  while IFS= read -r mapped; do
    if [[ "$mapped" == *' (deleted)' ]]; then
      printf 'Deleted mapping requires original ELF: %s\n' "$mapped" >> "$perf_diagnostics"
      continue
    fi
    # /proc/maps escapes embedded newlines/backslashes with octal sequences.
    printf -v mapped '%b' "$mapped"
    copy_perf_symbol_file "$mapped"
    if [ ! -f "$perf_symbols$mapped" ]; then
      printf 'Missing mapped ELF: %s\n' "$mapped" >> "$perf_diagnostics"
      continue
    fi
    copy_perf_symbol_file "/usr/lib/debug${mapped}.debug"
    # Preserve a .gnu_debuglink companion when installed alongside the ELF.
    debug_link="$(readelf --string-dump=.gnu_debuglink "$perf_symbols$mapped" 2>/dev/null | sed -n 's/^.*\]  *//p' | head -n 1 || true)"
    if [ -n "$debug_link" ] && [[ "$debug_link" != */* ]]; then
      copy_perf_symbol_file "$(dirname "$mapped")/$debug_link"
      copy_perf_symbol_file "$(dirname "$mapped")/.debug/$debug_link"
      copy_perf_symbol_file "/usr/lib/debug$(dirname "$mapped")/$debug_link"
    fi
  done < <(awk '$2 ~ /x/ && $6 ~ /^\// { sub(/^[^/]*\//, "/"); print }' \
    "${output}.maps.before.txt" "${output}.maps.after.txt" | sort -u)

  perf buildid-list -i "$perf_data" > "${output}.buildids.txt" 2>> "$perf_diagnostics"
  while read -r build_id rest; do
    if [[ "$build_id" =~ ^[0-9a-fA-F]{8,}$ ]]; then
      copy_perf_symbol_file "/usr/lib/debug/.build-id/${build_id:0:2}/${build_id:2}.debug"
    fi
  done < "${output}.buildids.txt"
  perf_kernel_args=()
  if ! cat /proc/kallsyms > "${output}.kallsyms.txt" 2>> "$perf_diagnostics"; then
    printf 'Kernel symbols unavailable; host security settings were not changed.\n' >> "$perf_diagnostics"
  elif ! awk '$1 !~ /^0+$/ { found=1; exit } END { exit !found }' "${output}.kallsyms.txt"; then
    printf 'Kernel addresses are masked; matching kernel symbols are needed for offline decoding.\n' >> "$perf_diagnostics"
  else
    perf_kernel_args=(--kallsyms "${output}.kallsyms.txt")
  fi
  # Decode offline in a private PID/mount namespace: recorded PIDs must not
  # cause perf to enter a still-live target mount namespace, where our copied
  # symbol paths are unavailable. addr2line must use the same root for DSOs
  # without debug sections, for which perf 6.1 passes an unprefixed filename.
  PERF_SYMBOL_ROOT="$perf_symbols" PERF_SYMBOL_DIAGNOSTICS="$perf_diagnostics" \
    PATH="/usr/local/lib/perf-symbolizer:$PATH" \
    unshare --mount --pid --fork --mount-proc \
    perf script --inline --symfs "$perf_symbols" "${perf_kernel_args[@]}" -i "$perf_data" \
    > "$perf_script" 2>> "$perf_diagnostics"
  python3 /usr/local/bin/validate_perf_script.py "$perf_script" \
    > "${output}.symbolization.json"
  /opt/FlameGraph/stackcollapse-perf.pl "$perf_script" > "$folded_output"
  echo "profile.sh: retained $perf_data, $perf_script and $perf_symbols" >&2
}

case "$tool" in
  perf)
    active_call_graph="$perf_call_graph"
    prepare_perf_artifacts
    # Service runtimes may create or replace worker threads after attachment.
    # Keep those descendants in the same profile instead of silently
    # producing an empty or main-thread-only flamegraph.
    if [ -n "$ready_file" ]; then
      mkdir -p "$(dirname "$ready_file")"
      printf '%s\n' "$pid" > "$ready_file"
    fi
    perf record "${perf_event_args[@]}" "${perf_sampling_args[@]}" --inherit \
      --call-graph "$active_call_graph" -p "$pid" -o "$perf_data" -- sleep "$duration"
    finish_perf_artifacts
    ;;
  pyspy)
    if [ -z "$pyspy_timeout" ]; then
      # Under sustained load py-spy may spend substantially longer than the
      # sampling window draining ptrace samples and writing folded stacks.
      # Keep the timeout bounded, but scale it with the requested window.
      pyspy_timeout=$((duration * 12))
      if [ "$pyspy_timeout" -lt $((duration + 30)) ]; then
        pyspy_timeout=$((duration + 30))
      fi
    fi
    if [ -n "$ready_file" ]; then
      mkdir -p "$(dirname "$ready_file")"
      printf '%s\n' "$pid" > "$ready_file"
    fi
    pyspy_mode_args=()
    if [ "$pyspy_nonblocking" != "0" ]; then
      pyspy_mode_args=(--nonblocking)
    fi
    echo "profile.sh: py-spy diagnostics: version=$(py-spy --version 2>&1), target_pid=$pid, target_pattern=$pattern, sample_duration=${duration}s, sample_rate=${pyspy_rate}Hz, nonblocking=$pyspy_nonblocking, timeout=${pyspy_timeout}s, kill_grace=10s, output=$folded_output" >&2
    echo "profile.sh: py-spy target before sampling: $(ps -o pid=,ppid=,stat=,etime=,args= -p "$pid" 2>&1 || true)" >&2
    pyspy_started_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    set +e
    timeout --verbose --signal=INT --kill-after=10 "$pyspy_timeout" \
      py-spy record -f raw -o "$folded_output" -p "$pid" -d "$duration" \
        --rate "$pyspy_rate" "${pyspy_mode_args[@]}"
    pyspy_exit_code=$?
    set -e
    pyspy_finished_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    if [ -f "$folded_output" ]; then
      pyspy_output_bytes="$(wc -c < "$folded_output" | tr -d ' ')"
      pyspy_output_lines="$(wc -l < "$folded_output" | tr -d ' ')"
    else
      pyspy_output_bytes=0
      pyspy_output_lines=0
    fi
    echo "profile.sh: py-spy finished: exit_code=$pyspy_exit_code, started_at=$pyspy_started_at, finished_at=$pyspy_finished_at, output_exists=$([ -f "$folded_output" ] && echo yes || echo no), output_bytes=$pyspy_output_bytes, output_lines=$pyspy_output_lines, target_alive=$([ -d "/proc/$pid" ] && echo yes || echo no)" >&2
    if [ "$pyspy_exit_code" -eq 124 ]; then
      echo "profile.sh: py-spy exceeded the ${pyspy_timeout}s wall-clock timeout and exited after SIGINT" >&2
    elif [ "$pyspy_exit_code" -eq 137 ]; then
      echo "profile.sh: py-spy exceeded the ${pyspy_timeout}s wall-clock timeout and did not exit during the 10s SIGINT grace period; timeout sent SIGKILL" >&2
    elif [ "$pyspy_exit_code" -ne 0 ]; then
      echo "profile.sh: py-spy failed with exit code $pyspy_exit_code before a complete profile was produced" >&2
    fi
    if [ "$pyspy_exit_code" -ne 0 ]; then
      exit "$pyspy_exit_code"
    fi
    ;;
  node-cpu)
    /usr/local/bin/node_inspector_profile.py \
      cpu "$pattern" "$duration" "$output" "$ready_file" "$stop_file"
    ;;
  node-heap)
    /usr/local/bin/node_inspector_profile.py \
      heap "$pattern" "$duration" "$output" "$ready_file" "$stop_file"
    ;;
  scheduler)
    exec /usr/local/bin/scheduler_profile.py "$pattern" "$duration" "$output" "$ready_file"
    ;;
  offcpu)
    active_call_graph="$perf_offcpu_call_graph"
    prepare_perf_artifacts
    if [ -n "$ready_file" ]; then
      mkdir -p "$(dirname "$ready_file")"
      printf '%s\n' "$pid" > "$ready_file"
    fi
    # Capture the complete user/kernel call chain at every blocking context
    # switch. Paired scheduler profiles provide durations; these stacks provide
    # the missing futex/epoll/CQ/mutex call-site attribution.
    perf record -e sched:sched_switch --inherit --call-graph "$active_call_graph" -p "$pid" \
      -o "$perf_data" -- sleep "$duration"
    finish_perf_artifacts
    ;;
  allocation-stacks)
    bytes_folded_output="${output}.bytes.folded.txt"
    summary_output="${output}.summary.json"
    maps_output="${output}.maps.txt"
    /usr/local/bin/analyze_allocation_stacks.py \
      --pid "$pid" --input "$duration" --folded "$folded_output" \
      --bytes-folded "$bytes_folded_output" --summary "$summary_output" \
      --maps-output "$maps_output"
    if [ -n "$ready_file" ]; then
      mkdir -p "$(dirname "$ready_file")"
      printf '%s\n' "$pid" > "$ready_file"
    fi
    ;;
  *)
    echo "profile.sh: unknown tool '$tool' (expected perf, pyspy, node-cpu, node-heap, scheduler, offcpu or allocation-stacks)" >&2
    exit 1
    ;;
esac

/opt/FlameGraph/flamegraph.pl "$folded_output" > "$output"

top_output="${output}.top.txt"
/usr/local/bin/analyze_folded.py "$folded_output" > "$top_output"

echo "profile.sh: wrote $output, $folded_output, and $top_output" >&2
