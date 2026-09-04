#!/usr/bin/env bash
set -euo pipefail

if [ "$#" -ne 1 ]; then
  echo "usage: $0 CHECKOUT" >&2
  exit 2
fi
checkout="$1"
if ! git -C "$checkout" diff --quiet || ! git -C "$checkout" diff --cached --quiet; then
  echo "managed checkout has local tracked changes: $checkout" >&2
  exit 1
fi
attempt=1
attempts=${DEPENDENCY_COMMAND_RETRY_ATTEMPTS:-10}
while ! git -C "$checkout" fetch --prune origin \
  +refs/heads/main:refs/remotes/origin/main; do
  if [ "$attempt" -ge "$attempts" ]; then
    echo "managed checkout fetch failed after $attempts attempts: $checkout" >&2
    exit 1
  fi
  delay=$((attempt * 2))
  echo "managed checkout fetch failed; retrying same route in ${delay}s ($attempt/$attempts): $checkout" >&2
  sleep "$delay"
  attempt=$((attempt + 1))
done
git -C "$checkout" checkout --quiet -B main origin/main
