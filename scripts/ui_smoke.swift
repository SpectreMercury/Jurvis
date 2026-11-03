// UI smoke test: compiles with the app sources (-D UI_SMOKE), connects to the running
// engine, renders every screen offscreen and writes PNG snapshots. A crash in any view
// (for example in AVKit) fails the run. Built and run by scripts/ui-smoke.sh.
import AppKit
import SwiftUI

let output = URL(fileURLWithPath: CommandLine.arguments.count > 1 ? CommandLine.arguments[1] : "build/ui-smoke")

@MainActor func capture<V: View>(_ name: String, size: CGSize = CGSize(width: 1280, height: 820), dark: Bool = false, wait: Double = 2.5, _ view: V) async {
    let window = NSWindow(contentRect: CGRect(origin: .zero, size: size), styleMask: [.titled, .resizable, .closable, .miniaturizable, .fullSizeContentView], backing: .buffered, defer: false)
    window.appearance = NSAppearance(named: dark ? .darkAqua : .aqua)
    let host = NSHostingView(rootView: view)
    window.contentView = host
    window.setFrameOrigin(CGPoint(x: -10000, y: -10000))
    window.orderBack(nil)
    try? await Task.sleep(for: .seconds(wait))
    host.layoutSubtreeIfNeeded()
    guard let rep = host.bitmapImageRepForCachingDisplay(in: host.bounds) else { print("FAIL \(name): no bitmap"); return }
    host.cacheDisplay(in: host.bounds, to: rep)
    try? rep.representation(using: .png, properties: [:])?.write(to: output.appendingPathComponent(name + ".png"))
    window.orderOut(nil)
    print("ok   \(name)")
}

@MainActor func run() async {
    let store = AppStore()
    await store.start()
    guard store.connected, let snapshot = store.snapshot else { print("FAIL engine not reachable: \(store.error ?? "")"); exit(1) }
    await store.reloadSettings()
    let root = RootView().environmentObject(store).tint(Color.jurvisAccent)

    for section in Section.allCases {
        store.section = section
        if section == .assistant { await store.newChat() }
        await capture("section-\(section.id)", root)
    }
    if let session = snapshot.sessions.first {
        await store.selectSession(session.id)
        await capture("chat-session", root)
        await capture("chat-session-dark", dark: true, root)
    }
    store.section = .tasks
    await capture("tasks-dark", dark: true, root)

    // Media detail sheets: the screen that used to crash on video.
    for asset in snapshot.assets {
        let kind = asset.isVideo ? "video" : "image"
        await capture("media-\(kind)-\(asset.kind)-\(asset.id.prefix(6))", size: CGSize(width: 700, height: 760), wait: 3,
                       MediaDetail(asset: asset).environmentObject(store))
    }
    if let project = snapshot.projects.first {
        await capture("drama-detail", NavigationStack { ProjectDetailView(projectID: project.id) }.environmentObject(store))
    }
    if let code = snapshot.jobs.first(where: { $0.kind == "code" && $0.status == "succeeded" && ($0.result["change_count"].number ?? 0) > 0 }) {
        await capture("code-change", size: CGSize(width: 820, height: 640), ScrollView { ChangeSetCard(jobID: code.id).padding(20) }.environmentObject(store))
    }
    // A conversation bound to a code project, and the home screen with a project picked.
    if let session = snapshot.sessions.first(where: { $0.code_project_id != nil }) {
        await store.selectSession(session.id)
        await capture("chat-code-session", root)
    }
    // Commands waiting for approval and finished, inside a goal.
    let sample = #"""
    [{"id":"m1","session_id":"s","role":"assistant","content":"","status":"streaming","created_at":0,"metadata":{"goal":{"status":"running","steps":3,"reviews":0},"steps":[
      {"text":"先把插件克隆下来。","calls":[{"id":"a","name":"run_command","label":"运行命令","status":"ok","summary":"完成 · 1.2 秒","command":"git clone https://github.com/example/demo-nodes.git","reason":"克隆插件仓库","cwd":"/Users/me/jurvis/.runtime/ComfyUI/custom_nodes","console":"Cloning into 'demo-nodes'...\ndone.","exit_code":0}]},
      {"text":"","calls":[{"id":"b","name":"run_command","label":"运行命令","status":"awaiting","command":"/Users/me/jurvis/.venv/bin/python -m pip install -r demo-nodes/requirements.txt","reason":"安装插件依赖","cwd":"/Users/me/jurvis/.runtime/ComfyUI/custom_nodes"}]}]}},
     {"id":"m2","session_id":"s","role":"assistant","content":"测试已全部通过。","status":"complete","created_at":1,"metadata":{"goal":{"status":"done","steps":6,"reviews":1},"steps":[
      {"text":"","calls":[{"id":"c","name":"code_read","label":"阅读源码","status":"ok","summary":"calc.py · 6 行"},{"id":"d","name":"run_command","label":"运行命令","status":"error","summary":"失败 · 退出码 1","command":"python3 -m unittest discover -s tests","reason":"运行测试","cwd":"/tmp/goal-code","console":"F..\nFAIL: test_total\nAssertionError: 6 != 5","exit_code":1}]},
      {"text":"测试已全部通过。","calls":[]}]}}]
    """#
    if let messages = try? JSONDecoder().decode([Message].self, from: Data(sample.utf8)) {
        await capture("goal-commands", size: CGSize(width: 820, height: 900), ScrollView {
            VStack(alignment: .leading, spacing: 30) { ForEach(messages) { AssistantMessage(message: $0, open: { _ in }) } }.padding(24)
        }.background(Color.canvas).environmentObject(store))
    } else { print("FAIL goal sample did not decode") }
    await store.loadCodeProjects()
    await store.newChat(project: store.codeProjects.first?.id)
    await capture("home-with-project", root)
    await store.newChat()
    await capture("home-narrow", size: CGSize(width: 820, height: 600), root)
    await capture("home-dark", dark: true, root)
    print("done \(output.path)")
    exit(0)
}

@main enum UISmoke {
    static func main() {
        try? FileManager.default.createDirectory(at: output, withIntermediateDirectories: true)
        let app = NSApplication.shared
        app.setActivationPolicy(.prohibited)
        Task { @MainActor in await run() }
        app.run()
    }
}
