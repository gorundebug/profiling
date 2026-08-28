from __future__ import annotations

import argparse
import json
import tempfile
import unittest
from unittest import mock
from pathlib import Path
from typing import Any

import node_inspector_profile
import run as profiling


class FakeInspector:
    def __init__(self, value: dict[str, Any]) -> None:
        self.value = value

    def call(self, method: str, params: dict[str, Any] | None = None) -> Any:
        self.method = method
        self.params = params
        return {"result": {"value": self.value}}


class FakeHeapSnapshotInspector:
    def call(self, method: str, params: dict[str, Any] | None = None) -> Any:
        self.collect_method = method
        return {}

    def call_stream(
        self,
        method: str,
        *,
        params: dict[str, Any] | None = None,
        event_method: str | None = None,
        on_event: Any | None = None,
    ) -> Any:
        self.snapshot_method = method
        self.snapshot_params = params
        self.event_method = event_method
        assert on_event is not None
        on_event({"chunk": '{"snapshot":'})
        on_event({"chunk": '{"meta":{}}}'})
        return {}


class TypeScriptProfilingTest(unittest.TestCase):
    def test_profiler_image_trusts_http_dependency_proxy(self) -> None:
        environment = {
            "DEPENDENCY_PROXY_DIR": "/cache",
            "DEPENDENCY_PROXY_HOST": "localhost",
            "DEPENDENCY_PROXY_DOCKER_HOST": "host.docker.internal",
            "PIP_INDEX_URL": "http://localhost:18081/repository/pypi-proxy/simple",
            "PIP_TRUSTED_HOST": "localhost",
        }
        with mock.patch.object(profiling, "run") as run:
            profiling.build_profiler_image(environment)
        command = run.call_args.args[0]
        self.assertIn(
            "DEPENDENCY_DOCKER_REGISTRY=localhost:18083",
            command,
        )
        self.assertIn(
            "PIP_INDEX_URL=http://host.docker.internal:18081/repository/pypi-proxy/simple",
            command,
        )
        self.assertIn("PIP_TRUSTED_HOST=host.docker.internal", command)

    def test_disabled_kafka_config_keeps_required_connector_fields(self) -> None:
        self.assertEqual(
            profiling.disabled_kafka_connector_values(),
            {
                "orderEventsBrokers": "redpanda:9092",
                "orderEventsPassword": "",
                "orderEventsSaslMechanism": "SCRAM-SHA-512",
                "orderEventsSecurityProtocol": "PLAINTEXT",
                "orderEventsUsername": "",
            },
        )

    def test_language_matrix_and_environment(self) -> None:
        languages = {language.name: language for language in profiling.LANGUAGES}
        self.assertEqual(
            set(languages),
            {
                "go", "go-native", "cpp", "cpp-native", "cppboost",
                "cppboost-native", "python", "python-native", "rust",
                "rust-native", "typescript", "typescript-native",
            },
        )
        self.assertEqual(languages["typescript"].tool, "node-cpu")
        self.assertEqual(languages["typescript-native"].tool, "node-cpu")
        args = argparse.Namespace(
            cores=2,
            coroutine_diagnostics=False,
            duration="20s",
            loadgen_cores=6,
            vus=256,
        )
        environment = profiling.environment(args, languages["typescript"])
        self.assertEqual(environment["SERVICEGEN_RUNTIME_STRIP"], "OFF")
        self.assertEqual(environment["PROFILING_VUS"], "256")
        self.assertEqual(
            environment["TSSERVICELIB_SOURCE_CONTEXT"],
            str(profiling.ROOT / "tsservicelib"),
        )
        overlay = languages["typescript"].overlay.read_text()
        self.assertIn(
            "ORDER_PROCESSED_ENABLED: "
            "${PROFILING_ORDER_PROCESSED_ENABLED:-false}",
            overlay,
        )
        self.assertIn('SERVICELIB_NOOP_METRICS: "0"', overlay)

    def test_boost_uses_pinned_dependency_contexts(self) -> None:
        languages = {language.name: language for language in profiling.LANGUAGES}
        args = argparse.Namespace(
            cores=2,
            coroutine_diagnostics=False,
            duration="20s",
            loadgen_cores=6,
            vus=256,
        )
        for name in ("cppboost", "cppboost-native"):
            environment = profiling.environment(args, languages[name])
            self.assertEqual(
                environment["SERVICEGEN_GRPC_SOURCE_CONTEXT"],
                "https://github.com/grpc/grpc.git#v1.71.0",
            )
            self.assertEqual(
                environment["SERVICEGEN_ASIO_GRPC_SOURCE_CONTEXT"],
                "https://github.com/Tradias/asio-grpc.git#v3.5.0",
            )

        with mock.patch.dict(
            profiling.os.environ,
            {
                "SERVICEGEN_GRPC_SOURCE_CONTEXT": "/cache/grpc-src",
                "SERVICEGEN_ASIO_GRPC_SOURCE_CONTEXT": "/cache/asio-grpc-src",
            },
            clear=False,
        ):
            environment = profiling.environment(args, languages["cppboost"])
        self.assertEqual(
            environment["SERVICEGEN_GRPC_SOURCE_CONTEXT"], "/cache/grpc-src"
        )
        self.assertEqual(
            environment["SERVICEGEN_ASIO_GRPC_SOURCE_CONTEXT"],
            "/cache/asio-grpc-src",
        )

    def test_cpp_normal_profile_uses_kafka_free_override(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch.object(profiling, "ARTIFACTS", Path(directory)):
                profiling.prepare_cpp_configs(2)
            output = Path(directory) / "cpp-config"
            variables = (output / "orderservice.config_vars.yaml").read_text()
            override = (output / "orderservice.overrides.yaml").read_text()
            self.assertIn(
                'orderServiceConfigOverridePath: '
                '"/profiling-config/orderservice.overrides.yaml"',
                variables,
            )
            self.assertIn("orderProcessedEnabled: false", variables)
            self.assertEqual(
                override,
                "streams:\n  publishOrderProcessed:\n    enabled: false\n",
            )

    def test_runtime_metrics_parser_accepts_prometheus_labels(self) -> None:
        parsed = profiling.parse_runtime_metrics(
            '# HELP runtime_active_work work\n'
            'runtime_active_work{service="orderservice"} 7\n'
            'runtime_worker_utilization{service="orderservice"} 0.75\n'
            'unrelated_total 1\n'
        )
        self.assertEqual(
            parsed,
            {"runtime_active_work": 7.0, "runtime_worker_utilization": 0.75},
        )

    def test_load_result_validation_rejects_errors_and_drops(self) -> None:
        language = next(
            language for language in profiling.LANGUAGES if language.name == "typescript"
        )
        valid = {
            "scenario": "process_order_out_of_stock",
            "duration": "20s",
            "request_count": 10,
            "error_rate": 0,
            "dropped_iterations": 0,
        }
        with tempfile.TemporaryDirectory() as directory:
            result = Path(directory) / "load.json"
            result.write_text(json.dumps(valid))
            profiling.validate_load_result(result, language, "20s")
            for field, value in (("error_rate", 0.1), ("dropped_iterations", 1)):
                result.write_text(json.dumps({**valid, field: value}))
                with self.assertRaises(RuntimeError):
                    profiling.validate_load_result(result, language, "20s")

    def test_load_result_validation_rejects_quota_mismatch(self) -> None:
        language = next(
            language for language in profiling.LANGUAGES if language.name == "typescript"
        )
        result_value = {
            "scenario": "process_order_out_of_stock",
            "build_type": "Release",
            "duration": "20s",
            "vus": 256,
            "service_cores": 2,
            "loadgen_cores": 6,
            "request_count": 10,
            "error_rate": 0,
            "dropped_iterations": 0,
        }
        expected = {
            "PROFILING_VUS": "256",
            "PROFILING_SERVICE_CORES": "2",
            "PROFILING_LOADGEN_CORES": "6",
        }
        with tempfile.TemporaryDirectory() as directory:
            result = Path(directory) / "load.json"
            result.write_text(json.dumps(result_value))
            profiling.validate_load_result(result, language, "20s", expected)
            result.write_text(json.dumps({**result_value, "service_cores": 4}))
            with self.assertRaisesRegex(RuntimeError, "metadata differs"):
                profiling.validate_load_result(result, language, "20s", expected)

    def test_failure_scenarios_require_their_observed_outcome(self) -> None:
        language = next(
            language
            for language in profiling.LANGUAGES
            if language.name == "typescript"
        )
        base = {
            "build_type": "Release",
            "duration": "20s",
            "vus": 256,
            "service_cores": 2,
            "loadgen_cores": 6,
            "request_count": 10,
            "error_rate": 0,
            "transport_error_rate": 0,
            "dropped_iterations": 0,
        }
        with tempfile.TemporaryDirectory() as directory:
            result = Path(directory) / "load.json"
            cancellation = {
                "PROFILING_SCENARIO": "process_order_cancellation",
                "PROFILING_EXPECTED_OUTCOME": "transport-timeout",
                "PROFILING_VUS": "256",
                "PROFILING_SERVICE_CORES": "2",
                "PROFILING_LOADGEN_CORES": "6",
            }
            result.write_text(
                json.dumps(
                    {
                        **base,
                        "scenario": "process_order_cancellation",
                        "transport_error_rate": 1,
                    }
                )
            )
            profiling.validate_load_result(
                result, language, "20s", cancellation
            )
            result.write_text(
                json.dumps({**base, "scenario": "process_order_cancellation"})
            )
            with self.assertRaisesRegex(RuntimeError, "no client timeouts"):
                profiling.validate_load_result(
                    result, language, "20s", cancellation
                )

            overload = {
                "PROFILING_SCENARIO": "process_order_overload",
                "PROFILING_EXPECTED_OUTCOME": "overload",
                "PROFILING_VUS": "256",
                "PROFILING_SERVICE_CORES": "2",
                "PROFILING_LOADGEN_CORES": "6",
            }
            result.write_text(
                json.dumps(
                    {
                        **base,
                        "scenario": "process_order_overload",
                        "dropped_iterations": 2,
                    }
                )
            )
            profiling.validate_load_result(result, language, "20s", overload)

    def test_non_normal_artifacts_do_not_overwrite_normal_profiles(self) -> None:
        normal = argparse.Namespace(scenario="normal")
        timeout = argparse.Namespace(scenario="timeout")
        self.assertEqual(
            profiling.scenario_artifact_name(normal, "typescript.load.json"),
            "typescript.load.json",
        )
        self.assertEqual(
            profiling.scenario_artifact_name(timeout, "typescript.load.json"),
            "typescript.load.timeout.json",
        )

    def test_kafka_recovery_must_finish_inside_measured_window(self) -> None:
        valid = [
            {"event": "redpanda_stopped", "elapsed_seconds": 2.0},
            {"event": "redpanda_healthy", "elapsed_seconds": 4.5},
        ]
        profiling.validate_recovery_timeline(valid, "6s")
        with self.assertRaisesRegex(RuntimeError, "after the measured"):
            profiling.validate_recovery_timeline(valid, "4s")

    def test_node_profiles_produce_weighted_folded_stacks(self) -> None:
        cpu = {
            "nodes": [
                {
                    "id": 1,
                    "callFrame": {"functionName": "root", "url": "file:///root.ts"},
                    "children": [2],
                },
                {
                    "id": 2,
                    "callFrame": {"functionName": "work", "url": "file:///work.ts"},
                },
            ],
            "samples": [2],
            "timeDeltas": [1000],
        }
        self.assertEqual(len(node_inspector_profile.cpu_folded(cpu)), 1)
        self.assertTrue(node_inspector_profile.cpu_folded(cpu)[0].endswith(" 1000"))

        heap = {
            "head": {
                "id": 1,
                "callFrame": {"functionName": "root", "url": "file:///root.ts"},
                "children": [
                    {
                        "id": 2,
                        "callFrame": {"functionName": "allocate", "url": "file:///work.ts"},
                    }
                ],
            },
            "samples": [{"nodeId": 2, "size": 4096}],
        }
        self.assertTrue(node_inspector_profile.heap_folded(heap)[0].endswith(" 4096"))

    def test_node_heap_snapshot_uses_public_streaming_inspector_api(self) -> None:
        inspector = FakeHeapSnapshotInspector()
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "retained.heapsnapshot"
            node_inspector_profile.take_heap_snapshot(inspector, output)
            self.assertEqual(inspector.collect_method, "HeapProfiler.collectGarbage")
            self.assertEqual(inspector.snapshot_method, "HeapProfiler.takeHeapSnapshot")
            self.assertEqual(
                inspector.event_method, "HeapProfiler.addHeapSnapshotChunk"
            )
            self.assertEqual(
                json.loads(output.read_text()), {"snapshot": {"meta": {}}}
            )

    def test_heap_snapshot_summary_validates_schema_and_self_size(self) -> None:
        snapshot = {
            "snapshot": {
                "meta": {"node_fields": ["type", "name", "self_size"]}
            },
            "nodes": [0, 1, 64, 0, 2, 128],
        }
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "retained.heapsnapshot"
            output.write_text(json.dumps(snapshot))
            summary = profiling.summarize_heap_snapshot(output)
            self.assertEqual(summary["node_count"], 2)
            self.assertEqual(summary["self_size_bytes"], 192)

    def test_node_runtime_sample_retains_diagnostic_dimensions(self) -> None:
        inspector = FakeInspector(
            {
                "activeResources": 3,
                "activeResourcesByType": {"TCPSocketWrap": 2},
                "cpuSystemSeconds": 0.1,
                "cpuUserSeconds": 0.2,
                "eventLoopActiveSeconds": 0.2,
                "eventLoopIdleSeconds": 0.1,
                "eventLoopLagMaxSeconds": 0.003,
                "eventLoopLagMeanSeconds": 0.001,
                "eventLoopUtilization": 2 / 3,
                "gc": [],
                "memory": {"heapUsed": 1024},
            }
        )
        sample = node_inspector_profile.sample_runtime_diagnostics(inspector, 1.25)
        self.assertEqual(sample["elapsedSeconds"], 1.25)
        self.assertEqual(sample["activeResources"], 3)
        self.assertEqual(sample["memory"]["heapUsed"], 1024)
        self.assertEqual(inspector.method, "Runtime.evaluate")

    def test_node_summary_and_framework_native_report_are_quantitative(self) -> None:
        args = argparse.Namespace(
            cores=2,
            duration="20s",
            loadgen_cores=6,
            vus=256,
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            previous_artifacts = profiling.ARTIFACTS
            profiling.ARTIFACTS = root
            try:
                summaries = []
                for name, rate in (("typescript", 10_000), ("typescript-native", 12_000)):
                    language = profiling.Language(
                        name, root, root / "compose.yml", "node-cpu", "node", "node"
                    )
                    output = root / f"{name}.orderservice.flamegraph.svg"
                    runtime = Path(f"{output}.runtime.json")
                    runtime.write_text(
                        json.dumps(
                            {
                                "samples": [
                                    {
                                        "activeResources": 4,
                                        "cpuSystemSeconds": 0.1,
                                        "cpuUserSeconds": 0.5,
                                        "eventLoopLagMaxSeconds": 0.002,
                                        "eventLoopActiveSeconds": 0.5,
                                        "eventLoopIdleSeconds": 0.25,
                                        "eventLoopUtilization": 0.75,
                                        "gc": [{"durationSeconds": 0.001, "kind": "1"}],
                                        "memory": {"heapUsed": 1000, "rss": 2000},
                                    }
                                ]
                            }
                        )
                    )
                    load = root / f"{name}.load.json"
                    load.write_text(
                        json.dumps(
                            {
                                "request_count": 100,
                                "requests_per_second": rate,
                                "error_rate": 0,
                                "dropped_iterations": 0,
                                "latency_ms": {
                                    "p50": 3,
                                    "p95": 5,
                                    "p99": 8,
                                    "max": 12,
                                },
                            }
                        )
                    )
                    raw = Path(f"{output}.cpuprofile")
                    raw.write_text("{}")
                    summaries.append(
                        profiling.write_node_profile_summary(
                            language,
                            args,
                            "orderservice",
                            output,
                            runtime,
                            load,
                            raw,
                            mode="cpu",
                        )
                    )
                reports = profiling.write_typescript_comparison(summaries)
                self.assertEqual(len(reports), 2)
                report = (root / "typescript.framework-native.cpu.md").read_text()
                self.assertIn("| orderservice | typescript | 10000.00", report)
                self.assertIn("| orderservice | typescript-native | 12000.00", report)
            finally:
                profiling.ARTIFACTS = previous_artifacts

    def test_typescript_comparison_keeps_failure_scenario_separate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            previous_artifacts = profiling.ARTIFACTS
            profiling.ARTIFACTS = root
            try:
                summaries = []
                for language in ("typescript", "typescript-native"):
                    summary = root / f"{language}.orderservice.summary.json"
                    summary.write_text(
                        json.dumps(
                            {
                                "language": language,
                                "service": "orderservice",
                                "profile_mode": "cpu",
                                "scenario": "process_order_cancellation",
                                "requests_per_second": 100,
                                "latency_ms": {
                                    "p50": 1,
                                    "p95": 2,
                                    "p99": 3,
                                    "max": 4,
                                },
                                "runtime": {
                                    "cpu_user_seconds": 1,
                                    "cpu_system_seconds": 0,
                                    "event_loop_utilization_avg": 0.5,
                                    "event_loop_lag_max_seconds": 0.001,
                                    "event_loop_idle_seconds": 1,
                                    "gc_collections": 0,
                                    "gc_pause_seconds": 0,
                                },
                            }
                        )
                    )
                    summaries.append(summary)

                reports = profiling.write_typescript_comparison(summaries)

                self.assertEqual(len(reports), 2)
                self.assertTrue(
                    (root / "typescript.framework-native.cpu.cancellation.json").is_file()
                )
                self.assertFalse(
                    (root / "typescript.framework-native.cpu.json").exists()
                )
            finally:
                profiling.ARTIFACTS = previous_artifacts

    def test_scheduler_mode_dispatches_without_requiring_offcpu(self) -> None:
        language = next(
            language
            for language in profiling.LANGUAGES
            if language.name == "typescript-native"
        )
        args = argparse.Namespace(
            cores=2,
            coroutine_diagnostics=False,
            duration="1s",
            loadgen_cores=6,
            profile_kind=("scheduler",),
            vus=256,
            warmup="0s",
        )
        with (
            mock.patch.object(profiling, "run"),
            mock.patch.object(profiling, "wait_for_service"),
            mock.patch.object(
                profiling,
                "profile_scheduler_target",
                side_effect=[Path("orders.scheduler.json"), Path("inventory.scheduler.json")],
            ) as scheduler,
        ):
            outputs = profiling.profile_language(language, args)
        self.assertEqual(scheduler.call_count, 2)
        self.assertEqual(
            outputs,
            [Path("orders.scheduler.json"), Path("inventory.scheduler.json")],
        )


if __name__ == "__main__":
    unittest.main()
