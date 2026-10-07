#!/bin/sh
set -e

# First run on a fresh volume: seed config and resume so the UI has something to edit.
mkdir -p /data/profile
[ -f /data/config.yaml ]        || cp /app/config.default.yaml /data/config.yaml
[ -f /data/profile/resume.md ]  || cp /app/profile.default/resume.md /data/profile/resume.md

# config.yaml points at profile/resume.md; make that resolve inside /data.
ln -sfn /data/profile /app/profile

python -m jobhunt.cli init

case "$1" in
  serve|"")  exec python -m jobhunt.cli serve --host 0.0.0.0 --port "${PORT:-8080}" ;;
  *)         exec python -m jobhunt.cli "$@" ;;
esac
