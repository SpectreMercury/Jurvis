// Typing check: hosts the real chat (bound to a code project) in a key window against the running engine,
// types ASCII and Chinese IME input into each text field while the store keeps polling,
// and fails if text is lost, composition is broken or the field loses focus.
// Built and run by scripts/ui-typing.sh.
import AppKit
import Combine
import SwiftUI

var failures = 0
func expect(_ ok: Bool, _ what: String) { print(ok ? "ok   \(what)" : "FAIL \(what)"); if !ok { failures += 1 } }

func views<T: NSView>(_ type: T.Type, in root: NSView) -> [T] {
    var out = [T](); var stack = [root]
    while let v = stack.popLast() { if let t = v as? T { out.append(t) }; stack.append(contentsOf: v.subviews) }
    return out
}

@MainActor func pause(_ seconds: Double) async { try? await Task.sleep(for: .seconds(seconds)) }

/// Types into whatever text view currently has focus in `window`, like a keyboard would.
@MainActor func type(_ text: String, in window: NSWindow) async {
    for ch in text {
        guard let editor = window.firstResponder as? NSTextView else { return }
        editor.insertText(String(ch), replacementRange: NSRange(location: NSNotFound, length: 0))
        await pause(0.15)
    }
}

/// Real key-down/key-up events through the window, like the hardware keyboard. Returns the slowest keystroke in ms.
@MainActor func press(_ text: String, in window: NSWindow) async -> Double {
    var slowest = 0.0
    for ch in text {
        let start = Date()
        for kind in [NSEvent.EventType.keyDown, .keyUp] {
            guard let event = NSEvent.keyEvent(with: kind, location: .zero, modifierFlags: [], timestamp: ProcessInfo.processInfo.systemUptime,
                                               windowNumber: window.windowNumber, context: nil, characters: String(ch),
                                               charactersIgnoringModifiers: String(ch), isARepeat: false, keyCode: 0) else { continue }
            window.sendEvent(event)
        }
        await pause(0.01)
        slowest = max(slowest, Date().timeIntervalSince(start) * 1000)
        await pause(0.09)
    }
    return slowest
}

/// A real mouse click at a point in window coordinates.
@MainActor func click(_ point: CGPoint, in window: NSWindow) async {
    for kind in [NSEvent.EventType.leftMouseDown, .leftMouseUp] {
        if let event = NSEvent.mouseEvent(with: kind, location: point, modifierFlags: [], timestamp: ProcessInfo.processInfo.systemUptime,
                                          windowNumber: window.windowNumber, context: nil, eventNumber: 0, clickCount: 1, pressure: 1) {
            window.sendEvent(event)
        }
        await pause(0.05)
    }
    await pause(0.3)
}

/// Pinyin composition: marked text stays while the app refreshes, then commits.
@MainActor func compose(_ pinyin: String, commit: String, in window: NSWindow) async -> Bool {
    guard let editor = window.firstResponder as? NSTextView else { return false }
    for i in 1...pinyin.count {
        let part = String(pinyin.prefix(i))
        editor.setMarkedText(part, selectedRange: NSRange(location: part.utf16.count, length: 0), replacementRange: NSRange(location: NSNotFound, length: 0))
        await pause(0.4)
    }
    await pause(2.5) // spans at least one background refresh
    let survived = (window.firstResponder as? NSTextView)?.hasMarkedText() ?? false
    (window.firstResponder as? NSTextView)?.insertText(commit, replacementRange: NSRange(location: NSNotFound, length: 0))
    return survived
}

@MainActor func run() async {
    let store = AppStore()
    await store.start()
    guard store.connected else { print("FAIL engine not reachable"); exit(1) }
    await store.loadCodeProjects()
    await store.newChat(project: store.codeProjects.first?.id)
    let window = NSWindow(contentRect: CGRect(x: 0, y: 0, width: Double(CommandLine.arguments.dropFirst().first ?? "1180") ?? 1180, height: 800),
                          styleMask: [.titled, .resizable, .closable, .fullSizeContentView], backing: .buffered, defer: false)
    let host = NSHostingView(rootView: RootView().environmentObject(store).tint(Color.jurvisAccent))
    window.contentView = host
    window.makeKeyAndOrderFront(nil)
    NSApp.activate()
    await pause(4)

    // Idle polling must not re-render the screen under the user's cursor.
    var published = 0
    var sources: [String: Int] = [:]
    var probes: [AnyCancellable] = []
    func probe<P: Publisher>(_ name: String, _ p: P) where P.Failure == Never { probes.append(p.dropFirst().sink { _ in sources[name, default: 0] += 1 }) }
    probe("snapshot", store.$snapshot); probe("messages", store.$messages); probe("selectedSession", store.$selectedSession)
    probe("connected", store.$connected); probe("connectionStatus", store.$connectionStatus); probe("codeProjectID", store.$codeProjectID)
    probe("section", store.$section); probe("mediaCache", store.$mediaCache); probe("error", store.$error); probe("api", store.$api)
    probe("files", store.$files); probe("modelState", store.$modelState); probe("devices", store.$devices); probe("components", store.$components)
    let watch = store.objectWillChange.sink { published += 1 }
    await pause(6.5)
    watch.cancel(); probes.removeAll()
    if !sources.isEmpty { print("     published by: \(sources)") }
    expect(published == 0, "idle polling publishes no changes (\(published) in 6.5 s)")

    // The composer: a growing text field.
    // The composer is the widest editable field (the sidebar search field is narrower).
    guard let field = views(NSTextField.self, in: host).filter({ $0.isEditable }).max(by: { $0.frame.width < $1.frame.width }) else { print("FAIL no composer"); exit(1) }
    let frame = field.convert(field.bounds, to: nil)
    print("     composer \(Int(frame.width))x\(Int(frame.height))")
    window.makeFirstResponder(nil)
    await click(CGPoint(x: frame.midX, y: frame.midY), in: window)
    expect((window.firstResponder as? NSTextView)?.delegate === field, "clicking the composer focuses it")
    let editor = window.firstResponder
    let lag = await press("fix calc ", in: window)
    await pause(2.5)
    expect(window.firstResponder === editor, "composer keeps focus across a refresh")
    expect(lag < 120, "keystrokes stay responsive (slowest \(Int(lag)) ms)")
    let composed = await compose("xiufu", commit: "修复", in: window)
    expect(composed, "composer keeps IME composition across a refresh")
    await type(" bug", in: window)
    await pause(2.5)
    let text = (window.firstResponder as? NSTextView)?.string ?? "(focus lost)"
    expect(text == "fix calc 修复 bug", "composer text is intact: \(text.debugDescription)")

    // ⌘V with a screenshot on the clipboard attaches it; with text on the clipboard it is left to the field.
    let board = NSPasteboard.general
    let saved = board.pasteboardItems?.map { item in item.types.compactMap { type in item.data(forType: type).map { (type, $0) } } } ?? []
    let shot = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: 8, pixelsHigh: 8, bitsPerSample: 8, samplesPerPixel: 4, hasAlpha: true,
                                isPlanar: false, colorSpaceName: .deviceRGB, bytesPerRow: 0, bitsPerPixel: 0)!
    let before = store.snapshot?.assets.count ?? 0
    func commandV() {
        if let event = NSEvent.keyEvent(with: .keyDown, location: .zero, modifierFlags: .command, timestamp: ProcessInfo.processInfo.systemUptime,
                                        windowNumber: window.windowNumber, context: nil, characters: "v", charactersIgnoringModifiers: "v",
                                        isARepeat: false, keyCode: 9) { NSApp.sendEvent(event) }
    }
    board.clearContents(); board.setString("just text", forType: .string)
    commandV(); await pause(2)
    expect((store.snapshot?.assets.count ?? 0) == before, "text on the clipboard is not uploaded as an image")
    board.clearContents(); board.setData(shot.representation(using: .png, properties: [:]), forType: .png)
    commandV()
    var pasted = false
    for _ in 0..<20 where !pasted { await pause(0.5); pasted = store.snapshot?.assets.contains { $0.name == "粘贴的图片.png" } ?? false }
    expect(pasted, "⌘V with an image on the clipboard uploads it as an attachment")
    expect((window.firstResponder as? NSTextView)?.string == "fix calc 修复 bug", "pasting an image leaves the typed text alone")
    let thumbs = views(NSView.self, in: host).contains { $0.accessibilityLabel()?.hasPrefix("移除 ") ?? false }
    if !thumbs { print("     (attachment thumbnail not found in the view tree; checked via the engine instead)") }
    board.clearContents()
    for item in saved { let restored = NSPasteboardItem(); for (type, data) in item { restored.setData(data, forType: type) }; board.writeObjects([restored]) }

    print(failures == 0 ? "done: typing and pasting work" : "done: \(failures) failure(s)")
    exit(failures == 0 ? 0 : 1)
}

@main enum UITyping {
    static func main() {
        let app = NSApplication.shared
        app.setActivationPolicy(.accessory)
        Task { @MainActor in await run() }
        app.run()
    }
}
