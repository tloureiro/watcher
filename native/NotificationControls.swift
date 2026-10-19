import Cocoa

extension Companion {
    var notificationStore: NotificationSnoozeStore? {
        guard !bridgeWorkspace.isEmpty, let runtime = state["runtime"] as? String else { return nil }
        return NotificationSnoozeStore(path: URL(fileURLWithPath: runtime).appendingPathComponent("data.db").path)
    }

    func notificationError(_ error: Error) {
        state["error"] = "Notification settings: \(error.localizedDescription)"
        rebuild()
    }

    func notificationSettings(_ action: @escaping (inout NotificationPreferences) -> Void,
                              completion: @escaping (NotificationPreferences) -> Void = { _ in }) {
        guard !quitting, let store = notificationStore else { return }
        let workspace = bridgeWorkspace
        notificationQueue.async {
            do {
                let settings = try store.update { preferences in
                    preferences.snoozes.removeAll { $0.until <= Date().timeIntervalSince1970 }
                    action(&preferences)
                    return preferences
                }
                DispatchQueue.main.async {
                    if self.bridgeWorkspace == workspace && !self.quitting { completion(settings) }
                }
            } catch { DispatchQueue.main.async { self.notificationError(error) } }
        }
    }

    static var snoozeDurations: [(String, Double)] {
        [("1 hour", 3600), ("3 hours", 10800), ("12 hours", 43200),
         ("1 day", 86400), ("3 days", 259200), ("1 week", 604800)]
    }

    /// Both notification actions and the menu use this same native chooser.
    func chooseSnooze(_ alert: DesktopAlert, existing: NotificationSnooze? = nil) {
        guard !quitting, notificationStore != nil else { return }
        let workspace = bridgeWorkspace
        NSApp.activate(ignoringOtherApps: true)
        let dialog = NSAlert()
        dialog.messageText = existing == nil ? "Snooze notifications" : "Extend snooze"
        dialog.informativeText = "\(alert.subject)\n\(alert.taskID.isEmpty ? "" : "Task: " + alert.taskID + "\n")Checks continue running. Suppressed notifications are discarded."
        dialog.addButton(withTitle: existing == nil ? "Snooze" : "Save")
        dialog.addButton(withTitle: "Cancel")
        let scope = NSPopUpButton(frame: NSRect(x: 0, y: 75, width: 340, height: 26))
        scope.addItem(withTitle: "This alert")
        if !alert.taskID.isEmpty { scope.addItem(withTitle: "This task — all its alerts") }
        if let existing { scope.selectItem(at: existing.alertID == nil ? 1 : 0); scope.isEnabled = false }
        let duration = NSPopUpButton(frame: NSRect(x: 0, y: 40, width: 340, height: 26))
        duration.addItems(withTitles: Self.snoozeDurations.map { $0.0 })
        let breakable = NSButton(checkboxWithTitle: "Break snooze if the message differs", target: nil, action: nil)
        breakable.frame = NSRect(x: 0, y: 4, width: 340, height: 26)
        breakable.state = existing?.breakOnChange == true ? .on : .off
        let view = NSView(frame: NSRect(x: 0, y: 0, width: 340, height: 110))
        for control in [scope as NSView, duration, breakable] { view.addSubview(control) }
        dialog.accessoryView = view
        guard dialog.runModal() == .alertFirstButtonReturn, workspace == bridgeWorkspace else { return }
        let seconds = Self.snoozeDurations[duration.indexOfSelectedItem].1
        let wholeTask = scope.indexOfSelectedItem == 1
        let canBreak = breakable.state == .on
        notificationSettings({ preferences in
            if let existing, let index = preferences.snoozes.firstIndex(where: { $0.id == existing.id }) {
                // Extending preserves the original baseline, so a message that
                // changed before Save still breaks a breakable snooze.
                preferences.snoozes[index].until = max(existing.until, Date().timeIntervalSince1970) + seconds
                preferences.snoozes[index].breakOnChange = canBreak
            } else if existing == nil {
                preferences.snooze(alert, wholeTask: wholeTask, until: Date().timeIntervalSince1970 + seconds, breakOnChange: canBreak)
            }
        })
    }

    @objc func snoozeNotifications() {
        notificationSettings({ _ in }) { preferences in
            let alerts = preferences.alerts.values.sorted { ($0.taskID, $0.subject, $0.alertID) < ($1.taskID, $1.subject, $1.alertID) }
            guard !alerts.isEmpty else { self.notificationNotice("No alerts yet", "Alerts appear here after a task sends its first desktop notification."); return }
            let workspace = self.bridgeWorkspace
            let dialog = NSAlert(); dialog.messageText = "Choose an alert to snooze"
            dialog.addButton(withTitle: "Continue…"); dialog.addButton(withTitle: "Cancel")
            let picker = NSPopUpButton(frame: NSRect(x: 0, y: 0, width: 420, height: 26))
            picker.addItems(withTitles: alerts.map { "\($0.taskID.isEmpty ? "" : $0.taskID + " · ")\($0.subject) [\($0.alertID)]" })
            dialog.accessoryView = picker
            NSApp.activate(ignoringOtherApps: true)
            if dialog.runModal() == .alertFirstButtonReturn && self.bridgeWorkspace == workspace {
                self.chooseSnooze(alerts[picker.indexOfSelectedItem])
            }
        }
    }

    @objc func silencedNotifications() {
        notificationSettings({ _ in }) { preferences in
            let rules = preferences.snoozes.sorted { $0.until < $1.until }
            guard !rules.isEmpty else { self.notificationNotice("No silenced notifications", "There are no active alert or task snoozes in this workspace."); return }
            let workspace = self.bridgeWorkspace
            let dialog = NSAlert(); dialog.messageText = "Silenced notifications"
            dialog.informativeText = "Select a snooze to resume notifications now or extend its duration."
            dialog.addButton(withTitle: "Resume Now"); dialog.addButton(withTitle: "Extend…"); dialog.addButton(withTitle: "Close")
            let picker = NSPopUpButton(frame: NSRect(x: 0, y: 0, width: 560, height: 26))
            picker.addItems(withTitles: rules.map {
                "\($0.alertID == nil ? "Task" : "Alert"): \($0.label) · until \(Date(timeIntervalSince1970: $0.until).formatted())\($0.breakOnChange ? " · breakable" : "")"
            })
            dialog.accessoryView = picker
            NSApp.activate(ignoringOtherApps: true)
            let result = dialog.runModal()
            guard self.bridgeWorkspace == workspace else { return }
            let rule = rules[picker.indexOfSelectedItem]
            if result == .alertFirstButtonReturn {
                self.notificationSettings({ $0.snoozes.removeAll { $0.id == rule.id } })
            } else if result == .alertSecondButtonReturn,
                      let alert = preferences.alerts.values.first(where: { rule.matches($0) }) {
                self.chooseSnooze(alert, existing: rule)
            }
        }
    }

    func notificationNotice(_ title: String, _ text: String) {
        NSApp.activate(ignoringOtherApps: true)
        let dialog = NSAlert(); dialog.messageText = title; dialog.informativeText = text; dialog.runModal()
    }
}
