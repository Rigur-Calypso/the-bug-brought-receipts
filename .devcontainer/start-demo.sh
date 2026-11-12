#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")/.."
umask 077

[[ "${CODESPACES:-}" == true ]] || { echo 'Run this bootstrap inside GitHub Codespaces.'; exit 1; }
mkdir -p .local
chmod 700 .local

if [[ ! -x .venv/bin/python ]]; then
  python3 -m venv .venv
fi
.venv/bin/python -m pip install --disable-pip-version-check -r requirements-web.txt

export OPR_SAMPLE_REPOSITORY="$PWD/.local/museum-of-off-by-ones"
if [[ ! -d "$OPR_SAMPLE_REPOSITORY/.git" ]]; then
  git clone --no-tags https://github.com/Rigur-Calypso/museum-of-off-by-ones.git "$OPR_SAMPLE_REPOSITORY"
fi

runtime="$PWD/.local/ollama-0.40.16"
if [[ ! -x "$runtime/bin/ollama" ]]; then
  download="$(mktemp -d "$PWD/.local/ollama-download.XXXXXXXX")"
  trap 'rm -rf "$download"' EXIT
  curl --fail --silent --show-error --location --retry 2 \
    https://github.com/ollama/ollama/releases/download/v0.40.16/ollama-linux-amd64.tar.zst \
    --output "$download/ollama.tar.zst"
  printf '%s  %s\n' 87bfafb2acbc89b2e6a1be0a9043de6aa75fb450696f86bc426e65fc448f788d \
    "$download/ollama.tar.zst" | sha256sum --check
  mkdir "$download/runtime"
  tar --extract --zstd --file "$download/ollama.tar.zst" --directory "$download/runtime"
  mv "$download/runtime" "$runtime"
  rm -rf "$download"
  trap - EXIT
fi

export PATH="$runtime/bin:$PATH"
export OLLAMA_MODELS="$PWD/.local/ollama-models"
exec ./scripts/start-local.sh
