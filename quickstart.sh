#!/usr/bin/env bash
set -euo pipefail

# Clones the sibling repos this profiling project needs (if missing) and
# runs it with sensible defaults. Intended entry point for someone who just
# cloned `profiling` and wants to capture flamegraphs without first learning
# the multi-repo layout.
#
# Usage:
#   ./quickstart.sh                # clone what's missing, then profile all implementations
#   ./quickstart.sh --clone-only   # only clone/update sibling repos, don't run
#   ./quickstart.sh --profile current  # profile generated graph with pools
#   ./quickstart.sh --skip-git-mirror-refresh  # trust cached Git revisions
#   ./quickstart.sh -- call-semantics   # same pooled profile, benchmark-style alias
#
# Anything after the flags is forwarded to `make profile` (via ARGS) in
# profiling, e.g.:
#   ./quickstart.sh -- --language rust --duration 10s --vus 64

ORG="https://github.com/gorundebug"
PROFILING_ROOT="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
if [ -n "${DEPENDENCIES_DIR:-}" ]; then
  MANAGED_DEPENDENCIES=0
else
  DEPENDENCIES_DIR="$PROFILING_ROOT/.dependencies"
  MANAGED_DEPENDENCIES=1
fi
EXAMPLE_PROFILE="${EXAMPLE_PROFILE:-function-call}"
PROFILE_EXPLICIT=0

REPOS=(goexample cppexample cppboostexample pyexample rustexample tsexample servicelib cppservicelib cppboostservicelib pyservicelib rustservicelib tsservicelib servicegen)

export GIT_HTTP_LOW_SPEED_LIMIT=${DEPENDENCY_GIT_LOW_SPEED_LIMIT:-1024}
export GIT_HTTP_LOW_SPEED_TIME=${DEPENDENCY_GIT_LOW_SPEED_TIME:-30}

retry_dependency_command() {
  attempt=1
  attempts=${DEPENDENCY_COMMAND_RETRY_ATTEMPTS:-10}
  until "$@"; do
    if [ "$attempt" -ge "$attempts" ]; then
      echo "dependency command failed after $attempts attempts: $*" >&2
      return 1
    fi
    delay=$((attempt * 2))
    echo "dependency command failed; retrying same route in ${delay}s ($attempt/$attempts): $*" >&2
    sleep "$delay"
    attempt=$((attempt + 1))
  done
}

clone_only=0
refresh_git_mirror=1
while [ "$#" -gt 0 ]; do
  case "$1" in
    --clone-only)
      clone_only=1
      shift
      ;;
    --skip-git-mirror-refresh)
      refresh_git_mirror=0
      shift
      ;;
    --dependencies-dir)
      if [ "$#" -lt 2 ]; then
        echo "--dependencies-dir requires a path" >&2
        exit 2
      fi
      DEPENDENCIES_DIR="$2"
      MANAGED_DEPENDENCIES=0
      shift 2
      ;;
    --dependencies-dir=*)
      DEPENDENCIES_DIR="${1#*=}"
      MANAGED_DEPENDENCIES=0
      shift
      ;;
    --profile)
      if [ "$#" -lt 2 ]; then
        echo "--profile requires function-call or current" >&2
        exit 2
      fi
      EXAMPLE_PROFILE="$2"
      PROFILE_EXPLICIT=1
      shift 2
      ;;
    --profile=*)
      EXAMPLE_PROFILE="${1#*=}"
      PROFILE_EXPLICIT=1
      shift
      ;;
    --)
      shift
      break
      ;;
    *)
      break
      ;;
  esac
done

# Keep the public quickstart spelling aligned with benchmarks/conformance.
# `call-semantics` is a quickstart mode selector, not an examples/run.py
# positional argument.
if [ "${1:-}" = "call-semantics" ]; then
  if [ "$PROFILE_EXPLICIT" -eq 1 ] && [ "$EXAMPLE_PROFILE" != "current" ]; then
    echo "call-semantics conflicts with --profile $EXAMPLE_PROFILE" >&2
    exit 2
  fi
  EXAMPLE_PROFILE="current"
  shift
fi

case "$EXAMPLE_PROFILE" in
  function-call|current) ;;
  *)
    echo "Unsupported profile '$EXAMPLE_PROFILE'; expected function-call or current" >&2
    exit 2
    ;;
esac

mkdir -p "$DEPENDENCIES_DIR"
DEPENDENCIES_DIR="$(CDPATH= cd -- "$DEPENDENCIES_DIR" && pwd)"
export DEPENDENCIES_DIR
export UPDATE_MANAGED_DEPENDENCIES="$MANAGED_DEPENDENCIES"

echo "==> Checking prerequisites"
missing=0
for tool in git docker python3 curl; do
  if ! command -v "$tool" >/dev/null 2>&1; then
    echo "  missing: $tool" >&2
    missing=1
  fi
done
if ! docker compose version >/dev/null 2>&1; then
  echo "  missing: docker compose plugin (needs Docker Desktop or the compose-plugin package)" >&2
  missing=1
fi
if [ "$missing" -ne 0 ]; then
  echo "Install the missing tools above and re-run." >&2
  exit 1
fi
echo "  git, docker, docker compose, python3, curl: OK"

if [ -n "${DEPENDENCY_PROXY_DIR:-}" ]; then
  proxy_host="${DEPENDENCY_PROXY_HOST:-localhost}"
  git_mirror_port="${DEPENDENCY_GIT_MIRROR_PORT:-18084}"
  bootstrap_git_mirror="http://$proxy_host:$git_mirror_port/cgi-bin/git"
  export GIT_CONFIG_COUNT=2
  export GIT_CONFIG_KEY_0="url.$bootstrap_git_mirror/github.com/.insteadOf"
  export GIT_CONFIG_VALUE_0=https://github.com/
  export GIT_CONFIG_KEY_1="url.$bootstrap_git_mirror/gitlab.com/.insteadOf"
  export GIT_CONFIG_VALUE_1=https://gitlab.com/
  if [ "$refresh_git_mirror" -eq 1 ]; then
    echo "==> Refreshing managed Git mirrors before resolving revisions"
    mirror_refresh_repositories=$(printf 'github.com/gorundebug/%s.git\n' "${REPOS[@]}")
    curl --fail-with-body --show-error --silent \
      --connect-timeout 15 --max-time 600 \
      --retry 2 --retry-delay 2 --retry-max-time 600 --retry-all-errors \
      --request POST \
      --data-binary "$mirror_refresh_repositories" \
      "$bootstrap_git_mirror/__servicegen_refresh"
  else
    echo "==> Trusting cached Git mirror revisions (--skip-git-mirror-refresh)"
  fi
fi

echo "==> Preparing repositories in $DEPENDENCIES_DIR"
for repo in "${REPOS[@]}"; do
  dir="$DEPENDENCIES_DIR/$repo"
  if [ -d "$dir/.git" ]; then
    if [ "$MANAGED_DEPENDENCIES" -eq 1 ]; then
      echo "  $repo: updating managed main checkout"
      "$PROFILING_ROOT/scripts/update-managed-checkout.sh" "$dir"
    else
      echo "  $repo: external checkout, leaving unchanged"
    fi
    continue
  fi
  echo "  cloning $repo"
  retry_dependency_command git clone --branch main --single-branch --depth 1 \
    "$ORG/$repo.git" "$dir"
done

if [ -n "${DEPENDENCY_PROXY_DIR:-}" ]; then
  proxy_script="$DEPENDENCIES_DIR/goexample/scripts/dependency-cache.generated.sh"
  if [ ! -x "$proxy_script" ]; then
    echo "Shared dependency proxy requested, but $proxy_script is missing" >&2
    exit 1
  fi
  export DEPENDENCY_PROXY_CLIENT_HOST="${DEPENDENCY_PROXY_HOST:-localhost}"
  eval "$("$proxy_script" env)"
  proxy_resolver="$DEPENDENCIES_DIR/cppexample/scripts/dependency-proxy-env.generated.sh"
  if [ ! -f "$proxy_resolver" ]; then
    echo "Shared dependency proxy requested, but $proxy_resolver is missing" >&2
    exit 1
  fi
  source "$proxy_resolver"
  export DEPENDENCY_REAL_DOCKER="$(command -v docker)"
  proxy_bin="$(mktemp -d "${TMPDIR:-/tmp}/servicelib-proxy-bin.XXXXXX")"
  ln -s "$DEPENDENCIES_DIR/cppexample/scripts/docker-dependency-proxy.generated.sh" "$proxy_bin/docker"
  export PATH="$proxy_bin:$PATH"
  echo "==> Using shared dependency proxy (host: $DEPENDENCY_PROXY_CLIENT_HOST, containers: ${DEPENDENCY_PROXY_DOCKER_HOST:-host.docker.internal})"
fi

echo "==> Restoring pinned native profiling projects"
python3 "$PROFILING_ROOT/examples/run.py" --fetch-native

# goexample/cppexample/cppboostexample/pyexample each split their service/module code into
# further separate repos (orderservice, inventoryservice, order_service_api,
# inventory_service_api, model), restored via their own clone.generated.sh.
# Rust keeps the equivalent code force-added inside rustexample itself, so it
# needs no extra step.
echo "==> Restoring each example's own service/module repos"
for example in goexample cppexample cppboostexample pyexample tsexample; do
  script="$DEPENDENCIES_DIR/$example/clone.generated.sh"
  if [ -f "$script" ]; then
    echo "  $example"
    (cd "$DEPENDENCIES_DIR/$example" && bash clone.generated.sh)
  fi
done

if [ "$clone_only" -eq 1 ]; then
  echo "==> --clone-only requested, not running the profiler"
  exit 0
fi

SOURCE_DEPENDENCIES_DIR="$DEPENDENCIES_DIR"
PROFILE_TEMP_DIR=""
cleanup_profile() {
  if [ -n "$PROFILE_TEMP_DIR" ]; then
    python3 -c 'import shutil, sys; shutil.rmtree(sys.argv[1], ignore_errors=True)' \
      "$PROFILE_TEMP_DIR"
  fi
}
trap cleanup_profile EXIT INT TERM

PROFILE_MARKER="$PROFILING_ROOT/examples/.artifacts/example-profile.txt"
PREVIOUS_PROFILE="$(cat "$PROFILE_MARKER" 2>/dev/null || true)"
if [ -d "$PROFILING_ROOT/examples/.artifacts" ] && [ "$PREVIOUS_PROFILE" != "$EXAMPLE_PROFILE" ]; then
  DISPLAY_PREVIOUS_PROFILE="${PREVIOUS_PROFILE:-unknown}"
  echo "==> Profile changed ($DISPLAY_PREVIOUS_PROFILE -> $EXAMPLE_PROFILE); clearing incompatible profiling artifacts"
  python3 -c 'import shutil, sys; shutil.rmtree(sys.argv[1], ignore_errors=True)' \
    "$PROFILING_ROOT/examples/.artifacts"
fi
mkdir -p "$PROFILING_ROOT/examples/.artifacts"
printf '%s\n' "$EXAMPLE_PROFILE" > "$PROFILE_MARKER"
export EXAMPLE_PROFILE="$EXAMPLE_PROFILE"

PROFILE_TEMP_DIR="$(mktemp -d "${TMPDIR:-/tmp}/servicelib-profiling-${EXAMPLE_PROFILE}.XXXXXX")"
PROFILE_WORKSPACE="$PROFILE_TEMP_DIR/workspace"
echo "==> Preparing disposable '$EXAMPLE_PROFILE' generated examples"
python3 "$PROFILING_ROOT/profile_workspace.py" \
  --source-root "$SOURCE_DEPENDENCIES_DIR" \
  --workspace "$PROFILE_WORKSPACE" \
  --profile "$EXAMPLE_PROFILE"
DEPENDENCIES_DIR="$PROFILE_WORKSPACE"
export DEPENDENCIES_DIR

echo "==> Profiling graph profile '$EXAMPLE_PROFILE'"
cd "$PROFILING_ROOT"
make profile ARGS="--graph-profile $EXAMPLE_PROFILE $*"
