#!/bin/bash
# Run an offline forecast batch on the GPU box.
set -e
cd /root/ict
source venv_kronos/bin/activate
export http_proxy=http://127.0.0.1:7890 https_proxy=http://127.0.0.1:7890

python run_forecast.py --csv btc_15m.csv --out cache_btc_kronos.csv \
  --repo /root/ict/Kronos --device cuda:0 --n-samples "${N_SAMPLES:-8}" \
  --step "${STEP:-48}" --context 512 --horizon 12
