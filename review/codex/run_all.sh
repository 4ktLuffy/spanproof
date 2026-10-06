#!/bin/sh
set -eu
PY=python
export LITELLM_LOCAL_MODEL_COST_MAP=True
for id in 01 02 03 04 05 06 07 08 09 10 15 16; do
 "$PY" "repro_SP-$id.py" > "result_SP-$id.json" 2> "err_SP-$id.txt"
done
for id in 04 11 12 13; do
 node --import @sentry/node/import "repro_SP-$id.cjs" > "result_JS-SP-$id.json" 2> "err_JS-SP-$id.txt"
done
for id in 02 03; do
 FULL=1 "$PY" "repro_SP-$id.py" > "control_SP-$id.json" 2> "control_err_SP-$id.txt"
done
FULL=1 node --import @sentry/node/import repro_SP-13.cjs > control_JS-SP-13.json 2> control_err_JS-SP-13.txt
NO_SENTRY=1 "$PY" repro_SP-15.py > control_SP-15.json 2> control_err_SP-15.txt
"$PY" verify.py
