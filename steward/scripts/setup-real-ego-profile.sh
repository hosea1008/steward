#!/usr/bin/env bash
# Install only the workstation bundle in its own DSH home.
set -euo pipefail
root="$(cd "$(dirname "$0")/.." && pwd)"
profile_home="${STEWARD_REAL_HOME:-$root/var/dsh/ego-real}"
mkdir -p "$profile_home/workspace"
DSH_HOME="$(cd "$profile_home" && pwd)"
export DSH_HOME
(cd "$root/dsh" && pnpm install --frozen-lockfile && pnpm run build)
cli="$root/dsh/node_modules/.bin/dsh"
cd "$DSH_HOME/workspace"
if [[ ! -f "$DSH_HOME/profiles/ego-real/package.json" ]]; then
  "$cli" --profile ego-real --from-default-profile headless --dump-default-config >/dev/null
fi
"$cli" plugin --profile ego-real add "$root/dsh/packages/dsh-bundle-ego-real"
