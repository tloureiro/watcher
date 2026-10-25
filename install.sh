#!/bin/sh
set -eu
cd "$(dirname "$0")"
printf 'Installing Watcher from this checkout. If Watcher is running, choose Quit Watcher (finish active work) and wait for it to exit first.\n'
exec ./scripts/setup.sh
