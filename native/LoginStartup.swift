import ServiceManagement

// Keep OS registration separate from the user's preference and engine status.
// The closures let regression checks exercise missing records without changing
// the machine's real login items.
final class LoginStartup {
    let readStatus: () -> SMAppService.Status
    let register: () throws -> Void
    let unregister: () throws -> Void
    private(set) var error: String?

    init(status: @escaping () -> SMAppService.Status = { SMAppService.mainApp.status },
         register: @escaping () throws -> Void = { try SMAppService.mainApp.register() },
         unregister: @escaping () throws -> Void = { try SMAppService.mainApp.unregister() }) {
        self.readStatus = status
        self.register = register
        self.unregister = unregister
    }

    var status: SMAppService.Status { readStatus() }

    func apply(requested: Bool) {
        error = nil
        do {
            switch (requested, status) {
            case (true, .notRegistered), (true, .notFound): try register()
            case (false, .enabled), (false, .requiresApproval): try unregister()
            default: break
            }
        } catch {
            let detail = error as NSError
            self.error = "Login setup: \(detail.localizedDescription) (\(detail.domain) \(detail.code))"
        }
    }

    static func name(_ status: SMAppService.Status) -> String {
        switch status {
        case .notRegistered: return "notRegistered"
        case .enabled: return "enabled"
        case .requiresApproval: return "requiresApproval"
        case .notFound: return "notFound"
        @unknown default: return "unknown"
        }
    }
}
