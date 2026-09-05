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

case "$tool" in
  perf)
    # Service runtimes may create or replace worker threads after attachment.
    # Keep those descendants in the same profile instead of silently
    # producing an empty or main-thread-only flamegraph.
    if [ -n "$ready_file" ]; then
      mkdir -p "$(dirname "$ready_file")"
      printf '%s\n' "$pid" > "$ready_file"
    fi
    perf record "${perf_event_args[@]}" "${perf_sampling_args[@]}" --inherit -g -p "$pid" -o /tmp/perf.data -- sleep "$duration"
    perf script -i /tmp/perf.data > /tmp/perf.script
    /opt/FlameGraph/stackcollapse-perf.pl /tmp/perf.script > "$folded_output"
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
    if [ -n "$ready_file" ]; then
      mkdir -p "$(dirname "$ready_file")"
      printf '%s\n' "$pid" > "$ready_file"
    fi
    # Capture the complete user/kernel call chain at every blocking context
    # switch. Paired scheduler profiles provide durations; these stacks provide
    # the missing futex/epoll/CQ/mutex call-site attribution.
    perf record -e sched:sched_switch --inherit -g -p "$pid" \
      -o /tmp/perf-offcpu.data -- sleep "$duration"
    perf script -i /tmp/perf-offcpu.data > /tmp/perf-offcpu.script
    /opt/FlameGraph/stackcollapse-perf.pl /tmp/perf-offcpu.script > "$folded_output"
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
