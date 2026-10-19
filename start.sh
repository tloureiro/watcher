#!/bin/sh
set -eu
cd "$(dirname "$0")"
exec open build/Watcher.app
