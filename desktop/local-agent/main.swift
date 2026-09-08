import Cocoa

final class AgentApp: NSObject, NSApplicationDelegate {
    var receivedURL = false
    var handling = false
    var binary: String { Bundle.main.bundlePath + "/Contents/MacOS/softnix-local-agent" }
    func alert(_ title: String, _ message: String) {
        let panel = NSAlert()
        panel.messageText = title; panel.informativeText = message
        panel.addButton(withTitle: "OK")
        NSApp.activate(ignoringOtherApps: true); panel.runModal()
    }
    func run(_ args: [String], code: String? = nil) throws {
        let process = Process()
        process.executableURL = URL(fileURLWithPath: binary); process.arguments = args
        let output = Pipe(); process.standardError = output; process.standardOutput = FileHandle.nullDevice
        let input = Pipe(); process.standardInput = input
        try process.run()
        if let code = code { input.fileHandleForWriting.write(Data((code + "\n").utf8)) }
        try? input.fileHandleForWriting.close()
        let data = output.fileHandleForReading.readDataToEndOfFile()
        process.waitUntilExit()
        if process.terminationStatus != 0 {
            throw NSError(domain: "Softnix", code: 1, userInfo: [NSLocalizedDescriptionKey:
                String(data: data, encoding: .utf8) ?? "Local Agent could not start"])
        }
    }
    func installed() -> Bool {
        let path = Bundle.main.bundlePath
        if !(path.hasPrefix("/Applications/") || path.hasPrefix(NSHomeDirectory() + "/Applications/")) {
            alert("Move Softnix Local Agent to Applications", "Drag the app into Applications, then open it again. This gives the background service a permanent location.")
            return false
        }
        return true
    }
    func ensureService() throws {
        let plist = NSHomeDirectory() + "/Library/LaunchAgents/ai.softnix.local-agent.plist"
        try run([FileManager.default.fileExists(atPath: plist) ? "start" : "install"])
    }
    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.accessory)
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.5) { [self] in
            guard !receivedURL else { return }
            guard installed() else { NSApp.terminate(nil); return }
            do {
                try ensureService()
                let panel = NSAlert()
                panel.messageText = "Softnix Local Agent is ready"
                panel.informativeText = "Choose Open local folder… in Bot Mode. The service starts automatically when you log in."
                panel.addButton(withTitle: "Done"); panel.addButton(withTitle: "Restart service")
                NSApp.activate(ignoringOtherApps: true)
                if panel.runModal() == .alertSecondButtonReturn { try run(["restart"]) }
            } catch { alert("Unable to start service", error.localizedDescription) }
            NSApp.terminate(nil)
        }
    }
    func application(_ application: NSApplication, open urls: [URL]) {
        receivedURL = true
        guard !handling else { return }; handling = true
        defer { NSApp.terminate(nil) }
        guard installed() else { return }
        guard urls.count == 1, let parts = URLComponents(url: urls[0], resolvingAgainstBaseURL: false),
              parts.scheme == "softnix-local-agent", parts.host == "connect", let items = parts.queryItems,
              items.filter({ $0.name == "server" }).count == 1, items.filter({ $0.name == "code" }).count == 1,
              let server = items.first(where: { $0.name == "server" })?.value,
              let code = items.first(where: { $0.name == "code" })?.value, code.count >= 20, code.count <= 100,
              let remote = URLComponents(string: server), remote.scheme == "https", remote.host != nil,
              remote.user == nil, remote.password == nil, remote.query == nil, remote.fragment == nil,
              (remote.path == "" || remote.path == "/")
        else { alert("Invalid connection link", "Return to Bot Mode and choose Open local folder… again."); return }
        NSApp.activate(ignoringOtherApps: true)
        let chooser = NSOpenPanel()
        chooser.title = "Choose a folder for Softnix"; chooser.canChooseDirectories = true
        chooser.canChooseFiles = false; chooser.allowsMultipleSelection = false; chooser.prompt = "Allow Read & Write"
        chooser.message = "Share this folder with \(server) and allow Softnix to read and save files."
        guard chooser.runModal() == .OK, let folder = chooser.url else { return }
        do {
            let args = ["connect", "--server", server, "--folder", folder.path, "--write"]
            try ensureService()
            try run(args, code: code)
        } catch { alert("Could not connect folder", error.localizedDescription) }
    }
}
let app = NSApplication.shared
let delegate = AgentApp()
app.delegate = delegate
app.run()
