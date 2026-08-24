#!/bin/sh
set -eu

if [ -z "${SERVICEGEN_REAL_DOCKER:-}" ]; then
  echo "SERVICEGEN_REAL_DOCKER is not set" >&2
  exit 2
fi

if [ -n "${SERVICEGEN_DEPENDENCY_PROXY_DIR:-}" ]; then
  host=${SERVICEGEN_DEPENDENCY_PROXY_DOCKER_HOST:-${SERVICEGEN_NEXUS_DOCKER_HOST:-host.docker.internal}}
  port=${SERVICEGEN_NEXUS_PORT:-18081}
  git_port=${SERVICEGEN_GIT_MIRROR_PORT:-18084}
  base="http://$host:$port/repository"
  git_mirror="http://$host:$git_port/cgi-bin/git"
  export GOPROXY="$base/go-proxy/"
  export NPM_CONFIG_REGISTRY="$base/npm-proxy/"
  export PIP_INDEX_URL="$base/pypi-proxy/simple"
  export PIP_TRUSTED_HOST="$host"
  export UV_INDEX_URL="$base/pypi-proxy/simple"
  export CARGO_REGISTRIES_CRATES_IO_INDEX="sparse+$base/cargo-proxy/"
  export SERVICEGEN_MAVEN_CENTRAL_URL="$base/maven-central"
  export SERVICEGEN_GITHUB_RAW_URL="$base/github-raw"
  export SERVICEGEN_GITLAB_RAW_URL="$base/gitlab-raw"
  export SERVICEGEN_APT_UBUNTU_ARCHIVE_URL="$base/apt-ubuntu-archive"
  export SERVICEGEN_APT_UBUNTU_SECURITY_URL="$base/apt-ubuntu-security"
  export SERVICEGEN_APT_UBUNTU_PORTS_URL="$base/apt-ubuntu-ports"
  export SERVICEGEN_APT_DEBIAN_URL="$base/apt-debian"
  export SERVICEGEN_APT_DEBIAN_SECURITY_URL="$base/apt-debian-security"
  export SERVICEGEN_HELM_PROMETHEUS_URL="$base/helm-prometheus"
  export SERVICEGEN_HELM_GRAFANA_URL="$base/helm-grafana"
  export SERVICEGEN_HELM_OPENTELEMETRY_URL="$base/helm-opentelemetry"
  export SERVICEGEN_HELM_JAEGER_URL="$base/helm-jaeger"
  export SERVICEGEN_HELM_REDPANDA_URL="$base/helm-redpanda"
  export SERVICEGEN_GIT_MIRROR_URL="$git_mirror"
  export GIT_CONFIG_COUNT=2
  export GIT_CONFIG_KEY_0="url.$git_mirror/github.com/.insteadOf"
  export GIT_CONFIG_VALUE_0=https://github.com/
  export GIT_CONFIG_KEY_1="url.$git_mirror/gitlab.com/.insteadOf"
  export GIT_CONFIG_VALUE_1=https://gitlab.com/
fi

if [ -n "${SERVICEGEN_DEPENDENCY_PROXY_DIR:-}" ] && [ "${1:-}" = "run" ]; then
  shift
  exec "$SERVICEGEN_REAL_DOCKER" run \
    --add-host "host.docker.internal:host-gateway" "$@"
fi

exec "$SERVICEGEN_REAL_DOCKER" "$@"
