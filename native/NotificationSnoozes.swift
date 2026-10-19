import Foundation
import CryptoKit
import SQLite3

struct DesktopAlert: Codable, Equatable {
    let taskID: String
    let alertID: String
    let subject: String
    let body: String

    // Encoding components avoids collisions between arbitrary task/alert ids.
    var identity: String { String(data: try! JSONEncoder().encode([taskID, alertID]), encoding: .utf8)! }
    var fingerprint: String {
        SHA256.hash(data: try! JSONEncoder().encode([subject, body])).map { String(format: "%02x", $0) }.joined()
    }
    var info: [String: String] { ["task_id": taskID, "alert_id": alertID, "subject": subject, "body": body] }
    init(taskID: String, alertID: String, subject: String, body: String) {
        self.taskID = taskID; self.alertID = alertID; self.subject = subject; self.body = body
    }
    init?(_ info: [String: String]) {
        guard let subject = info["subject"], let body = info["body"] else { return nil }
        self.init(taskID: info["task_id"] ?? "", alertID: info["alert_id"] ?? subject, subject: subject, body: body)
    }
}

struct NotificationSnooze: Codable {
    let id: String
    let taskID: String
    let alertID: String? // nil silences the entire task
    let label: String
    var until: Double
    var breakOnChange: Bool
    let baseline: [String: String]

    func matches(_ alert: DesktopAlert) -> Bool {
        taskID == alert.taskID && (alertID == nil || alertID == alert.alertID)
    }
}

struct NotificationPreferences: Codable {
    var alerts: [String: DesktopAlert] = [:]
    var snoozes: [NotificationSnooze] = []

    mutating func suppress(_ alert: DesktopAlert, now: Double) -> Bool {
        snoozes.removeAll { rule in
            rule.until <= now || (rule.matches(alert) && rule.breakOnChange && rule.baseline[alert.identity] != alert.fingerprint)
        }
        alerts[alert.identity] = alert
        return snoozes.contains { $0.matches(alert) }
    }

    mutating func snooze(_ alert: DesktopAlert, wholeTask: Bool, until: Double, breakOnChange: Bool) {
        // The clicked notification may be older than the latest check. Use the
        // current message baseline, including every known alert of this task.
        if alerts[alert.identity] == nil { alerts[alert.identity] = alert }
        snoozes.removeAll { $0.taskID == alert.taskID && $0.alertID == (wholeTask ? nil : alert.alertID) }
        let known = alerts.filter { $0.value.taskID == alert.taskID && (wholeTask || $0.value.alertID == alert.alertID) }
        snoozes.append(NotificationSnooze(id: UUID().uuidString, taskID: alert.taskID,
            alertID: wholeTask ? nil : alert.alertID,
            label: wholeTask ? alert.taskID : (alert.taskID.isEmpty ? alert.subject : alert.taskID + " · " + alert.subject),
            until: until, breakOnChange: breakOnChange, baseline: known.mapValues { $0.fingerprint }))
    }
}

/// A single private row in the existing compact database. SQLite transactions
/// share the snapshot writer lock; there is no third file or sync protocol.
/// Call on the companion's serial notification queue, never on the UI thread.
struct NotificationSnoozeStore {
    let path: String

    func update<T>(_ body: (inout NotificationPreferences) throws -> T) throws -> T {
        var db: OpaquePointer?
        guard sqlite3_open_v2(path, &db, SQLITE_OPEN_READWRITE | SQLITE_OPEN_CREATE, nil) == SQLITE_OK else {
            let message = db.map { String(cString: sqlite3_errmsg($0)) } ?? "Cannot open notification settings"
            sqlite3_close(db)
            throw failure(message)
        }
        defer { sqlite3_close(db) }
        sqlite3_busy_timeout(db, 60000)
        func execute(_ sql: String) throws {
            guard sqlite3_exec(db, sql, nil, nil, nil) == SQLITE_OK else { throw failure(String(cString: sqlite3_errmsg(db))) }
        }
        try execute("CREATE TABLE IF NOT EXISTS notification_preferences (id INTEGER PRIMARY KEY CHECK(id=1), value BLOB NOT NULL)")
        try execute("BEGIN IMMEDIATE")
        defer { sqlite3_exec(db, "ROLLBACK", nil, nil, nil) }
        var statement: OpaquePointer?
        guard sqlite3_prepare_v2(db, "SELECT value FROM notification_preferences WHERE id=1", -1, &statement, nil) == SQLITE_OK else {
            throw failure(String(cString: sqlite3_errmsg(db)))
        }
        var preferences = NotificationPreferences()
        let step = sqlite3_step(statement)
        if step == SQLITE_ROW {
            let count = Int(sqlite3_column_bytes(statement, 0))
            let data = sqlite3_column_blob(statement, 0).map { Data(bytes: $0, count: count) } ?? Data()
            sqlite3_finalize(statement)
            preferences = try JSONDecoder().decode(NotificationPreferences.self, from: data)
        } else {
            sqlite3_finalize(statement)
            guard step == SQLITE_DONE else { throw failure(String(cString: sqlite3_errmsg(db))) }
        }
        let encoder = JSONEncoder(); encoder.outputFormatting = .sortedKeys
        let previous = try encoder.encode(preferences)
        let result = try body(&preferences)
        let data = try encoder.encode(preferences)
        // Sorted encoding keeps idle reads and unchanged observations read-only.
        if previous != data {
            guard sqlite3_prepare_v2(db, "INSERT OR REPLACE INTO notification_preferences VALUES (1, ?)", -1, &statement, nil) == SQLITE_OK else {
                throw failure(String(cString: sqlite3_errmsg(db)))
            }
            defer { sqlite3_finalize(statement) }
            let status = data.withUnsafeBytes { bytes -> Int32 in
                sqlite3_bind_blob(statement, 1, bytes.baseAddress, Int32(bytes.count), unsafeBitCast(-1, to: sqlite3_destructor_type.self))
                return sqlite3_step(statement)
            }
            guard status == SQLITE_DONE else { throw failure(String(cString: sqlite3_errmsg(db))) }
        }
        try execute("COMMIT")
        return result
    }

    private func failure(_ message: String) -> NSError {
        NSError(domain: "Watcher notifications", code: 1, userInfo: [NSLocalizedDescriptionKey: message])
    }
}
