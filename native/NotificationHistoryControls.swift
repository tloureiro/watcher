import Cocoa

extension Companion {
    var historyStore: NotificationHistoryStore? {
        notificationStore.map { NotificationHistoryStore(path: $0.path) }
    }
    var historyDays: Double { state["history_days"] as? Double ?? 90 }

    func populateNotificationHistory(_ menu: NSMenu) {
        menu.removeAllItems()
        if bridgeWorkspace.isEmpty && !smoke {
            add(menu, "Choose a workspace to see notifications", nil)
        } else if let error = historyError {
            add(menu, "Couldn’t load notifications — Retry", #selector(retryNotificationHistory)).toolTip = error
        } else if !historyLoaded {
            add(menu, "Loading notifications…", nil)
        } else if recentNotifications.isEmpty {
            add(menu, "No recent notifications", nil)
        }
        for occurrence in recentNotifications.prefix(10) {
            let title = occurrence.alert.subject.components(separatedBy: .newlines).joined(separator: " ")
            let short = title.count > 54 ? String(title.prefix(53)) + "…" : title
            let status = occurrence.delivery == .sent ? "" : " · " + occurrence.delivery.label
            let row = add(menu, "\(short) · \(occurrence.age())\(status)", #selector(showNotification(_:)))
            row.representedObject = occurrence
            row.toolTip = occurrence.details
        }
        menu.addItem(.separator())
        add(menu, "View All Notifications…", #selector(showAllNotifications), enabled: historyStore != nil || smoke)
    }

    @objc func retryNotificationHistory() { refreshNotificationHistory() }

    func refreshNotificationHistory() {
        guard let store = historyStore, !quitting else { return }
        let workspace = bridgeWorkspace, days = historyDays
        historyGeneration += 1
        let generation = historyGeneration
        notificationQueue.async {
            let result = Result { try store.read(days: days, limit: 10) }
            DispatchQueue.main.async {
                guard self.bridgeWorkspace == workspace, self.historyGeneration == generation, !self.quitting else { return }
                switch result {
                case .success(let items):
                    self.recentNotifications = items; self.historyError = nil; self.historyLoaded = true
                case .failure(let error): self.historyError = error.localizedDescription
                }
                self.rebuild()
            }
        }
    }

    func activateNotificationHistory() {
        historyWindow?.close(); historyWindow = nil
        recentNotifications = []; historyLoaded = false; historyError = nil
        refreshNotificationHistory()
        guard !smoke, let store = historyStore else { return }
        let workspace = bridgeWorkspace, days = historyDays
        // Import is best effort, independent of new bridge requests. The OS can
        // only return notifications that remain in Notification Center.
        center.getDeliveredNotifications { notifications in
            let values = notifications.compactMap { notification -> NotificationOccurrence? in
                let content = notification.request.content
                return NotificationOccurrence.imported(id: notification.request.identifier,
                    date: notification.date.timeIntervalSince1970,
                    info: content.userInfo as? [String: String] ?? [:],
                    subject: content.title, body: content.body, workspace: workspace)
            }
            DispatchQueue.main.async {
                guard self.bridgeWorkspace == workspace, !self.quitting else { return }
                self.notificationQueue.async {
                    let result = Result { try store.record(values, days: days) }
                    DispatchQueue.main.async {
                        guard self.bridgeWorkspace == workspace, !self.quitting else { return }
                        if case .failure(let error) = result { self.historyError = error.localizedDescription }
                        else { self.refreshNotificationHistory(); self.historyWindow?.newNotificationsAvailable() }
                        self.rebuild()
                    }
                }
            }
        }
    }

    // Captured store/workspace prevents a late delivery callback from writing
    // an old notification into the newly selected workspace.
    func persistNotification(_ occurrence: NotificationOccurrence, store: NotificationHistoryStore,
                             workspace: String, days: Double, completion: @escaping (Bool) -> Void) {
        notificationQueue.async {
            let result = Result { try store.record([occurrence], days: days) }
            DispatchQueue.main.async {
                switch result {
                case .success:
                    if self.bridgeWorkspace == workspace {
                        self.refreshNotificationHistory(); self.historyWindow?.newNotificationsAvailable()
                    }
                    completion(true)
                case .failure(let error):
                    if self.bridgeWorkspace == workspace {
                        self.historyError = error.localizedDescription; self.rebuild()
                    }
                    completion(false)
                }
            }
        }
    }

    @objc func showNotification(_ item: NSMenuItem) {
        guard let occurrence = item.representedObject as? NotificationOccurrence else { return }
        openNotificationHistory(selected: occurrence)
    }
    @objc func showAllNotifications() { openNotificationHistory(selected: nil) }
    func openNotificationHistory(selected: NotificationOccurrence?) {
        guard let store = historyStore else { return }
        if historyWindow == nil {
            historyWindow = NotificationHistoryWindow(companion: self, store: store, workspace: bridgeWorkspace,
                days: historyDays, name: (state["workspace"] as? String).map { URL(fileURLWithPath: $0).lastPathComponent } ?? "Workspace")
        }
        historyWindow?.present(selected: selected)
    }

    func notificationRunURL(_ occurrence: NotificationOccurrence) -> URL? {
        guard state["disabled"] as? Bool != true, process?.isRunning == true,
              state["can_run"] as? Bool == true, let run = occurrence.runID, UUID(uuidString: run) != nil,
              let base = state["dashboard"] as? String else { return nil }
        return URL(string: base + "/runs/flow-run/" + run)
    }

    /// Synthetic lifecycle diagnostic, like the production-menu inspector.
    /// Never reached during ordinary startup; no controller or OS banners.
    func inspectHistoryLifecycle() {
        defaults.set(true, forKey: "muted")
        bridgeWorkspace = "old"
        let store = historyStore!
        func waitForQueue() {
            var done = false
            notificationOperations.notify(queue: .main) {
                self.notificationQueue.async { DispatchQueue.main.async { done = true } }
            }
            let deadline = Date().addingTimeInterval(10)
            while !done && Date() < deadline { RunLoop.main.run(until: Date().addingTimeInterval(0.01)) }
            precondition(done)
        }
        notifyEngineStopped(); notifyEngineStopped()
        waitForQueue()
        let engines = try! store.read(days: 90)
        precondition(engines.count == 2 && engines.allSatisfy { $0.isEngine && $0.delivery == .muted })

        // The root tree must stay intact while an asynchronous update arrives.
        rebuild(); let originalMenu = item.menu!
        menuTracking = true
        refreshNotificationHistory(); waitForQueue()
        precondition(item.menu === originalMenu)
        let menuStable = item.menu === originalMenu
        menuTracking = false

        // Queue callbacks for the old workspace, then change context before
        // allowing the main run loop to consume any of them.
        refreshNotificationHistory()
        var persisted = false
        persistNotification(engines[0], store: store, workspace: "old", days: 90) { persisted = $0 }
        let oldWindow = NotificationHistoryWindow(companion: self, store: store, workspace: "old", days: 90, name: "Old")
        oldWindow.reload()
        bridgeWorkspace = "new"
        recentNotifications = []
        state["runtime"] = URL(fileURLWithPath: store.path).deletingLastPathComponent().appendingPathComponent("new").path
        try! FileManager.default.createDirectory(atPath: state["runtime"] as! String, withIntermediateDirectories: true)
        activateNotificationHistory()
        waitForQueue()
        precondition(persisted && recentNotifications.isEmpty && oldWindow.occurrences.isEmpty)
        precondition(try! historyStore!.read(days: 90).isEmpty)
        let report: [String: Any] = ["engine_occurrences": engines.count, "menu_stable": menuStable,
            "workspace_isolated": recentNotifications.isEmpty, "late_window_ignored": oldWindow.occurrences.isEmpty]
        FileHandle.standardOutput.write(try! JSONSerialization.data(withJSONObject: report) + Data([10]))
        NSStatusBar.system.removeStatusItem(item)
    }
}

/// One reusable native window: history on the left, selectable full text on the
/// right. Menu clicks select an occurrence here rather than opening a browser.
final class NotificationHistoryWindow: NSWindowController, NSTableViewDataSource, NSTableViewDelegate {
    weak var companion: Companion?
    let store: NotificationHistoryStore
    let workspace: String
    var days: Double
    var occurrences: [NotificationOccurrence] = []
    var requestedSelection: String?
    var generation = 0
    var loading = false
    var hasMore = false
    let table = NSTableView()
    let message = NSTextView()
    let status = NSTextField(labelWithString: "")
    let copyButton = NSButton(title: "Copy Message", target: nil, action: nil)
    let runButton = NSButton(title: "Open Run", target: nil, action: nil)
    let snoozeButton = NSButton(title: "Snooze…", target: nil, action: nil)
    let olderButton = NSButton(title: "Load Older", target: nil, action: nil)

    init(companion: Companion, store: NotificationHistoryStore, workspace: String, days: Double, name: String) {
        self.companion = companion; self.store = store; self.workspace = workspace; self.days = days
        let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 850, height: 520),
            styleMask: [.titled, .closable, .miniaturizable, .resizable], backing: .buffered, defer: false)
        window.title = "Notifications — " + name
        window.minSize = NSSize(width: 760, height: 420)
        window.isReleasedWhenClosed = false
        super.init(window: window)
        window.center()

        let column = NSTableColumn(identifier: NSUserInterfaceItemIdentifier("notification"))
        column.resizingMask = .autoresizingMask
        table.columnAutoresizingStyle = .lastColumnOnlyAutoresizingStyle
        table.addTableColumn(column); table.headerView = nil
        table.rowHeight = 65; table.usesAlternatingRowBackgroundColors = true
        table.delegate = self; table.dataSource = self
        table.setAccessibilityLabel("Notification history, newest first")
        let list = NSScrollView(); list.documentView = table; list.hasVerticalScroller = true
        list.borderType = .bezelBorder

        message.isEditable = false; message.isSelectable = true
        message.isRichText = true; message.importsGraphics = false
        message.textContainerInset = NSSize(width: 14, height: 14)
        message.isVerticallyResizable = true; message.isHorizontallyResizable = false
        message.autoresizingMask = [.width]
        message.textContainer?.widthTracksTextView = true
        message.textContainer?.containerSize = NSSize(width: 400, height: CGFloat.greatestFiniteMagnitude)
        message.setAccessibilityLabel("Full notification details")
        let detail = NSScrollView(); detail.documentView = message; detail.hasVerticalScroller = true
        detail.borderType = .bezelBorder
        let actions = NSStackView(views: [copyButton, runButton, snoozeButton])
        actions.spacing = 8; actions.alignment = .centerY
        let right = NSStackView(views: [detail, actions]); right.orientation = .vertical; right.alignment = .leading; right.spacing = 10
        let panes = NSStackView(views: [list, right]); panes.spacing = 14; panes.alignment = .top
        let refresh = NSButton(title: "Refresh", target: self, action: #selector(reload))
        let footer = NSStackView(views: [status, refresh, olderButton]); footer.spacing = 10
        status.lineBreakMode = .byTruncatingTail
        status.setContentHuggingPriority(.defaultLow, for: .horizontal)
        let root = NSStackView(views: [panes, footer]); root.orientation = .vertical; root.alignment = .leading; root.spacing = 12
        root.translatesAutoresizingMaskIntoConstraints = false
        window.contentView!.addSubview(root)
        NSLayoutConstraint.activate([
            root.leadingAnchor.constraint(equalTo: window.contentView!.leadingAnchor, constant: 16),
            root.trailingAnchor.constraint(equalTo: window.contentView!.trailingAnchor, constant: -16),
            root.topAnchor.constraint(equalTo: window.contentView!.topAnchor, constant: 16),
            root.bottomAnchor.constraint(equalTo: window.contentView!.bottomAnchor, constant: -16),
            panes.widthAnchor.constraint(equalTo: root.widthAnchor), footer.widthAnchor.constraint(equalTo: root.widthAnchor),
            list.widthAnchor.constraint(equalToConstant: 290), list.heightAnchor.constraint(equalTo: panes.heightAnchor),
            right.heightAnchor.constraint(equalTo: panes.heightAnchor), detail.widthAnchor.constraint(equalTo: right.widthAnchor)
        ])
        copyButton.target = self; copyButton.action = #selector(copyMessage)
        runButton.target = self; runButton.action = #selector(openRun)
        snoozeButton.target = self; snoozeButton.action = #selector(snooze)
        olderButton.target = self; olderButton.action = #selector(loadOlder)
        window.initialFirstResponder = table
        updateDetails()
    }
    required init?(coder: NSCoder) { fatalError("init(coder:) has not been implemented") }

    var selected: NotificationOccurrence? {
        occurrences.indices.contains(table.selectedRow) ? occurrences[table.selectedRow] : nil
    }

    func present(selected: NotificationOccurrence?) {
        if let selected { requestedSelection = selected.id }
        NSApp.activate(ignoringOtherApps: true)
        showWindow(nil); window?.makeKeyAndOrderFront(nil)
        window?.makeFirstResponder(table)
        reload()
    }

    @objc func reload() { fetch(reset: true) }
    @objc func loadOlder() { fetch(reset: false) }
    func newNotificationsAvailable() {
        // Do not replace a selected older message or reset the user's scroll
        // position when a task happens to send another notification.
        if selected == nil { reload() }
        else { status.stringValue = "New notifications available · Refresh to view" }
    }
    func fetch(reset: Bool) {
        guard let companion, companion.bridgeWorkspace == workspace, !companion.quitting,
              reset || (!loading && hasMore) else { return }
        let keep = requestedSelection ?? selected?.id
        generation += 1
        let current = generation
        let before = reset ? nil : occurrences.last
        days = companion.historyDays
        let retention = days
        loading = true; status.stringValue = "Loading notifications…"; olderButton.isEnabled = false
        companion.notificationQueue.async {
            let result = Result { try self.store.read(days: retention, before: before) }
            DispatchQueue.main.async {
                guard companion.bridgeWorkspace == self.workspace, !companion.quitting, self.generation == current else { return }
                self.loading = false
                switch result {
                case .success(let page):
                    self.occurrences = reset ? page : self.occurrences + page
                    self.hasMore = page.count == 100
                    self.table.reloadData()
                    if let index = self.occurrences.firstIndex(where: { $0.id == keep }) {
                        self.table.selectRowIndexes(IndexSet(integer: index), byExtendingSelection: false)
                    } else if !self.occurrences.isEmpty {
                        self.table.selectRowIndexes(IndexSet(integer: 0), byExtendingSelection: false)
                    }
                    if reset && self.table.selectedRow >= 0 { self.table.scrollRowToVisible(self.table.selectedRow) }
                    self.requestedSelection = nil
                    self.status.stringValue = self.occurrences.isEmpty ? "No recent notifications" : "\(self.occurrences.count) shown · Last \(retention.formatted()) days"
                    self.status.toolTip = nil
                    self.updateDetails()
                case .failure(let error):
                    self.status.stringValue = "Couldn’t load notifications. Click Refresh to retry."
                    self.status.toolTip = error.localizedDescription
                }
                self.olderButton.isEnabled = self.hasMore
            }
        }
    }

    func numberOfRows(in tableView: NSTableView) -> Int { occurrences.count }
    func tableView(_ tableView: NSTableView, viewFor tableColumn: NSTableColumn?, row: Int) -> NSView? {
        let occurrence = occurrences[row]
        let cell = NSTableCellView()
        let title = NSTextField(labelWithString: occurrence.alert.subject.components(separatedBy: .newlines).joined(separator: " "))
        title.font = .systemFont(ofSize: 13, weight: .medium); title.lineBreakMode = .byTruncatingTail
        let meta = NSTextField(labelWithString: "\(occurrence.age()) · \(occurrence.delivery.label)")
        meta.font = .systemFont(ofSize: 11); meta.textColor = .secondaryLabelColor
        let snippet = NSTextField(labelWithString: occurrence.alert.body.components(separatedBy: .newlines).joined(separator: " "))
        snippet.font = .systemFont(ofSize: 11); snippet.textColor = .secondaryLabelColor; snippet.lineBreakMode = .byTruncatingTail
        let stack = NSStackView(views: [title, meta, snippet]); stack.orientation = .vertical; stack.alignment = .leading; stack.spacing = 3
        stack.translatesAutoresizingMaskIntoConstraints = false; cell.addSubview(stack)
        NSLayoutConstraint.activate([
            stack.leadingAnchor.constraint(equalTo: cell.leadingAnchor, constant: 8),
            stack.trailingAnchor.constraint(equalTo: cell.trailingAnchor, constant: -8),
            stack.centerYAnchor.constraint(equalTo: cell.centerYAnchor),
            title.widthAnchor.constraint(equalTo: stack.widthAnchor), snippet.widthAnchor.constraint(equalTo: stack.widthAnchor)
        ])
        cell.textField = title; cell.toolTip = occurrence.details
        cell.setAccessibilityLabel(occurrence.details)
        return cell
    }
    func tableViewSelectionDidChange(_ notification: Notification) { updateDetails() }

    func updateActions() {
        copyButton.isEnabled = selected != nil
        runButton.isEnabled = selected.flatMap { companion?.notificationRunURL($0) } != nil
        runButton.toolTip = runButton.isEnabled ? "Open this run in Prefect" : "Requires an available engine and a linked run"
        snoozeButton.isEnabled = selected.map { !$0.isEngine } ?? false
    }
    func updateDetails() {
        let text = selected?.details ?? "Select a notification to read its full message.\n\nSent, muted, and snoozed notifications appear here."
        let note = selected?.delivery == .sent ? "\n\nmacOS permissions and Focus settings determine banner presentation." : ""
        let content = NSMutableAttributedString(string: text + note,
            attributes: [.font: NSFont.systemFont(ofSize: 14), .foregroundColor: NSColor.labelColor])
        if let selected {
            let heading = (selected.alert.subject as NSString).length
            content.addAttributes([.font: NSFont.systemFont(ofSize: 17, weight: .semibold)], range: NSRange(location: 0, length: heading))
            if let separator = text.range(of: "\n\n", range: text.index(text.startIndex, offsetBy: selected.alert.subject.count)..<text.endIndex) {
                let end = NSRange(text.startIndex..<separator.lowerBound, in: text).length
                content.addAttributes([.font: NSFont.systemFont(ofSize: 12), .foregroundColor: NSColor.secondaryLabelColor],
                    range: NSRange(location: heading, length: end - heading))
            }
            if !note.isEmpty {
                content.addAttributes([.font: NSFont.systemFont(ofSize: 11), .foregroundColor: NSColor.secondaryLabelColor],
                    range: NSRange(location: (text as NSString).length, length: (note as NSString).length))
            }
        }
        message.textStorage?.setAttributedString(content)
        message.scrollToBeginningOfDocument(nil)
        updateActions()
    }
    @objc func copyMessage() {
        guard let selected else { return }
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString(selected.alert.subject + "\n\n" + selected.alert.body, forType: .string)
    }
    @objc func openRun() {
        guard let selected, let url = companion?.notificationRunURL(selected) else { return }
        NSWorkspace.shared.open(url)
    }
    @objc func snooze() {
        guard let selected, !selected.isEngine else { return }
        companion?.chooseSnooze(selected.alert)
    }
}
