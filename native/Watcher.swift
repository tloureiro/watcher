import Cocoa
import UserNotifications
import Network
import ServiceManagement

enum SearchlightIcon: Hashable {
    case ready, working, paused, disabled, attention

    static func status(_ state: [String: Any]) -> SearchlightIcon {
        if state["error"] is String { return .attention }
        // Disabled is set before active work finishes draining.
        if (state["active"] as? Int ?? 0) > 0 { return .working }
        if state["disabled"] as? Bool == true { return .disabled }
        if state["paused"] as? Bool == true { return .paused }
        return .ready
    }

    // Template images adapt to light/dark menu bars and the selection highlight.
    // Cache both working frames; idle operation needs no drawing timer.
    private static var images: [SearchlightIcon: [NSImage]] = [:]
    func image(alternate: Bool = false) -> NSImage {
        if Self.images[self] == nil {
            Self.images[self] = [draw(alternate: false), draw(alternate: true)]
        }
        return Self.images[self]![alternate ? 1 : 0]
    }

    private func draw(alternate: Bool) -> NSImage {
        let image = NSImage(size: NSSize(width: 18, height: 18), flipped: true) { _ in
            NSGraphicsContext.saveGraphicsState()
            defer { NSGraphicsContext.restoreGraphicsState() }
            let transform = NSAffineTransform()
            transform.scale(by: 18.0 / 24.0)
            transform.concat()
            NSColor.black.setFill()
            func polygon(_ points: [(CGFloat, CGFloat)]) -> NSBezierPath {
                let path = NSBezierPath()
                path.move(to: NSPoint(x: points[0].0, y: points[0].1))
                for point in points.dropFirst() { path.line(to: NSPoint(x: point.0, y: point.1)) }
                path.close()
                return path
            }
            let tower = polygon([(3,23),(5.5,10),(5.5,6),(4,6),(9,1),(14,6),(12.5,6),(12.5,10),(15,23)])
            tower.windingRule = .evenOdd
            tower.append(NSBezierPath(roundedRect: NSRect(x: 7.4, y: 6.7, width: 3.2, height: 3), xRadius: 0.5, yRadius: 0.5))
            tower.append(NSBezierPath(roundedRect: NSRect(x: 8, y: 17, width: 2, height: 6), xRadius: 1, yRadius: 1))
            tower.fill()
            switch self {
            case .ready:
                polygon([(15,6.5),(24,2),(24,13),(15,9)]).fill()
            case .working:
                let beam: [(CGFloat, CGFloat)] = alternate
                    ? [(15,7),(24,0.5),(24,8),(15,9)]
                    : [(15,7),(24,7),(24,15),(15,9)]
                polygon(beam).fill()
            case .paused:
                NSBezierPath(roundedRect: NSRect(x: 17, y: 3, width: 2.5, height: 10), xRadius: 0.6, yRadius: 0.6).fill()
                NSBezierPath(roundedRect: NSRect(x: 21.5, y: 3, width: 2.5, height: 10), xRadius: 0.6, yRadius: 0.6).fill()
            case .attention:
                NSBezierPath(roundedRect: NSRect(x: 19, y: 2, width: 3.5, height: 7), xRadius: 1, yRadius: 1).fill()
                NSBezierPath(ovalIn: NSRect(x: 19, y: 11, width: 3.5, height: 3.5)).fill()
            case .disabled: break
            }
            return true
        }
        image.isTemplate = true
        return image
    }
}

final class Companion: NSObject, NSApplicationDelegate, UNUserNotificationCenterDelegate, NSMenuDelegate {
    var item: NSStatusItem!
    var beamTimer: Timer?
    var alternateBeam = false
    var process: Process?
    var input: Pipe?
    var output = Data()
    var state: [String: Any] = [:]
    var listener: NWListener?
    var token = UUID().uuidString
    var bridgeWorkspace = ""
    let notificationQueue = DispatchQueue(label: "local.watcher.notifications")
    var notificationsInFlight = Set<String>()
    let notificationOperations = DispatchGroup()
    var recentNotifications: [NotificationOccurrence] = []
    var historyLoaded = false
    var historyError: String?
    var historyGeneration = 0
    var historyWindow: NotificationHistoryWindow?
    var notificationHistoryMenu: NSMenu?
    var quitting = false
    var preparingWorkspace = false
    var menuTracking = false
    var activeTasksMenu: NSMenu?
    let loginStartup = LoginStartup()
    var localHome: URL {
        ProcessInfo.processInfo.environment["WATCHER_LOCAL_HOME"].map { URL(fileURLWithPath: $0) } ?? FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("Library/Application Support/Watcher")
    }
    func installation() throws -> [String: String] {
        let data = try Data(contentsOf: localHome.appendingPathComponent("install.json"))
        guard let settings = try JSONSerialization.jsonObject(with: data) as? [String: String],
              settings["python"] != nil, settings["uv"] != nil else {
            throw NSError(domain: "Watcher", code: 1, userInfo: [NSLocalizedDescriptionKey: "Run scripts/setup.sh before opening Watcher."])
        }
        return settings
    }
    let smoke = ProcessInfo.processInfo.environment["WATCHER_SMOKE_WORKSPACE"] != nil
    let defaults = ProcessInfo.processInfo.environment["WATCHER_SMOKE_WORKSPACE"] == nil ? UserDefaults.standard : UserDefaults(suiteName: "local.watcher.smoke.\(UUID().uuidString)")!
    let center = UNUserNotificationCenter.current()

    func applicationDidFinishLaunching(_ notification: Notification) {
        item = NSStatusBar.system.statusItem(withLength: 30)
        center.delegate = self
        center.setNotificationCategories([UNNotificationCategory(identifier: "watcher-alert", actions: [
            UNNotificationAction(identifier: "snooze", title: "Snooze…", options: [.foreground])
        ], intentIdentifiers: [])])
        if !smoke { center.requestAuthorization(options: [.alert, .sound]) { _, _ in } }
        if defaults.object(forKey: "login") == nil { defaults.set(true, forKey: "login") }
        if !smoke { updateLogin() } else { defaults.set(true, forKey: "muted") }
        startBridge()
        rebuild()
        if let path = ProcessInfo.processInfo.environment["WATCHER_SMOKE_WORKSPACE"] ?? defaults.string(forKey: "workspace") {
            let mode = ProcessInfo.processInfo.environment["WATCHER_SMOKE_INITIALIZE"]
            prepareWorkspace(path, create: smoke && mode != nil, example: smoke && mode == "example")
        } else {
            DispatchQueue.main.async { self.welcome() }
        }
    }
    @discardableResult
    func add(_ menu: NSMenu, _ title: String, _ action: Selector?, checked: Bool = false, represented: String? = nil, enabled: Bool = true) -> NSMenuItem {
        let row = NSMenuItem(title: title, action: action, keyEquivalent: "")
        row.target = self
        row.state = checked ? .on : .off
        row.representedObject = represented
        row.isEnabled = enabled && action != nil
        menu.addItem(row)
        return row
    }
    func group(_ menu: NSMenu, _ title: String) -> NSMenu {
        let row = NSMenuItem(title: title, action: nil, keyEquivalent: "")
        let child = NSMenu(title: title)
        child.autoenablesItems = false
        row.submenu = child
        menu.addItem(row)
        return child
    }
    func menuNeedsUpdate(_ menu: NSMenu) {
        if menu === activeTasksMenu { populateTasks(menu) }
        else if menu === notificationHistoryMenu { populateNotificationHistory(menu) }
        else { rebuild(menu) }
    }
    func menuWillOpen(_ menu: NSMenu) {
        if menu === item.menu {
            menuTracking = true
            if process?.isRunning == true { send(["action": "task_menu"]) }
            refreshNotificationHistory()
        }
    }
    func menuDidClose(_ menu: NSMenu) {
        if menu === item.menu {
            menuTracking = false
            DispatchQueue.main.async { self.rebuild() }
        }
    }
    func populateTasks(_ menu: NSMenu) {
        menu.removeAllItems()
        let available = state["disabled"] as? Bool != true && ["running", "definition-error", "maintenance-error"].contains(state["phase"] as? String ?? "")
        if !available {
            add(menu, state["disabled"] as? Bool == true ? "Automation is disabled" : "Tasks are unavailable while the engine starts or stops", nil)
            return
        }
        if let error = state["task_menu_error"] as? String {
            add(menu, error, nil)
            return
        }
        let tasks = state["task_menu"] as? [[String: Any]] ?? []
        if tasks.isEmpty {
            add(menu, (state["tasks"] as? [String] ?? []).isEmpty ? "No tasks configured" : "All tasks are paused or disabled", nil)
        }
        for task in tasks {
            guard let name = task["id"] as? String, let deployment = task["deployment_id"] as? String else { continue }
            let child = group(menu, name)
            if let run = task["run"] as? [String: Any], let status = run["state_name"] as? String {
                add(child, "Latest run: \(status)", nil)
            } else { add(child, "Not run yet", nil) }
            if let artifact = task["artifact"] as? [String: Any], artifact["id"] is String {
                let row = add(child, "Open Latest Artifact", #selector(openLatestArtifact(_:)), represented: name)
                row.toolTip = [artifact["key"] as? String, artifact["created"] as? String].compactMap { $0 }.joined(separator: " · ")
            } else { add(child, "No artifact produced yet", nil) }
            if let run = task["run"] as? [String: Any], let id = run["id"] as? String {
                add(child, "Open Latest Run", #selector(openTaskPage(_:)), represented: "/runs/flow-run/" + id)
            }
            add(child, "Open Task in Prefect", #selector(openTaskPage(_:)), represented: "/deployments/deployment/" + deployment)
            child.addItem(.separator())
            add(child, "Run Now", #selector(runTask(_:)), represented: name, enabled: state["can_run"] as? Bool == true)
        }
    }
    func rebuild(_ existingMenu: NSMenu? = nil) {
        // Status arrives while AppKit tracks submenus. Keep the current tree in
        // place so a heartbeat cannot collapse the user's hovered task/actions.
        if menuTracking { updateIcon(); return }
        let menu = existingMenu ?? NSMenu()
        menu.autoenablesItems = false
        menu.removeAllItems()
        menu.delegate = self
        let active = state["active"] as? Int ?? 0
        let phase = preparingWorkspace ? "Preparing workspace…" : (state["phase"] as? String ?? "Choose a workspace")
        add(menu, "Watcher: \(phase)\(active > 0 ? " (\(active) active)" : "")", nil)
        if let error = state["error"] as? String { add(menu, "Show Error…", #selector(showError)).toolTip = error }
        if let path = state["workspace"] as? String { add(menu, URL(fileURLWithPath: path).lastPathComponent, nil) }
        menu.addItem(.separator())
        activeTasksMenu = group(menu, "Active Tasks")
        activeTasksMenu?.delegate = self
        populateTasks(activeTasksMenu!)
        notificationHistoryMenu = group(menu, "Recent Notifications")
        notificationHistoryMenu?.delegate = self
        populateNotificationHistory(notificationHistoryMenu!)
        let engineAvailable = state["disabled"] as? Bool != true && process?.isRunning == true && state["can_run"] as? Bool == true
        add(menu, "Recent Activity", #selector(activity), enabled: engineAvailable)
        add(menu, "Open Prefect Dashboard", #selector(dashboard), enabled: engineAvailable)
        menu.addItem(.separator())
        let automation = group(menu, state["paused"] as? Bool == true ? "Automation (Paused)" : "Automation")
        add(automation, (state["paused"] as? Bool == true) ? "Resume Scheduling" : "Pause Scheduling", #selector(pause), enabled: engineAvailable)
        add(automation, (state["disabled"] as? Bool ?? false) ? "Enable Automation" : "Disable Automation (finish active work)", #selector(toggle), enabled: process?.isRunning == true)
        automation.addItem(.separator())
        add(automation, "Stop Immediately", #selector(immediate), enabled: process?.isRunning == true && (active > 0 || state["disabled"] as? Bool != true))
        let notifications = group(menu, defaults.bool(forKey: "muted") ? "Notifications (Muted)" : "Notifications")
        add(notifications, "Mute Notifications", #selector(mute), checked: defaults.bool(forKey: "muted"))
        add(notifications, "Snooze Notifications…", #selector(snoozeNotifications), enabled: state["workspace_id"] != nil)
        add(notifications, "Silenced Notifications…", #selector(silencedNotifications), enabled: state["workspace_id"] != nil)
        let workspace = group(menu, "Workspace")
        add(workspace, "Show in Finder", #selector(openWorkspace), enabled: state["workspace"] != nil)
        workspace.addItem(.separator())
        add(workspace, "Create Workspace…", #selector(createWorkspace), enabled: !preparingWorkspace)
        add(workspace, "Open Existing Workspace…", #selector(selectWorkspace), enabled: !preparingWorkspace)
        add(workspace, "Try the Example", #selector(tryExample), enabled: !preparingWorkspace)
        if let last = state["last_seen"] as? [String: Any], let host = last["host"] as? String, let timestamp = last["time"] as? Double {
            workspace.addItem(.separator())
            add(workspace, "Last reported by \(host)", nil)
            add(workspace, "\(Date(timeIntervalSince1970: timestamp).formatted()) (advisory)", nil)
        }
        let loginStatus = smoke ? SMAppService.Status.notRegistered : loginStartup.status
        let loginNeedsAttention = !smoke && (loginStartup.error != nil || (defaults.bool(forKey: "login") && loginStatus != .enabled))
        let settings = group(menu, loginNeedsAttention ? "Settings (Attention)" : "Settings")
        add(settings, "Launch at Login", #selector(login), checked: loginStatus == .enabled)
        if loginStatus == .requiresApproval { settings.items.last?.state = .mixed }
        if let error = loginStartup.error {
            add(settings, String(error.prefix(180)), #selector(showLoginError))
        } else if defaults.bool(forKey: "login") && loginStatus != .enabled {
            let title = loginStatus == .requiresApproval
                ? "Login startup needs approval in System Settings…"
                : "Login startup is unavailable — click to retry"
            add(settings, title, loginStatus == .requiresApproval ? #selector(loginSettings) : #selector(retryLogin))
        }
        add(settings, "Login Items Settings…", #selector(loginSettings))
        menu.addItem(.separator())
        add(menu, "Quit Watcher (finish active work)", #selector(quit))
        if existingMenu == nil { item.menu = menu }
        updateIcon()
        let description = "Watcher: \(phase)\(state["paused"] as? Bool == true ? " — scheduling paused" : "")\(active > 0 ? " (\(active) active)" : "")"
        item.button?.toolTip = description
        item.button?.setAccessibilityLabel(description)
    }
    func updateIcon() {
        let icon = SearchlightIcon.status(state)
        let animate = icon == .working && !NSWorkspace.shared.accessibilityDisplayShouldReduceMotion
        if animate && beamTimer == nil {
            let timer = Timer(timeInterval: 0.8, repeats: true) { [weak self] _ in
                guard let self else { return }
                self.alternateBeam.toggle()
                self.updateIcon()
            }
            timer.tolerance = 0.15
            RunLoop.main.add(timer, forMode: .common)
            beamTimer = timer
        } else if !animate {
            beamTimer?.invalidate()
            beamTimer = nil
            alternateBeam = false
        }
        item.button?.image = icon.image(alternate: alternateBeam)
    }
    func launch(_ path: String) {
        guard process == nil else {
            drainNotifications { self.send(["action": "switch", "path": path]) }
            return
        }
        let local = localHome
        do {
            let install = try installation()
            let p = Process()
            p.executableURL = URL(fileURLWithPath: install["python"]!)
            p.arguments = ["-m", "watcher.cli", "serve", path]
            var environment = ProcessInfo.processInfo.environment
            environment["WATCHER_UV"] = install["uv"]!
            p.environment = environment
            input = Pipe()
            let stdout = Pipe()
            p.standardInput = input
            p.standardOutput = stdout
            let log = local.appendingPathComponent("companion.log")
            if !FileManager.default.fileExists(atPath: log.path) { FileManager.default.createFile(atPath: log.path, contents: nil) }
            let handle = try FileHandle(forWritingTo: log)
            try handle.seekToEnd()
            p.standardError = handle
            stdout.fileHandleForReading.readabilityHandler = { [weak self] handle in
                let data = handle.availableData
                if data.isEmpty { handle.readabilityHandler = nil; return }
                DispatchQueue.main.async { self?.receive(data) }
            }
            p.terminationHandler = { [weak self] _ in DispatchQueue.main.async {
                guard let self else { return }
                self.process = nil
                self.input = nil
                if self.quitting { NSApp.terminate(nil) }
                else { self.state["error"] = "Controller stopped. Select the workspace again to restart."; self.rebuild() }
            }}
            try p.run()
            process = p

        } catch { state["error"] = error.localizedDescription; rebuild() }
    }
    func receive(_ data: Data) {
        output.append(data)
        while let end = output.firstIndex(of: 10) {
            let line = output.prefix(upTo: end)
            output.removeSubrange(...end)
            if let value = try? JSONSerialization.jsonObject(with: line) as? [String: Any],
               value["workspace_id"] as? String == state["workspace_id"] as? String,
               let id = value["open_artifact"] as? String, UUID(uuidString: id) != nil,
               let base = state["dashboard"] as? String, let url = URL(string: base + "/artifacts/artifact/" + id) {
                NSWorkspace.shared.open(url)
                continue
            }
            if let value = try? JSONSerialization.jsonObject(with: line) as? [String: Any], value["phase"] != nil {
                let failed = value["phase"] as? String == "engine-error" && state["phase"] as? String != "engine-error"
                state = value
                if let path = state["workspace"] as? String { defaults.set(path, forKey: "workspace") }
                publishBridge()
                if failed { notifyEngineStopped() }
                historyWindow?.updateActions()
                rebuild()
            }
        }
    }
    func send(_ value: [String: Any]) {
        guard process?.isRunning == true else { state["error"] = "Controller is stopped. Select the workspace to restart."; rebuild(); return }
        guard let data = try? JSONSerialization.data(withJSONObject: value) else { return }
        input?.fileHandleForWriting.write(data + Data([10]))
    }
    @objc func dashboard() { NSWorkspace.shared.open(URL(string: state["dashboard"] as? String ?? "http://127.0.0.1:4200")!) }
    @objc func activity() { NSWorkspace.shared.open(URL(string: (state["dashboard"] as? String ?? "http://127.0.0.1:4200") + "/runs")!) }
    @objc func openLatestArtifact(_ item: NSMenuItem) {
        guard let name = item.representedObject as? String else { return }
        send(["action": "latest_artifact", "task": name])
    }
    @objc func openTaskPage(_ item: NSMenuItem) {
        guard let path = item.representedObject as? String, let base = state["dashboard"] as? String,
              let url = URL(string: base + path) else { return }
        NSWorkspace.shared.open(url)
    }
    @objc func showError() { let a = NSAlert(); a.messageText = "Watcher"; a.informativeText = state["error"] as? String ?? ""; a.runModal() }
    @objc func runTask(_ item: NSMenuItem) { send(["action": "run", "task": item.representedObject as! String]) }
    @objc func pause() { send(["action": "pause", "value": !(state["paused"] as? Bool ?? false)]) }
    @objc func toggle() { send(["action": (state["disabled"] as? Bool ?? false) ? "enable" : "disable"]) }
    @objc func immediate() { send(["action": "immediate"]) }
    @objc func mute() { defaults.set(!defaults.bool(forKey: "muted"), forKey: "muted"); rebuild() }
    @objc func login() {
        // An unchecked, failed registration retries enabling on the first click.
        let enabled = loginStartup.status == .enabled || loginStartup.status == .requiresApproval
        defaults.set(!enabled, forKey: "login")
        updateLogin()
        rebuild()
    }
    func updateLogin() {
        loginStartup.apply(requested: defaults.bool(forKey: "login"))
    }
    @objc func retryLogin() { defaults.set(true, forKey: "login"); updateLogin(); rebuild() }
    @objc func loginSettings() { SMAppService.openSystemSettingsLoginItems() }
    @objc func showLoginError() {
        let alert = NSAlert()
        alert.messageText = "Watcher login startup"
        alert.informativeText = loginStartup.error ?? ""
        alert.addButton(withTitle: "Retry")
        alert.addButton(withTitle: "Login Items Settings…")
        alert.addButton(withTitle: "Cancel")
        switch alert.runModal() {
        case .alertFirstButtonReturn: retryLogin()
        case .alertSecondButtonReturn: loginSettings()
        default: break
        }
    }
    @objc func openWorkspace() { if let path = state["workspace"] as? String { NSWorkspace.shared.open(URL(fileURLWithPath: path)) } }
    @objc func welcome() {
        guard !preparingWorkspace else { return }
        NSApp.activate(ignoringOtherApps: true)
        let alert = NSAlert()
        alert.messageText = "Welcome to Watcher"
        alert.informativeText = "Create a workspace for your tasks, open an existing one, or try the USD/CAD example. Watcher remembers your workspace for next time."
        for title in ["Create Workspace…", "Open Existing Workspace…", "Try the Example", "Later"] { alert.addButton(withTitle: title) }
        switch alert.runModal() {
        case .alertFirstButtonReturn: createWorkspace()
        case .alertSecondButtonReturn: selectWorkspace()
        case .alertThirdButtonReturn: tryExample()
        default: break
        }
    }
    @objc func createWorkspace() {
        guard !preparingWorkspace else { return }
        NSApp.activate(ignoringOtherApps: true)
        let panel = NSSavePanel()
        panel.title = "Create Workspace"
        panel.message = "Choose a name and location for your new workspace."
        panel.nameFieldStringValue = "Watcher Workspace"
        panel.canCreateDirectories = true
        panel.prompt = "Create"
        if panel.runModal() == .OK, let url = panel.url { prepareWorkspace(url.path, create: true) }
    }
    @objc func tryExample() {
        prepareWorkspace(localHome.appendingPathComponent("Example Workspace").path, create: true, example: true)
    }
    @objc func selectWorkspace() {
        guard !preparingWorkspace else { return }
        NSApp.activate(ignoringOtherApps: true)
        let panel = NSOpenPanel(); panel.canChooseDirectories = true; panel.canChooseFiles = false
        panel.allowsMultipleSelection = false
        panel.message = "Open an existing Watcher workspace. Switching finishes active work first."
        if panel.runModal() == .OK, let url = panel.url { prepareWorkspace(url.path) }
    }
    func prepareWorkspace(_ path: String, create: Bool = false, example: Bool = false) {
        guard !preparingWorkspace, !quitting else { return }
        preparingWorkspace = true
        rebuild()
        // Initialization/validation uses the same Python workspace rules as the CLI.
        // Read the pipe while the helper runs so diagnostics cannot fill it and block.
        DispatchQueue.global(qos: .userInitiated).async {
            var failure: String?
            do {
                let install = try self.installation()
                let helper = Process()
                helper.executableURL = URL(fileURLWithPath: install["python"]!)
                helper.arguments = ["-m", "watcher.cli", create ? "init" : "validate-workspace", path] + (example ? ["--example"] : [])
                let pipe = Pipe()
                helper.standardOutput = pipe
                helper.standardError = pipe
                try helper.run()
                let diagnostic = pipe.fileHandleForReading.readDataToEndOfFile()
                helper.waitUntilExit()
                if helper.terminationStatus != 0 {
                    failure = String(data: diagnostic, encoding: .utf8) ?? "Could not prepare workspace."
                }
            } catch { failure = error.localizedDescription }
            let error = failure
            DispatchQueue.main.async {
                self.preparingWorkspace = false
                guard !self.quitting else { return }
                if let error {
                    self.state["error"] = error
                    self.rebuild()
                    if !self.smoke { self.showError() }
                } else {
                    self.state.removeValue(forKey: "error")
                    self.launch(path)
                    self.rebuild()
                }
            }
        }
    }
    @objc func quit() {
        quitting = true
        // Include pending OS callbacks/history writes in the final snapshot.
        drainNotifications {
            if self.process == nil { NSApp.terminate(nil) } else { self.send(["action": "quit"]) }
        }
    }
    func drainNotifications(_ completion: @escaping () -> Void) {
        notificationOperations.notify(queue: .main) {
            self.notificationQueue.async { DispatchQueue.main.async(execute: completion) }
        }
    }
    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        if process != nil { quit(); return .terminateCancel }; return .terminateNow
    }
    func startBridge() {
        do {
            let parameters = NWParameters.tcp
            parameters.requiredLocalEndpoint = .hostPort(host: "127.0.0.1", port: .any)
            let listener = try NWListener(using: parameters)
            self.listener = listener
            listener.stateUpdateHandler = { [weak self] state in if case .ready = state { DispatchQueue.main.async { self?.publishBridge() } } }
            listener.newConnectionHandler = { [weak self] connection in
                connection.start(queue: .main)
                self?.readRequest(connection, buffer: Data())
            }
            listener.start(queue: .main)
        } catch { state["error"] = "Notification endpoint: \(error)" }
    }
    func publishBridge() {
        guard let port = listener?.port, let runtime = state["runtime"] as? String, let id = state["workspace_id"] as? String else { return }
        if bridgeWorkspace == id { return }
        if !bridgeWorkspace.isEmpty { center.removeAllPendingNotificationRequests() }
        bridgeWorkspace = id
        activateNotificationHistory()
        token = UUID().uuidString
        let value = ["url": "http://127.0.0.1:\(port.rawValue)/notify", "token": token, "workspace": id]
        do {
            let url = URL(fileURLWithPath: runtime).appendingPathComponent("notification.json")
            try JSONSerialization.data(withJSONObject: value).write(to: url, options: .atomic)
            try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: url.path)
        } catch { state["error"] = "Notification configuration: \(error)" }
    }
    func reply(_ connection: NWConnection, _ code: Int) {
        let data = Data("HTTP/1.1 \(code) \(code == 200 ? "OK" : "Error")\r\nContent-Length: 0\r\nConnection: close\r\n\r\n".utf8)
        connection.send(content: data, completion: .contentProcessed { _ in connection.cancel() })
    }
    func readRequest(_ connection: NWConnection, buffer: Data) {
        connection.receive(minimumIncompleteLength: 1, maximumLength: 65536) { [weak self] data, _, complete, error in
            guard let self else { connection.cancel(); return }
            var buffer = buffer; if let data { buffer.append(data) }
            guard buffer.count <= 65536 else { self.reply(connection, 413); return }
            guard let boundary = buffer.range(of: Data("\r\n\r\n".utf8)), let header = String(data: buffer[..<boundary.lowerBound], encoding: .utf8) else {
                if complete || error != nil { connection.cancel() } else { self.readRequest(connection, buffer: buffer) }; return
            }
            let lines = header.components(separatedBy: "\r\n")
            var headers: [String: String] = [:]
            for line in lines.dropFirst() { let pieces = line.split(separator: ":", maxSplits: 1); if pieces.count == 2 { headers[pieces[0].lowercased()] = pieces[1].trimmingCharacters(in: .whitespaces) } }
            guard lines.first == "POST /notify HTTP/1.1", headers["authorization"] == "Bearer \(self.token)",
                  let length = Int(headers["content-length"] ?? ""), length >= 0, length < 60000 else { self.reply(connection, 403); return }
            let body = buffer[boundary.upperBound...]
            if body.count < length { if complete { self.reply(connection, 400) } else { self.readRequest(connection, buffer: buffer) }; return }
            guard let value = try? JSONSerialization.jsonObject(with: body.prefix(length)) as? [String: String], value["workspace"] == self.bridgeWorkspace,
                  let key = value["key"], let text = value["body"] else { self.reply(connection, 400); return }
            let identityParts = [value["task_id"] ?? "", value["alert_id"] ?? (value["subject"] ?? "Watcher"), key]
            let identity = self.bridgeWorkspace + ":" + String(data: try! JSONEncoder().encode(identityParts), encoding: .utf8)!
            if (self.defaults.stringArray(forKey: "notifications") ?? []).contains(identity) { self.reply(connection, 200); return }
            guard !self.quitting, !self.notificationsInFlight.contains(identity), let store = self.notificationStore,
                  let history = self.historyStore else { self.reply(connection, 503); return }
            self.notificationsInFlight.insert(identity)
            self.notificationOperations.enter()
            let workspace = self.bridgeWorkspace
            let days = self.historyDays, timestamp = Date().timeIntervalSince1970
            let alert = DesktopAlert(taskID: value["task_id"] ?? "", alertID: value["alert_id"] ?? (value["subject"] ?? "Watcher"),
                                     subject: value["subject"] ?? "Watcher", body: text)
            self.notificationQueue.async {
                do {
                    let suppressed = try store.update { $0.suppress(alert, now: Date().timeIntervalSince1970) }
                    DispatchQueue.main.async {
                        guard self.bridgeWorkspace == workspace else {
                            self.rejectNotification(identity, connection: connection, code: 409); return
                        }
                        if suppressed || self.defaults.bool(forKey: "muted") {
                            let occurrence = NotificationOccurrence(id: identity, timestamp: timestamp, alert: alert,
                                runID: value["run_id"], delivery: suppressed ? .snoozed : .muted, isEngine: false)
                            self.persistNotification(occurrence, store: history, workspace: workspace, days: days) { success in
                                if success && self.bridgeWorkspace == workspace { self.acceptNotification(identity, connection: connection) }
                                else { self.rejectNotification(identity, connection: connection, code: success ? 409 : 503) }
                            }
                            return
                        }
                        let content = UNMutableNotificationContent()
                        content.title = alert.subject; content.body = alert.body; content.sound = .default
                        content.categoryIdentifier = "watcher-alert"
                        content.userInfo = alert.info.merging(["workspace": workspace, "run_id": value["run_id"] ?? ""]) { _, new in new }
                        self.center.add(UNNotificationRequest(identifier: identity, content: content, trigger: nil)) { error in
                            DispatchQueue.main.async {
                                if error != nil {
                                    self.rejectNotification(identity, connection: connection, code: 503)
                                } else if self.bridgeWorkspace != workspace {
                                    self.center.removePendingNotificationRequests(withIdentifiers: [identity])
                                    self.center.removeDeliveredNotifications(withIdentifiers: [identity])
                                    self.rejectNotification(identity, connection: connection, code: 409)
                                } else {
                                    let occurrence = NotificationOccurrence(id: identity, timestamp: timestamp, alert: alert,
                                        runID: value["run_id"], delivery: .sent, isEngine: false)
                                    self.persistNotification(occurrence, store: history, workspace: workspace, days: days) { success in
                                        if success && self.bridgeWorkspace == workspace { self.acceptNotification(identity, connection: connection) }
                                        else { self.rejectNotification(identity, connection: connection, code: success ? 409 : 503) }
                                    }
                                }
                            }
                        }
                    }
                } catch {
                    DispatchQueue.main.async {
                        self.rejectNotification(identity, connection: connection, code: 503)
                        if self.bridgeWorkspace == workspace { self.notificationError(error) }
                    }
                }
            }
        }
    }
    func acceptNotification(_ identity: String, connection: NWConnection) {
        var seen = defaults.stringArray(forKey: "notifications") ?? []
        seen.append(identity)
        defaults.set(Array(seen.suffix(2000)), forKey: "notifications")
        notificationsInFlight.remove(identity)
        notificationOperations.leave()
        reply(connection, 200)
    }
    func rejectNotification(_ identity: String, connection: NWConnection, code: Int) {
        notificationsInFlight.remove(identity)
        notificationOperations.leave()
        reply(connection, code)
    }
    func notifyEngineStopped() {
        guard let history = historyStore else { return }
        let workspace = bridgeWorkspace, days = historyDays
        let identity = "watcher-engine-error:" + UUID().uuidString
        let alert = DesktopAlert(taskID: "", alertID: "engine-stopped", subject: "Watcher engine stopped",
            body: "Automation is unavailable. Open Watcher to inspect the error and restart.")
        let timestamp = Date().timeIntervalSince1970
        notificationOperations.enter()
        let persist: (NotificationDelivery) -> Void = { delivery in
            let occurrence = NotificationOccurrence(id: identity, timestamp: timestamp, alert: alert,
                runID: nil, delivery: delivery, isEngine: true)
            self.persistNotification(occurrence, store: history, workspace: workspace, days: days) { _ in
                self.notificationOperations.leave()
            }
        }
        if defaults.bool(forKey: "muted") { persist(.muted); return }
        let content = UNMutableNotificationContent()
        content.title = alert.subject; content.body = alert.body
        content.userInfo = ["workspace": workspace, "kind": "engine"]
        center.add(UNNotificationRequest(identifier: identity, content: content, trigger: nil)) { error in
            DispatchQueue.main.async {
                if error == nil { persist(.sent) }
                else { self.notificationOperations.leave() }
            }
        }
    }
    func userNotificationCenter(_ center: UNUserNotificationCenter, willPresent notification: UNNotification, withCompletionHandler completionHandler: @escaping (UNNotificationPresentationOptions) -> Void) { completionHandler([.banner, .sound]) }
    func userNotificationCenter(_ center: UNUserNotificationCenter, didReceive response: UNNotificationResponse, withCompletionHandler completionHandler: @escaping () -> Void) {
        let info = response.notification.request.content.userInfo as? [String: String] ?? [:]
        DispatchQueue.main.async {
            guard info["workspace"] == self.bridgeWorkspace else { return }
            if response.actionIdentifier == "snooze", let alert = DesktopAlert(info) { self.chooseSnooze(alert) }
            else if response.actionIdentifier == UNNotificationDefaultActionIdentifier {
                let run = info["run_id"] ?? ""
                let base = self.state["dashboard"] as? String ?? "http://127.0.0.1:4200"
                if let url = URL(string: base + (run.isEmpty ? "" : "/runs/flow-run/" + run)) { NSWorkspace.shared.open(url) }
            }
        }
        completionHandler()
    }
}
@main
enum WatcherMain {
    static func main() {
        // Read-only diagnostics: no controller, notifications, or registration.
        if CommandLine.arguments.contains("--login-status") {
            let report: [String: Any] = [
                "status": LoginStartup.name(SMAppService.mainApp.status),
                "requested": UserDefaults.standard.bool(forKey: "login"),
                "bundle": Bundle.main.bundleURL.path
            ]
            let data = try! JSONSerialization.data(withJSONObject: report, options: [.sortedKeys])
            FileHandle.standardOutput.write(data + Data([10]))
            return
        }
        let app = NSApplication.shared
        let delegate = Companion()
        // Synthetic menu inspection uses the actual AppKit tree, without
        // starting a controller or changing notification/login preferences.
        if delegate.smoke, let path = ProcessInfo.processInfo.environment["WATCHER_SMOKE_MENU_STATE"] {
            delegate.state = try! JSONSerialization.jsonObject(with: Data(contentsOf: URL(fileURLWithPath: path))) as! [String: Any]
            if let values = delegate.state["notification_history"] as? [[String: Any]] {
                delegate.recentNotifications = try! JSONDecoder().decode([NotificationOccurrence].self,
                    from: JSONSerialization.data(withJSONObject: values))
                delegate.historyLoaded = true
            }
            delegate.historyError = delegate.state["notification_history_error"] as? String
            delegate.item = NSStatusBar.system.statusItem(withLength: 30)
            if ProcessInfo.processInfo.environment["WATCHER_SMOKE_HISTORY_LIFECYCLE"] != nil {
                delegate.inspectHistoryLifecycle()
                return
            }
            delegate.rebuild()
            if ProcessInfo.processInfo.environment["WATCHER_SMOKE_HISTORY_WINDOW"] != nil {
                if let appearance = ProcessInfo.processInfo.environment["WATCHER_SMOKE_HISTORY_APPEARANCE"] {
                    app.appearance = NSAppearance(named: NSAppearance.Name(appearance))
                }
                let window = NotificationHistoryWindow(companion: delegate, store: NotificationHistoryStore(path: "/unused"),
                    workspace: "synthetic", days: 90, name: "Synthetic Workspace")
                window.occurrences = delegate.recentNotifications
                window.table.reloadData()
                window.table.selectRowIndexes(IndexSet(integer: min(1, max(0, window.occurrences.count - 1))), byExtendingSelection: false)
                window.window!.makeFirstResponder(window.table)
                if window.occurrences.count > 2 {
                    let down = NSEvent.keyEvent(with: .keyDown, location: .zero, modifierFlags: [], timestamp: 0,
                        windowNumber: window.window!.windowNumber, context: nil, characters: "\u{F701}",
                        charactersIgnoringModifiers: "\u{F701}", isARepeat: false, keyCode: 125)!
                    window.table.keyDown(with: down)
                    precondition(window.selected?.id == window.occurrences[2].id)
                    window.table.selectRowIndexes(IndexSet(integer: 1), byExtendingSelection: false)
                }
                let before = window.message.string
                window.newNotificationsAvailable()
                precondition(window.message.string == before)
                window.window!.contentView!.layoutSubtreeIfNeeded()
                let report: [String: Any] = ["text": window.message.string, "selected": window.selected?.id ?? "",
                    "copy": window.copyButton.isEnabled, "snooze": window.snoozeButton.isEnabled,
                    "open_run": window.runButton.isEnabled, "detail_width": window.message.enclosingScrollView!.frame.width,
                    "detail_height": window.message.enclosingScrollView!.frame.height,
                    "update_notice": window.status.stringValue]
                if let path = ProcessInfo.processInfo.environment["WATCHER_SMOKE_HISTORY_IMAGE"],
                   let view = window.window?.contentView,
                   let bitmap = view.bitmapImageRepForCachingDisplay(in: view.bounds) {
                    view.cacheDisplay(in: view.bounds, to: bitmap)
                    try! bitmap.representation(using: .png, properties: [:])!.write(to: URL(fileURLWithPath: path))
                }
                FileHandle.standardOutput.write(try! JSONSerialization.data(withJSONObject: report) + Data([10]))
                NSStatusBar.system.removeStatusItem(delegate.item)
                return
            }
            func rows(_ menu: NSMenu) -> [[String: Any]] {
                menu.items.map { row in
                    var value: [String: Any] = ["title": row.title, "enabled": row.isEnabled, "separator": row.isSeparatorItem]
                    if let action = row.action { value["action"] = NSStringFromSelector(action) }
                    if let path = row.representedObject as? String { value["path"] = path }
                    if let occurrence = row.representedObject as? NotificationOccurrence { value["occurrence"] = occurrence.id }
                    if let tooltip = row.toolTip { value["tooltip"] = tooltip }
                    if let child = row.submenu { value["children"] = rows(child) }
                    return value
                }
            }
            let data = try! JSONSerialization.data(withJSONObject: rows(delegate.item.menu!))
            FileHandle.standardOutput.write(data + Data([10]))
            NSStatusBar.system.removeStatusItem(delegate.item)
            return
        }
        app.delegate = delegate
        app.setActivationPolicy(.accessory)
        app.run()
        withExtendedLifetime(delegate) {}
    }
}
