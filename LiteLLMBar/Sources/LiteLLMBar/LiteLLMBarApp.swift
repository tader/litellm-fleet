import SwiftUI
import ServiceManagement
import AppKit

@main
struct LiteLLMBarApp: App {
    @StateObject private var state = FleetState()

    var body: some Scene {
        MenuBarExtra {
            MenuContent(state: state)
        } label: {
            Image(systemName: state.status.symbol)
        }
    }
}

enum FleetStatus {
    case running, stopped, busy, dockerDown

    var symbol: String {
        switch self {
        case .running: return "circle.inset.filled"
        case .stopped: return "circle"
        case .busy: return "circle.dotted"
        case .dockerDown: return "exclamationmark.circle"
        }
    }

    var label: String {
        switch self {
        case .running: return "Router: running"
        case .stopped: return "Router: stopped"
        case .busy: return "Working…"
        case .dockerDown: return "Docker not available"
        }
    }
}

@MainActor
final class FleetState: ObservableObject {
    @Published var status: FleetStatus = .stopped
    @Published var loginItemEnabled = SMAppService.mainApp.status == .enabled
    @Published var lastError: String?

    private var timer: Timer?

    var repoPath: String {
        UserDefaults.standard.string(forKey: "RepoPath")
            ?? FileManager.default.homeDirectoryForCurrentUser
                .appendingPathComponent("litellm-fleet").path
    }
    var composeFile: String { repoPath + "/generated/docker-compose.yml" }

    init() {
        timer = Timer.scheduledTimer(withTimeInterval: 5, repeats: true) { _ in
            Task { @MainActor in await self.poll() }
        }
        Task { @MainActor in
            await poll()
            if status == .stopped { await start() }  // at login: bring fleet up
        }
    }

    func poll(force: Bool = false) async {
        // The timer-driven poll must not clobber status mid-operation, but the
        // refresh a compose op issues on completion has to run even while busy.
        if status == .busy && !force { return }
        var req = URLRequest(url: URL(string: "http://127.0.0.1:4000/health/liveliness")!)
        req.timeoutInterval = 3
        do {
            let (_, resp) = try await URLSession.shared.data(for: req)
            status = (resp as? HTTPURLResponse)?.statusCode == 200 ? .running : .stopped
        } catch {
            status = .stopped
        }
    }

    func start() async { await compose(["up", "-d", "--remove-orphans"]) }
    func stop() async { await compose(["down", "--remove-orphans"]) }
    // --force-recreate so a restart also applies changed configs, not just
    // bounces the existing containers (plain `restart` ignores config edits).
    func restart() async { await compose(["up", "-d", "--force-recreate", "--remove-orphans"]) }

    private func compose(_ args: [String]) async {
        status = .busy
        lastError = nil
        let result = await runProcess(
            "/usr/bin/env", ["docker-compose", "-f", composeFile] + args)
        if result.exitCode != 0 {
            lastError = String(result.stderr.suffix(300))
            status = result.stderr.contains("docker")
                && result.stderr.contains("daemon") ? .dockerDown : .stopped
            return  // status is definitive; a poll would only overwrite it
        }
        await poll(force: true)  // clear .busy set above
    }

    func regenerate() async {
        status = .busy
        let result = await runProcess(
            "/usr/bin/env", ["uv", "run", "generate.py"], cwd: repoPath)
        if result.exitCode != 0 {
            lastError = String(result.stderr.suffix(300))
        } else {
            await compose(["up", "-d", "--remove-orphans"])  // apply new configs
            return
        }
        await poll(force: true)  // clear .busy set above
    }

    func copyMasterKey() {
        guard let env = try? String(contentsOfFile: repoPath + "/generated/.env", encoding: .utf8),
              let line = env.split(separator: "\n").first(where: { $0.hasPrefix("LITELLM_MASTER_KEY=") })
        else {
            lastError = "master key not found in generated/.env"
            return
        }
        let key = String(line.dropFirst("LITELLM_MASTER_KEY=".count))
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString(key, forType: .string)
    }

    func toggleLoginItem() {
        do {
            if loginItemEnabled {
                try SMAppService.mainApp.unregister()
            } else {
                try SMAppService.mainApp.register()
            }
            loginItemEnabled = SMAppService.mainApp.status == .enabled
        } catch {
            lastError = "login item: \(error.localizedDescription)"
        }
    }

    func openLogs() {
        let script = "tell application \"Terminal\" to do script \"docker-compose -f '\(composeFile)' logs -f --tail 100\""
        if let osa = NSAppleScript(source: script) { osa.executeAndReturnError(nil) }
    }

    func editConfig() {
        NSWorkspace.shared.open(URL(fileURLWithPath: repoPath + "/main.yaml"))
    }

    func chooseRepoFolder() {
        let panel = NSOpenPanel()
        panel.canChooseDirectories = true
        panel.canChooseFiles = false
        panel.allowsMultipleSelection = false
        panel.directoryURL = URL(fileURLWithPath: repoPath)
        panel.message = "Select the repo folder (the one containing main.yaml)"
        panel.prompt = "Use Folder"
        NSApp.activate(ignoringOtherApps: true)
        if panel.runModal() == .OK, let url = panel.url {
            UserDefaults.standard.set(url.path, forKey: "RepoPath")
            Task { await poll(force: true) }  // re-check against the new path's fleet
        }
    }
}

struct MenuContent: View {
    @ObservedObject var state: FleetState

    var body: some View {
        Text(state.status.label)
        if let err = state.lastError {
            Text(err).font(.caption)
        }
        Divider()
        if state.status == .running {
            Button("Stop LiteLLM") { Task { await state.stop() } }
            Button("Restart LiteLLM") { Task { await state.restart() } }
        } else {
            Button("Start LiteLLM") { Task { await state.start() } }
                .disabled(state.status == .busy)
        }
        Button("Copy Master Key") { state.copyMasterKey() }
        Divider()
        Button("Regenerate Configs") { Task { await state.regenerate() } }
        Button("Edit Config") { state.editConfig() }
        Button("Open Logs") { state.openLogs() }
        Button("Set Repo Folder…") { state.chooseRepoFolder() }
        Divider()
        Toggle("Start at Login", isOn: Binding(
            get: { state.loginItemEnabled },
            set: { _ in state.toggleLoginItem() }
        ))
        Divider()
        Button("Quit (fleet keeps running)") { NSApplication.shared.terminate(nil) }
    }
}

struct ProcessResult {
    let exitCode: Int32
    let stdout: String
    let stderr: String
}

func runProcess(_ launchPath: String, _ args: [String], cwd: String? = nil) async -> ProcessResult {
    await withCheckedContinuation { cont in
        DispatchQueue.global().async {
            let p = Process()
            p.executableURL = URL(fileURLWithPath: launchPath)
            p.arguments = args
            if let cwd { p.currentDirectoryURL = URL(fileURLWithPath: cwd) }
            var env = ProcessInfo.processInfo.environment
            let home = FileManager.default.homeDirectoryForCurrentUser.path
            env["PATH"] = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:" + (env["PATH"] ?? "")
            env["HOME"] = home
            p.environment = env
            let out = Pipe(), err = Pipe()
            p.standardOutput = out
            p.standardError = err
            do {
                try p.run()
                // Drain pipes while the process runs; reading only after
                // waitUntilExit deadlocks once a pipe buffer fills.
                var stdoutData = Data(), stderrData = Data()
                let group = DispatchGroup()
                group.enter()
                DispatchQueue.global().async {
                    stdoutData = out.fileHandleForReading.readDataToEndOfFile()
                    group.leave()
                }
                group.enter()
                DispatchQueue.global().async {
                    stderrData = err.fileHandleForReading.readDataToEndOfFile()
                    group.leave()
                }
                p.waitUntilExit()
                group.wait()
                cont.resume(returning: ProcessResult(
                    exitCode: p.terminationStatus,
                    stdout: String(data: stdoutData, encoding: .utf8) ?? "",
                    stderr: String(data: stderrData, encoding: .utf8) ?? ""))
            } catch {
                cont.resume(returning: ProcessResult(exitCode: -1, stdout: "", stderr: error.localizedDescription))
            }
        }
    }
}
