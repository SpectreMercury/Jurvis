// Press test: compiles with the app sources (-D UI_SMOKE), clicks ActionButtons with real mouse events and
// checks the busy state is on screen within 50 ms of the click, before the action has finished.
// The actions are local sleeps, so no engine is needed. Built and run by scripts/ui-press.sh.
import AppKit
import SwiftUI

let output = URL(fileURLWithPath: CommandLine.arguments.count > 1 ? CommandLine.arguments[1] : "build/ui-press")
var failures = 0
func expect(_ ok: Bool, _ what: String) { print((ok ? "ok   " : "FAIL ") + what); if !ok { failures += 1 } }
func pause(_ seconds: Double) async { try? await Task.sleep(for: .seconds(seconds)) }
struct Refused: LocalizedError { var errorDescription: String? { "拒绝" } }

@MainActor final class Probe: ObservableObject {
    var runs = 0
    var widths: Set<Int> = []
}

struct Bench: View {
    @ObservedObject var probe: Probe
    var fails: Bool
    var body: some View {
        ActionButton(title: "运行", busyTitle: "正在运行中", doneTitle: "完成", kind: .primary) {
            probe.runs += 1
            try await Task.sleep(for: .seconds(0.8))
            if fails { throw Refused() }
        }
        .onGeometryChange(for: Int.self) { Int($0.size.width.rounded()) } action: { probe.widths.insert($0) }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .background(Color.canvas)
    }
}

/// A picture on a drama page with the viewer over it, as `RootView` hosts it.
struct ViewerBench: View {
    @EnvironmentObject var store: AppStore
    var assets: [Asset]
    var body: some View {
        PosterTile(asset: assets[0], among: assets).frame(width: 90)
            .frame(maxWidth: .infinity, maxHeight: .infinity)
            .background(Color.canvas)
            .overlay { if let viewing = store.viewing { MediaViewer(viewing: viewing) } }
    }
}

@MainActor func press(_ window: NSWindow, key: UInt16) {
    if let event = NSEvent.keyEvent(with: .keyDown, location: .zero, modifierFlags: [], timestamp: ProcessInfo.processInfo.systemUptime, windowNumber: window.windowNumber,
                                    context: nil, characters: "", charactersIgnoringModifiers: "", isARepeat: false, keyCode: key) { NSApp.sendEvent(event) }
}

@MainActor func click(_ window: NSWindow) {
    let point = CGPoint(x: window.contentView!.bounds.midX, y: window.contentView!.bounds.midY)
    for type in [NSEvent.EventType.leftMouseDown, .leftMouseUp] {
        if let event = NSEvent.mouseEvent(with: type, location: point, modifierFlags: [], timestamp: ProcessInfo.processInfo.systemUptime,
                                          windowNumber: window.windowNumber, context: nil, eventNumber: 0, clickCount: 1, pressure: 1) { window.sendEvent(event) }
    }
}

/// Green pixels on screen: the busy ring is the only colour in the window, on any button or appearance.
@MainActor func signalPixels(_ host: NSView, saveAs name: String) -> Int {
    guard let rep = host.bitmapImageRepForCachingDisplay(in: host.bounds) else { return 0 }
    host.cacheDisplay(in: host.bounds, to: rep)
    try? rep.representation(using: .png, properties: [:])?.write(to: output.appendingPathComponent(name + ".png"))
    var count = 0
    for x in 0..<rep.pixelsWide { for y in 0..<rep.pixelsHigh {
        if let c = rep.colorAt(x: x, y: y)?.usingColorSpace(.sRGB), c.greenComponent - c.redComponent > 0.2 { count += 1 }
    } }
    return count
}

@MainActor func scenario(fails: Bool, store: AppStore) async -> (Probe, NSView, NSWindow) {
    let probe = Probe()
    let window = NSWindow(contentRect: CGRect(x: 0, y: 0, width: 320, height: 160), styleMask: [.titled], backing: .buffered, defer: false)
    let host = NSHostingView(rootView: Bench(probe: probe, fails: fails).environmentObject(store))
    window.contentView = host
    window.makeKeyAndOrderFront(nil)
    NSApp.activate()
    await pause(0.6)
    return (probe, host, window)
}

@MainActor func run() async {
    let store = AppStore()

    let (probe, host, window) = await scenario(fails: false, store: store)
    expect(signalPixels(host, saveAs: "1-idle") == 0, "idle button shows no busy ring")
    click(window)
    await pause(0.05)
    expect(signalPixels(host, saveAs: "2-busy-50ms") > 0, "busy ring is on screen 50 ms after the click")
    expect(probe.runs == 1, "the action started")
    click(window); click(window)
    await pause(1.0)
    expect(probe.runs == 1, "clicks while busy do not run it again (\(probe.runs) runs)")
    expect(signalPixels(host, saveAs: "3-done") == 0, "busy ring is gone once the action finished")
    await pause(1.2)
    expect(probe.widths.count == 1, "the button keeps its width through every state \(probe.widths.sorted())")
    click(window)
    await pause(0.05)
    expect(probe.runs == 2, "the button works again after finishing")
    window.close()

    let (failing, failHost, failWindow) = await scenario(fails: true, store: store)
    click(failWindow)
    await pause(0.05)
    expect(signalPixels(failHost, saveAs: "4-failing-busy") > 0, "a failing action shows busy too")
    await pause(1.2)
    expect(store.error == "拒绝", "the failure reaches the user (\(store.error ?? "nothing"))")
    expect(signalPixels(failHost, saveAs: "5-failed") == 0, "a failed button does not stay stuck busy")
    click(failWindow)
    await pause(0.05)
    expect(failing.runs == 2, "a failed action can be tried again")

    let assets = (1...3).map { Asset(id: "a\($0)", name: "画面 \($0)", path: "", mime: "image/png", kind: "image", size: 1, created_at: 0, job_id: nil, metadata: .null) }
    let viewerWindow = NSWindow(contentRect: CGRect(x: 0, y: 0, width: 640, height: 480), styleMask: [.titled], backing: .buffered, defer: false)
    viewerWindow.contentView = NSHostingView(rootView: ViewerBench(assets: assets).environmentObject(store))
    viewerWindow.makeKeyAndOrderFront(nil)
    await pause(0.6)
    click(viewerWindow)
    await pause(0.2)
    expect(store.viewing?.current.id == "a1", "clicking a picture opens it in the viewer")
    expect(store.viewing?.assets.count == 3, "the viewer gets the whole set")
    press(viewerWindow, key: 124); press(viewerWindow, key: 124); press(viewerWindow, key: 124)
    await pause(0.2)
    expect(store.viewing?.index == 2, "the right arrow steps and stops at the last one")
    press(viewerWindow, key: 123)
    await pause(0.2)
    expect(store.viewing?.index == 1, "the left arrow steps back")
    press(viewerWindow, key: 53)
    await pause(0.2)
    expect(store.viewing == nil, "Esc closes the viewer")

    print(failures == 0 ? "done" : "\(failures) failed")
    exit(failures == 0 ? 0 : 1)
}

@main enum UIPress {
    static func main() {
        try? FileManager.default.createDirectory(at: output, withIntermediateDirectories: true)
        let app = NSApplication.shared
        app.setActivationPolicy(.accessory)
        Task { @MainActor in await run() }
        app.run()
    }
}
