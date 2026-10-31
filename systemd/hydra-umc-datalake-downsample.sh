#!/bin/sh
# =============================================================================
# HYDRA-UMC-DATALAKE - Periodic downsampling call (run by the downsample unit)
# Copyright (C) 2026 JuanenRac (Electro Hobby 3D) <electrohobby3d@gmail.com>
# GPL-3.0-or-later - see LICENSE
# =============================================================================
# Calls POST /downsample/apply of the running data lake. DRY RUN BY DEFAULT: nothing is
# rolled up or deleted unless HYDRA_UMC_DATALAKE_DOWNSAMPLE_DRY_RUN is exactly "false".
#
# Environment (all optional; usually set in /etc/hydra-umc/datalake.env):
#   HYDRA_UMC_DATALAKE_URL                    base URL of the data lake (default http://127.0.0.1:8095)
#   HYDRA_UMC_DATALAKE_ADMIN_TOKEN            administration token (omitted from the call when empty)
#   HYDRA_UMC_DATALAKE_DOWNSAMPLE_DRY_RUN     "true" (default) or "false"; anything else is refused
#   HYDRA_UMC_DATALAKE_DOWNSAMPLE_MAX_PASSES  runs per call while the answer says "truncated" (default 1, 1 to 100;
#                                             a dry run is always one pass: it would repeat the same answer)
#   HYDRA_UMC_DATALAKE_DOWNSAMPLE_TIMEOUT     seconds curl may take per pass (default 300)
# The service itself must run with --enable-downsampling (otherwise the route answers 404 and this fails).
# The token travels on curl's standard input, never in the command line.
set -eu

url="${HYDRA_UMC_DATALAKE_URL:-http://127.0.0.1:8095}"
dry="${HYDRA_UMC_DATALAKE_DOWNSAMPLE_DRY_RUN-true}"
passes="${HYDRA_UMC_DATALAKE_DOWNSAMPLE_MAX_PASSES:-1}"
timeout="${HYDRA_UMC_DATALAKE_DOWNSAMPLE_TIMEOUT:-300}"

case "$dry" in
  true | false) ;;
  *) echo "HYDRA_UMC_DATALAKE_DOWNSAMPLE_DRY_RUN must be true or false (got: $dry)" >&2; exit 2 ;;
esac
case "$passes" in
  '' | *[!0-9]*) echo "HYDRA_UMC_DATALAKE_DOWNSAMPLE_MAX_PASSES must be an integer from 1 to 100" >&2; exit 2 ;;
esac
if [ "$passes" -lt 1 ] || [ "$passes" -gt 100 ]; then
  echo "HYDRA_UMC_DATALAKE_DOWNSAMPLE_MAX_PASSES must be an integer from 1 to 100" >&2; exit 2
fi
case "$timeout" in
  '' | *[!0-9]*) echo "HYDRA_UMC_DATALAKE_DOWNSAMPLE_TIMEOUT must be a positive integer" >&2; exit 2 ;;
esac
if [ "$timeout" -lt 1 ]; then
  echo "HYDRA_UMC_DATALAKE_DOWNSAMPLE_TIMEOUT must be a positive integer" >&2; exit 2
fi
if [ "$dry" = "true" ]; then
  passes=1
  echo "downsample: DRY RUN (nothing is written; set HYDRA_UMC_DATALAKE_DOWNSAMPLE_DRY_RUN=false to apply)"
else
  echo "downsample: APPLYING (rolled-up raw points are deleted)"
fi

pass=1
while :; do
  reply="$(
    {
      printf 'url = "%s/downsample/apply"\n' "$url"
      printf 'request = "POST"\n'
      printf 'header = "Content-Type: application/json"\n'
      if [ -n "${HYDRA_UMC_DATALAKE_ADMIN_TOKEN:-}" ]; then
        printf 'header = "Authorization: Bearer %s"\n' "$HYDRA_UMC_DATALAKE_ADMIN_TOKEN"
      fi
      printf 'data = "{\\"dryRun\\": %s}"\n' "$dry"
    } | curl --fail --silent --show-error --max-time "$timeout" --config -
  )"
  echo "downsample pass $pass: $reply"
  if [ "$pass" -ge "$passes" ]; then
    break
  fi
  case "$reply" in
    *'"truncated": true'*) pass=$((pass + 1)) ;;
    *) break ;;
  esac
done
