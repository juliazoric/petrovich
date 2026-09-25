#!/usr/bin/env bash
# Скачивает модели в ./models. По умолчанию — Qwen3-4B (2.4 ГБ):
# она используется и как мозг для BRAIN=local, и как классификатор спама.
set -euo pipefail

DIR="$(cd "$(dirname "$0")/.." && pwd)/models"
mkdir -p "$DIR"

MODEL="${1:-Qwen3-4B-Instruct-2507-Q4_K_M.gguf}"
BASE="https://huggingface.co"

case "$MODEL" in
  Qwen3-4B-Instruct-2507-Q4_K_M.gguf)
    URL="$BASE/Qwen/Qwen3-4B-Instruct-2507-GGUF/resolve/main/Qwen3-4B-Instruct-2507-Q4_K_M.gguf" ;;
  Qwen2.5-0.5B-Instruct-Q4_K_M.gguf)
    URL="$BASE/Qwen/Qwen2.5-0.5B-Instruct-GGUF/resolve/main/qwen2.5-0.5b-instruct-q4_k_m.gguf" ;;
  *)
    echo "Не знаю такой модели: $MODEL" >&2
    echo "Известные: Qwen3-4B-Instruct-2507-Q4_K_M.gguf, Qwen2.5-0.5B-Instruct-Q4_K_M.gguf" >&2
    exit 1 ;;
esac

echo "→ $MODEL"
curl -fL --progress-bar -o "$DIR/$MODEL" "$URL"
echo "Готово: $DIR/$MODEL"
