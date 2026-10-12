#!/bin/sh
# Project-local toolchain: Python deps via uv, Tectonic (LaTeX) into .bin/.
# Tectonic fetches only the TeX packages the template uses, cached in ~/.cache/Tectonic.
set -eu
cd "$(dirname "$0")"
uv sync
if [ ! -x .bin/tectonic ]; then
    mkdir -p .bin
    (cd .bin && curl --proto '=https' --tlsv1.2 -fsSL https://drop-sh.fullyjustified.net | sh)
fi
.bin/tectonic --version
