"""Exercise the native login lifecycle without touching real macOS login items."""
from pathlib import Path
import subprocess


def test_login_registration_recovers_missing_record_and_surfaces_failure(tmp_path):
    harness = tmp_path / "main.swift"
    harness.write_text(r'''
import Foundation
import ServiceManagement

var status = SMAppService.Status.notFound
var registrations = 0
var removals = 0
var fail = false
let login = LoginStartup(status: { status }, register: {
    registrations += 1
    if fail { throw NSError(domain: "SyntheticLogin", code: 42) }
    status = .enabled
}, unregister: {
    removals += 1
    status = .notRegistered
})
// The real incident: a saved true preference with no OS record must register.
login.apply(requested: true)
precondition(status == .enabled && registrations == 1)
login.apply(requested: true)
precondition(registrations == 1) // Opening again leaves an enabled item alone.
login.apply(requested: false)
precondition(status == .notRegistered && removals == 1)
login.apply(requested: false)
precondition(removals == 1)
login.apply(requested: true)
precondition(status == .enabled && registrations == 2)
// Approval belongs to macOS: don't repeatedly register or bypass it.
status = .requiresApproval
login.apply(requested: true)
precondition(status == .requiresApproval && registrations == 2)
login.apply(requested: false)
precondition(status == .notRegistered && removals == 2)
// A failed registration remains visible until an explicit successful retry.
status = .notFound
fail = true
login.apply(requested: true)
precondition(status == .notFound && login.error!.contains("SyntheticLogin 42"))
precondition(login.status == .notFound && login.error != nil)
fail = false
login.apply(requested: true)
precondition(status == .enabled && login.error == nil)
''')
    executable = tmp_path / "login-check"
    subprocess.run([
        "swiftc", "-framework", "ServiceManagement",
        str(Path("native/LoginStartup.swift").resolve()), str(harness),
        "-o", str(executable),
    ], check=True, capture_output=True, text=True)
    subprocess.run([str(executable)], check=True, capture_output=True, text=True)
