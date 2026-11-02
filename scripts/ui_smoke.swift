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
    if let code = snapshot.jobs.first(where: { $0.kind == "code" && $0.status == "succeeded" }) {
        await capture("code-review", size: CGSize(width: 920, height: 720), CodeReviewView(jobID: code.id).environmentObject(store))
    }
    // Narrow window: layout must not break at the minimum size.
    store.section = .coding
    await capture("coding-narrow", size: CGSize(width: 920, height: 620), root)
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
