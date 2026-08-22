import http from "k6/http";
import { check } from "k6";
import { Counter } from "k6/metrics";

const duration = __ENV.BENCHMARK_DURATION || "20s";
const resultFile = __ENV.BENCHMARK_RESULT_FILE || "/results/k6.json";
const target = __ENV.BENCHMARK_TARGET ||
  "http://orderservice:9091/v1/processorder";
const method = (__ENV.BENCHMARK_METHOD || "POST").toUpperCase();
const vus = Number.parseInt(__ENV.BENCHMARK_VUS || "32", 10);
const mode = __ENV.BENCHMARK_MODE || "closed";
const targetRate = Number.parseInt(__ENV.BENCHMARK_RATE || "0", 10);
const preAllocatedVUs = Number.parseInt(
  __ENV.BENCHMARK_PRE_ALLOCATED_VUS || "128",
  10,
);
const maxVUs = Number.parseInt(__ENV.BENCHMARK_MAX_VUS || "4096", 10);
const serviceCores = Number.parseInt(__ENV.BENCHMARK_SERVICE_CORES || "0", 10);
const loadgenCores = Number.parseInt(__ENV.BENCHMARK_LOADGEN_CORES || "0", 10);
const buildType = __ENV.BENCHMARK_BUILD_TYPE || "unknown";
const scenario = __ENV.BENCHMARK_SCENARIO || "process_order_out_of_stock";
const expectedOutcome = __ENV.BENCHMARK_EXPECTED_OUTCOME || "success";
const expectedBusinessStatus = __ENV.BENCHMARK_EXPECTED_BUSINESS_STATUS || "";
const requestTimeout = __ENV.BENCHMARK_REQUEST_TIMEOUT || "60s";
const gracefulStop = __ENV.BENCHMARK_GRACEFUL_STOP || "5s";
const expectedStatus = Number.parseInt(
  __ENV.BENCHMARK_EXPECTED_STATUS || "200",
  10,
);
const startedIterations = new Counter("profiling_started_iterations");
const completedIterations = new Counter("profiling_completed_iterations");

const commonOptions = {
  discardResponseBodies: expectedBusinessStatus === "",
  noConnectionReuse: false,
  noVUConnectionReuse: false,
  summaryTrendStats: ["avg", "med", "p(90)", "p(95)", "p(99)", "max"],
  thresholds: {
    checks: ["rate==1"],
    ...(expectedOutcome === "overload"
      ? {}
      : { dropped_iterations: ["count==0"] }),
    ...(expectedOutcome === "transport-timeout"
      ? {}
      : { http_req_failed: ["rate==0"] }),
  },
};

export const options = mode === "arrival-rate"
  ? {
      ...commonOptions,
      scenarios: {
        capacity: {
          executor: "constant-arrival-rate",
          rate: targetRate,
          timeUnit: "1s",
          duration,
          preAllocatedVUs,
          maxVUs,
          gracefulStop,
        },
      },
    }
  : {
      ...commonOptions,
      scenarios: {
        profile: {
          executor: "constant-vus",
          vus,
          duration,
          gracefulStop,
        },
      },
    };

const payload = JSON.stringify({
  customer_id: "benchmark-customer",
  items: [
    {
      item_id: "benchmark-item",
      // A missing SKU keeps every request on the same business path. Using
      // SKU-001 would exhaust the in-memory stock during the benchmark.
      sku: "BENCHMARK-MISSING-SKU",
      quantity: 1,
      unit_price: 799.0,
    },
  ],
});

const params = {
  headers: {
    "Content-Type": "application/json",
  },
  timeout: requestTimeout,
  tags: {
    scenario,
    build_type: buildType,
  },
};

export default function () {
  startedIterations.add(1);
  const response = method === "GET"
    ? http.get(target, params)
    : http.post(target, payload, params);
  check(response, expectedOutcome === "transport-timeout"
    ? {
        "request is cancelled by client timeout": (value) =>
          value.status === 0 && /timeout/i.test(value.error || ""),
      }
    : {
        "HTTP status is expected": (value) => value.status === expectedStatus,
        ...(expectedBusinessStatus === ""
          ? {}
          : {
              "business status is expected": (value) => {
                try {
                  return value.json("status") === expectedBusinessStatus;
                } catch (_) {
                  return false;
                }
              },
            }),
      });
  completedIterations.add(1);
}

export function handleSummary(data) {
  const requests = data.metrics.http_reqs?.values || {};
  const durationValues = data.metrics.http_req_duration?.values || {};
  const failed = data.metrics.http_req_failed?.values || {};
  const checks = data.metrics.checks?.values || {};
  const dropped = data.metrics.dropped_iterations?.values || {};
  const iterations = data.metrics.iterations?.values || {};
  const started = data.metrics.profiling_started_iterations?.values || {};
  const completed = data.metrics.profiling_completed_iterations?.values || {};
  const scheduledIterations =
    (iterations.count || requests.count || 0) + (dropped.count || 0);
  const summary = {
    scenario,
    expected_outcome: expectedOutcome,
    expected_business_status: expectedBusinessStatus,
    request_timeout: requestTimeout,
    graceful_stop: gracefulStop,
    build_type: buildType,
    mode,
    duration,
    vus,
    service_cores: serviceCores,
    loadgen_cores: loadgenCores,
    target_rate: targetRate,
    request_count: requests.count || 0,
    requests_per_second: requests.rate || 0,
    test_run_duration_ms: data.state?.testRunDurationMs || 0,
    iteration_count: iterations.count || 0,
    started_iterations: started.count || 0,
    completed_iterations: completed.count || 0,
    interrupted_iterations: Math.max(
      0,
      (started.count || 0) - (completed.count || 0),
    ),
    dropped_iterations: dropped.count || 0,
    dropped_rate: scheduledIterations > 0
      ? (dropped.count || 0) / scheduledIterations
      : 0,
    error_rate: 1 - (checks.rate ?? 1),
    transport_error_rate: failed.rate || 0,
    latency_ms: {
      avg: durationValues.avg || 0,
      p50: durationValues.med || 0,
      p90: durationValues["p(90)"] || 0,
      p95: durationValues["p(95)"] || 0,
      p99: durationValues["p(99)"] || 0,
      max: durationValues.max || 0,
    },
  };
  return {
    [resultFile]: JSON.stringify(summary, null, 2) + "\n",
    stdout:
      `requests=${summary.request_count} ` +
      `rate=${summary.requests_per_second.toFixed(2)}/s ` +
      `target=${summary.target_rate}/s ` +
      `dropped=${summary.dropped_iterations} ` +
      `p95=${summary.latency_ms.p95.toFixed(2)}ms ` +
      `p99=${summary.latency_ms.p99.toFixed(2)}ms ` +
      `errors=${(summary.error_rate * 100).toFixed(4)}%\n`,
  };
}
