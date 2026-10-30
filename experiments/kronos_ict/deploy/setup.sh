#!/bin/bash
# One-shot GPU box setup for the Kronos x ICT-Trader experiment.
# Usage:  bash deploy/setup.sh
set -e

PROXY="http://127.0.0.1:7890"   # Clash/mihomo mixed port on the dev box
WORK=/root/ict

# 1) proxy (needed for GitHub + HuggingFace)
export http_proxy="$PROXY" https_proxy="$PROXY"
echo "proxy: $PROXY"

# 2) clone Kronos
mkdir -p "$WORK"; cd "$WORK"
[ -d Kronos ] || git clone --depth 1 https://github.com/shiyu-coder/Kronos.git

# 3) venv + torch (cu124) + deps
[ -d venv_kronos ] || python3 -m venv venv_kronos
source venv_kronos/bin/activate
pip install --upgrade pip -q
echo "== installing torch (cu124) =="
pip install torch --index-url https://download.pytorch.org/whl/cu124
echo "== installing deps =="
pip install einops huggingface_hub safetensors tqdm pandas

echo "== verify =="
python -c "import torch; print('torch', torch.__version__, 'cuda', torch.cuda.is_available(), torch.cuda.get_device_name(0) if torch.cuda.is_available() else '')"
echo "== ALLDONE =="
