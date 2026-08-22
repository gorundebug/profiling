#!/bin/sh
set -eu

if [ -z "${SERVICEGEN_REAL_DOCKER:-}" ]; then
  echo "SERVICEGEN_REAL_DOCKER is not set" >&2
  exit 2
fi

if [ -n "${SERVICEGEN_DEPENDENCY_PROXY_DIR:-}" ]; then
  host=${SERVICEGEN_DEPENDENCY_PROXY_DOCKER_HOST:-${SERVICEGEN_NEXUS_DOCKER_HOST:-host.docker.internal}}
  port=${SERVICEGEN_NEXUS_PORT:-18081}
  base="http://$host:$port/repository"
  export GOPROXY="$base/go-proxy/,direct"
  export NPM_CONFIG_REGISTRY="$base/npm-proxy/"
  export PIP_INDEX_URL="$base/pypi-proxy/simple"
  export PIP_TRUSTED_HOST="$host"
  export UV_INDEX_URL="$base/pypi-proxy/simple"
  export CARGO_REGISTRIES_CRATES_IO_INDEX="sparse+$base/cargo-proxy/"
  export SERVICEGEN_MAVEN_CENTRAL_URL="$base/maven-central"
  export SERVICEGEN_HELM_PROMETHEUS_URL="$base/helm-prometheus"
  export SERVICEGEN_HELM_GRAFANA_URL="$base/helm-grafana"
  export SERVICEGEN_HELM_OPENTELEMETRY_URL="$base/helm-opentelemetry"
  export SERVICEGEN_HELM_JAEGER_URL="$base/helm-jaeger"
  export SERVICEGEN_HELM_REDPANDA_URL="$base/helm-redpanda"
fi

exec "$SERVICEGEN_REAL_DOCKER" "$@"
