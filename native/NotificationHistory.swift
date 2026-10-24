import Foundation
import SQLite3

enum NotificationDelivery: String, Codable {
    case sent, muted, snoozed

    var label: String {
        switch self {
        case .sent: return "Sent to macOS"
        case .muted: return "Muted"
        case .snoozed: return "Snoozed"
        }
    }
}

struct NotificationOccurrence: Codable, Equatable {
    let id: String
    let timestamp: Double
    let alert: DesktopAlert
    let runID: String?
    let delivery: NotificationDelivery
    let isEngine: Bool

    func age(now: Double = Date().timeIntervalSince1970) -> String {
        let seconds = max(0, now - timestamp)
        if seconds < 60 { return "Just now" }
        if seconds < 3600 { return "\(Int(seconds / 60)) min ago" }
        if seconds < 86400 { return "\(Int(seconds / 3600)) hr ago" }
        return "\(Int(seconds / 86400)) d ago"
    }

    var details: String {
        let date = Date(timeIntervalSince1970: timestamp).formatted(date: .complete, time: .standard)
        let source = isEngine ? "Watcher" : (alert.taskID.isEmpty ? "Desktop alert" : "Task: " + alert.taskID)
        return "\(alert.subject)\n\(source) · \(date)\n\(delivery.label)\n\n\(alert.body)"
    }

    // Import only this workspace's notifications. Never invent dates from the
    // snooze baseline, which contains only the latest text of each alert.
    static func imported(id: String, date: Double, info: [String: String], subject: String,
                         body: String, workspace: String) -> NotificationOccurrence? {
        guard !workspace.isEmpty, info["workspace"] == workspace else { return nil }
        let engine = id == "watcher-engine-error" || info["kind"] == "engine"
        let identity = id == "watcher-engine-error" ? "legacy-engine:\(date)" : id
        let alert = DesktopAlert(taskID: info["task_id"] ?? "", alertID: info["alert_id"] ?? subject,
                                 subject: subject, body: body)
        return NotificationOccurrence(id: identity, timestamp: date, alert: alert,
            runID: info["run_id"].flatMap { $0.isEmpty ? nil : $0 }, delivery: .sent, isEngine: engine)
    }
}

/// A separate, indexed occurrence table in the same private/snapshotted data.db.
/// Every connection is short-lived and used only on the notification queue.
struct NotificationHistoryStore {
    let path: String

    private func transaction<T>(days: Double, now: Double, _ body: (OpaquePointer) throws -> T) throws -> T {
        var handle: OpaquePointer?
        guard sqlite3_open_v2(path, &handle, SQLITE_OPEN_READWRITE | SQLITE_OPEN_CREATE, nil) == SQLITE_OK,
              let db = handle else {
            let message = handle.map { String(cString: sqlite3_errmsg($0)) } ?? "Cannot open notification history"
            sqlite3_close(handle)
            throw NSError(domain: "Watcher notifications", code: 1, userInfo: [NSLocalizedDescriptionKey: message])
        }
        defer { sqlite3_close(db) }
        sqlite3_busy_timeout(db, 60000)
        try execute(db, "CREATE TABLE IF NOT EXISTS notification_history (id TEXT PRIMARY KEY, timestamp REAL NOT NULL, value BLOB NOT NULL)")
        try execute(db, "CREATE INDEX IF NOT EXISTS notification_history_order ON notification_history(timestamp DESC, id DESC)")
        try execute(db, "BEGIN IMMEDIATE")
        defer { sqlite3_exec(db, "ROLLBACK", nil, nil, nil) }
        let prune = try prepare(db, "DELETE FROM notification_history WHERE timestamp < ?")
        sqlite3_bind_double(prune, 1, now - days * 86400)
        let result = sqlite3_step(prune)
        sqlite3_finalize(prune)
        guard result == SQLITE_DONE else { throw failure(db) }
        let value = try body(db)
        try execute(db, "COMMIT")
        return value
    }

    func record(_ items: [NotificationOccurrence], days: Double, now: Double = Date().timeIntervalSince1970) throws {
        try transaction(days: days, now: now) { db in
            let statement = try prepare(db, "INSERT OR IGNORE INTO notification_history VALUES (?, ?, ?)")
            defer { sqlite3_finalize(statement) }
            for item in items where item.timestamp >= now - days * 86400 {
                sqlite3_reset(statement)
                bind(item.id, to: statement, at: 1)
                sqlite3_bind_double(statement, 2, item.timestamp)
                let bytes = try JSONEncoder().encode(item)
                let result = bytes.withUnsafeBytes { data -> Int32 in
                    sqlite3_bind_blob(statement, 3, data.baseAddress, Int32(data.count), transient)
                    return sqlite3_step(statement)
                }
                guard result == SQLITE_DONE else { throw failure(db) }
            }
        }
    }

    func read(days: Double, limit: Int = 100, before: NotificationOccurrence? = nil,
              now: Double = Date().timeIntervalSince1970) throws -> [NotificationOccurrence] {
        try transaction(days: days, now: now) { db in
            let sql = before == nil
                ? "SELECT value FROM notification_history ORDER BY timestamp DESC, id DESC LIMIT ?"
                : "SELECT value FROM notification_history WHERE timestamp < ? OR (timestamp = ? AND id < ?) ORDER BY timestamp DESC, id DESC LIMIT ?"
            let statement = try prepare(db, sql)
            defer { sqlite3_finalize(statement) }
            if let before {
                sqlite3_bind_double(statement, 1, before.timestamp)
                sqlite3_bind_double(statement, 2, before.timestamp)
                bind(before.id, to: statement, at: 3)
            }
            sqlite3_bind_int(statement, before == nil ? 1 : 4, Int32(limit))
            var values: [NotificationOccurrence] = []
            while true {
                let step = sqlite3_step(statement)
                if step == SQLITE_DONE { return values }
                guard step == SQLITE_ROW, let bytes = sqlite3_column_blob(statement, 0) else { throw failure(db) }
                values.append(try JSONDecoder().decode(NotificationOccurrence.self,
                    from: Data(bytes: bytes, count: Int(sqlite3_column_bytes(statement, 0)))))
            }
        }
    }

    private var transient: sqlite3_destructor_type { unsafeBitCast(-1, to: sqlite3_destructor_type.self) }
    private func bind(_ value: String, to statement: OpaquePointer, at index: Int32) {
        _ = value.withCString { sqlite3_bind_text(statement, index, $0, -1, transient) }
    }
    private func prepare(_ db: OpaquePointer, _ sql: String) throws -> OpaquePointer {
        var statement: OpaquePointer?
        guard sqlite3_prepare_v2(db, sql, -1, &statement, nil) == SQLITE_OK, let statement else { throw failure(db) }
        return statement
    }
    private func execute(_ db: OpaquePointer, _ sql: String) throws {
        guard sqlite3_exec(db, sql, nil, nil, nil) == SQLITE_OK else { throw failure(db) }
    }
    private func failure(_ db: OpaquePointer) -> NSError {
        NSError(domain: "Watcher notifications", code: 1,
                userInfo: [NSLocalizedDescriptionKey: String(cString: sqlite3_errmsg(db))])
    }
}
