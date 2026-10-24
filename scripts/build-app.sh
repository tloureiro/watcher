#!/bin/sh
set -eu
cd "$(dirname "$0")/.."
mkdir -p build/Watcher.app/Contents/MacOS build/Watcher.app/Contents/Resources
swiftc -swift-version 5 -O -framework Cocoa -framework UserNotifications -framework Network -framework ServiceManagement -lsqlite3 native/LoginStartup.swift native/NotificationSnoozes.swift native/NotificationHistory.swift native/NotificationControls.swift native/NotificationHistoryControls.swift native/Watcher.swift -o build/Watcher.app/Contents/MacOS/Watcher
cp native/Info.plist build/Watcher.app/Contents/Info.plist
codesign --force --sign - --identifier local.watcher.app build/Watcher.app
printf 'Built %s/build/Watcher.app\n' "$PWD"
