#!/usr/bin/env bash
# parity-ci.sh: the container checks (parity tests 2, 3, 4, 5, 6, 11, 13, 15, 16 + standby), on made-up data.
# One PASS/FAIL line per check, exit 1 on any FAIL. Inside the image: docker run --rm --entrypoint bash IMG
#   /opt/queue/bin/queue-image/parity-ci.sh   (or wherever parity_ci.py sits). Natively: QUEUE_CODE=DIR parity-ci.sh
set -u
here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
export QUEUE_CODE=${QUEUE_CODE:-/opt/queue/bin}
[ -d "$QUEUE_CODE" ] || { echo "FAIL parity-ci: no code dir $QUEUE_CODE" >&2; exit 1; }
exec "${PYTHON:-python3}" "$here/parity_ci.py"
