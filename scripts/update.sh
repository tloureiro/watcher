#!/bin/sh
set -eu
cd "$(dirname "$0")/.."
printf 'Installing this checkout’s locked versions. Watcher must be quit; its instance lock is held throughout setup.\n'
exec ./scripts/setup.sh
