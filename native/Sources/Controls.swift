import SwiftUI

// MARK: - Button style

enum ButtonKind { case primary, secondary, ghost, danger }

enum ButtonScale {
    case small, regular, large
    var height: CGFloat {
        #if os(macOS)
        switch self { case .small: return 28; case .regular: return 34; case .large: return 38 }
        #else
        switch self { case .small: return 32; case .regular: return 40; case .large: return 48 }
        #endif
    }
    var padding: CGFloat { self == .small ? 12 : 16 }
    var font: Font { self == .small ? Font.jSmall : .jCallout }
}

/// Every button in the app: a rounded rectangle. Pressed and hover states come from here, so a click always answers at once.
struct JurvisButtonStyle: ButtonStyle {
    var kind: ButtonKind = .secondary
    var scale: ButtonScale = .regular
    var fill = false
    var square = false

    func makeBody(configuration: Configuration) -> some View {
        Surface(configuration: configuration, style: self)
    }

    private struct Surface: View {
        let configuration: Configuration
        let style: JurvisButtonStyle
        @Environment(\.isEnabled) private var enabled
        @State private var hovering = false

        var active: Bool { enabled && (hovering || configuration.isPressed) }
        var foreground: Color {
            switch style.kind {
            case .primary: return .inkReversed
            case .secondary: return .ink
            case .ghost: return active ? .ink : .inkSoft
            case .danger: return .danger
            }
        }
        var background: Color {
            switch style.kind {
            case .primary: return active ? Color.ink.opacity(0.82) : .ink
            case .secondary: return active ? .bubble : .card
            case .ghost: return active ? .bubble : .clear
            case .danger: return active ? .bubble : .card
            }
        }
        var edge: Color {
            switch style.kind {
            case .primary, .ghost: return .clear
            case .secondary: return .stroke
            case .danger: return .stroke
            }
        }

        var body: some View {
            let shape = RoundedRectangle(cornerRadius: Metrics.controlRadius, style: .continuous)
            configuration.label
                .font(style.scale.font).lineLimit(1)
                .foregroundStyle(foreground)
                .padding(.horizontal, style.square ? 0 : style.scale.padding)
                .frame(minWidth: style.square ? style.scale.height : nil, maxWidth: style.fill ? .infinity : nil, minHeight: style.scale.height)
                .background(shape.fill(background))
                .overlay(shape.strokeBorder(edge))
                #if os(iOS)
                // The pill may be smaller than a finger; the target never is.
                .frame(minWidth: 44, minHeight: 44)
                .contentShape(Rectangle())
                #else
                .contentShape(shape)
                #endif
                .opacity(enabled ? 1 : 0.38)
                .scaleEffect(configuration.isPressed ? 0.97 : 1)
                .animation(.easeOut(duration: 0.08), value: configuration.isPressed)
                .onHover { hovering = $0 }
        }
    }
}

extension ButtonStyle where Self == JurvisButtonStyle {
    static func jurvis(_ kind: ButtonKind = .secondary, _ scale: ButtonScale = .regular, fill: Bool = false) -> JurvisButtonStyle {
        JurvisButtonStyle(kind: kind, scale: scale, fill: fill)
    }
    /// A square, icon-only button.
    static func jurvisIcon(_ kind: ButtonKind = .ghost, _ scale: ButtonScale = .small) -> JurvisButtonStyle {
        JurvisButtonStyle(kind: kind, scale: scale, square: true)
    }
}

// MARK: - Action button

/// A button whose work takes time. It turns busy in the same frame as the click and stays busy until the
/// action and the refresh after it finish, so the screen never sits still waiting for the Mac.
/// The width is held by all three faces, so the label can change without the layout moving.
struct ActionButton: View {
    @EnvironmentObject var store: AppStore
    var title: String
    var busyTitle: String? = nil
    var doneTitle: String? = nil
    var icon: String? = nil
    var kind: ButtonKind = .secondary
    var scale: ButtonScale = .regular
    var fill = false
    /// Work that outlives the action itself, such as a job still stopping on the Mac.
    var isBusy = false
    var action: () async throws -> Void
    @State private var running = false
    @State private var done = false
    @State private var shakes = 0

    var busy: Bool { running || isBusy }

    var body: some View {
        Button(action: tap) {
            ZStack {
                face(title) { if let icon { Image(systemName: icon) } }.opacity(!busy && !done ? 1 : 0)
                face(busyTitle ?? title) { if busy { BusyRing() } else { Color.clear.frame(width: 13, height: 13) } }.opacity(busy ? 1 : 0)
                if let doneTitle { face(doneTitle) { Image(systemName: "checkmark") }.opacity(done && !busy ? 1 : 0) }
            }
        }
        .buttonStyle(.jurvis(kind, scale, fill: fill))
        .modifier(Shake(amount: CGFloat(shakes)))
        .accessibilityLabel(busy ? (busyTitle ?? title) : title)
        .accessibilityValue(busy ? "处理中" : "")
    }

    func face<Leading: View>(_ text: String, @ViewBuilder leading: () -> Leading) -> some View {
        HStack(spacing: 6) { leading(); Text(text) }
    }

    func tap() {
        guard !busy else { return }
        running = true
        Task {
            let started = ContinuousClock.now
            let succeeded = await store.perform(action)
            // A reply faster than the eye would otherwise read as a flicker.
            let shown = ContinuousClock.now - started
            if shown < .milliseconds(250) { try? await Task.sleep(for: .milliseconds(250) - shown) }
            running = false
            if !succeeded { withAnimation(.linear(duration: 0.3)) { shakes += 1 } }
            else if doneTitle != nil {
                done = true
                try? await Task.sleep(for: .seconds(1))
                done = false
            }
        }
    }
}

/// A short sideways shake that says the action failed.
private struct Shake: GeometryEffect {
    var amount: CGFloat
    var animatableData: CGFloat {
        get { amount }
        set { amount = newValue }
    }
    func effectValue(size: CGSize) -> ProjectionTransform {
        ProjectionTransform(CGAffineTransform(translationX: 4 * sin(amount * .pi * 3), y: 0))
    }
}

/// Label for a `Menu` or link styled as a button, with the same busy face as `ActionButton`.
struct ButtonFace: View {
    var title: String
    var icon: String? = nil
    var busy = false
    var body: some View {
        HStack(spacing: 6) {
            if busy { BusyRing() } else if let icon { Image(systemName: icon) }
            Text(title)
        }
    }
}

// MARK: - Progress

/// The signal ring: something is happening right now.
struct BusyRing: View {
    var size: CGFloat = 13
    var tint: Color = .signal
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    var body: some View {
        // Driven by the clock, not a repeating animation, which stops when a lazy stack recycles its row.
        TimelineView(.animation(paused: reduceMotion)) { context in
            let turn = context.date.timeIntervalSinceReferenceDate.truncatingRemainder(dividingBy: 0.7) / 0.7
            ZStack {
                Circle().stroke(tint.opacity(0.28), lineWidth: 2)
                Circle().trim(from: 0, to: 0.28).stroke(tint, style: StrokeStyle(lineWidth: 2, lineCap: .round))
                    .rotationEffect(.degrees(turn * 360))
            }
        }
        .frame(width: size, height: size)
        .accessibilityHidden(true)
    }
}

/// The green dot, breathing: Jurvis is working on this.
struct SignalDot: View {
    var size: CGFloat = 7
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    var body: some View {
        TimelineView(.animation(paused: reduceMotion)) { context in
            let phase = (sin(context.date.timeIntervalSinceReferenceDate * 2 * .pi / 1.2) + 1) / 2
            Circle().fill(Color.signal)
                .opacity(reduceMotion ? 1 : 0.45 + 0.55 * phase)
                .scaleEffect(reduceMotion ? 1 : 0.8 + 0.2 * phase)
        }
        .frame(width: size, height: size)
        .accessibilityHidden(true)
    }
}

struct ProgressBar: View {
    var value: Double
    var tint: Color = .signal
    var body: some View {
        GeometryReader { geometry in
            ZStack(alignment: .leading) {
                Capsule().fill(Color.bubble)
                Capsule().fill(tint).frame(width: geometry.size.width * min(max(value, 0), 1))
            }
        }
        .frame(height: 4)
        .accessibilityValue("\(Int(value * 100))%")
    }
}

/// A full-area loading state, for a screen that has nothing to show yet.
struct LoadingState: View {
    var text: String
    var body: some View {
        HStack(spacing: 8) {
            BusyRing()
            Text(text).font(.jSmall).foregroundStyle(Color.inkSoft)
        }
        .frame(maxWidth: .infinity, minHeight: 120)
    }
}

// MARK: - Controls

/// Tabs on a grey track; the selected one is a white pill. Every tab can carry an icon.
struct SegmentedControl: View {
    var options: [String]
    @Binding var selection: String
    var icons: [String: String] = [:]
    /// Tabs share the width of the control instead of hugging their labels.
    var fill = false
    #if os(macOS)
    private let height: CGFloat = 28
    #else
    private let height: CGFloat = 32
    #endif
    var body: some View {
        HStack(spacing: 2) {
            ForEach(options, id: \.self) { option in
                let on = option == selection
                Button { selection = option } label: {
                    HStack(spacing: 6) {
                        if let icon = icons[option] { Image(systemName: icon).font(.system(size: 12)) }
                        Text(option).lineLimit(1).fixedSize()
                    }
                    .font(.jCallout)
                    .foregroundStyle(on ? Color.ink : .inkSoft)
                    .padding(.horizontal, 12).frame(maxWidth: fill ? .infinity : nil).frame(height: height)
                    .background {
                        if on {
                            RoundedRectangle(cornerRadius: 6, style: .continuous).fill(Color.card)
                                .overlay(RoundedRectangle(cornerRadius: 6, style: .continuous).strokeBorder(Color.stroke, lineWidth: 0.5))
                        }
                    }
                    .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
                .accessibilityLabel(option)
                .accessibilityAddTraits(on ? .isSelected : [])
            }
        }
        .padding(2)
        .background(Color.bubble, in: RoundedRectangle(cornerRadius: 8, style: .continuous))
        .fixedSize(horizontal: !fill, vertical: true)
    }
}

/// Filters as chips in a row that scrolls sideways; the chosen one is ink. Each can show how many it holds.
struct FilterChips: View {
    var options: [String]
    @Binding var selection: String
    var icons: [String: String] = [:]
    var counts: [String: Int] = [:]
    var body: some View {
        ScrollView(.horizontal, showsIndicators: false) {
            HStack(spacing: 8) {
                ForEach(options, id: \.self) { option in
                    let on = option == selection
                    Button { selection = option } label: {
                        HStack(spacing: 6) {
                            if let icon = icons[option] { Image(systemName: icon).font(.system(size: 12)) }
                            Text(option).lineLimit(1).fixedSize()
                            if let count = counts[option] {
                                Text("\(count)").monospacedDigit().foregroundStyle(on ? Color.inkReversed.opacity(0.65) : .inkFaint)
                            }
                        }
                        .font(.jCallout).foregroundStyle(on ? Color.inkReversed : .ink)
                        .padding(.horizontal, 12).frame(height: 32)
                        .background(on ? Color.ink : .card, in: Capsule())
                        .overlay(Capsule().strokeBorder(on ? Color.clear : .stroke))
                        .contentShape(Capsule())
                    }
                    .buttonStyle(.plain)
                    .accessibilityLabel(counts[option].map { "\(option)，\($0) 项" } ?? option)
                    .accessibilityAddTraits(on ? .isSelected : [])
                }
            }
            .padding(.vertical, 1)
        }
    }
}

/// Cancel and confirm for a sheet, pinned to its bottom edge.
struct SheetFooter<Content: View>: View {
    @ViewBuilder var content: () -> Content
    var body: some View {
        HStack(spacing: 8) { Spacer(); content() }
            .padding(.horizontal, 16).padding(.vertical, 12)
            .background(Color.canvas)
            .overlay(alignment: .top) { Rectangle().fill(Color.hairline).frame(height: 1) }
    }
}

/// A list row in a settings-style card: title and detail on the left, an accessory on the right.
struct InfoRow<Accessory: View>: View {
    var title: String
    var detail: String? = nil
    @ViewBuilder var accessory: () -> Accessory
    var body: some View {
        HStack(spacing: 12) {
            VStack(alignment: .leading, spacing: 3) {
                Text(title).font(.jBody)
                if let detail { Text(detail).font(.jSmall).foregroundStyle(Color.inkFaint) }
            }
            Spacer(minLength: 8)
            accessory()
        }
        .padding(.horizontal, 16).padding(.vertical, 11).frame(minHeight: 52)
    }
}

extension InfoRow where Accessory == Text {
    init(title: String, value: String) {
        self.init(title: title) { Text(value).font(.jCallout).foregroundStyle(Color.inkSoft) }
    }
}

/// Rows in one card, split by hairlines.
struct RowGroup<Content: View>: View {
    var title: String
    var footnote: String? = nil
    @ViewBuilder var content: () -> Content
    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Eyebrow(text: title).padding(.leading, 4)
            Group(subviews: content()) { rows in
                VStack(spacing: 0) {
                    ForEach(Array(rows.enumerated()), id: \.offset) { index, row in
                        if index > 0 { Rectangle().fill(Color.hairline).frame(height: 1).padding(.leading, 16) }
                        row
                    }
                }
                .frame(maxWidth: .infinity, alignment: .leading)
                .surface()
            }
            if let footnote { Text(footnote).font(.jSmall).foregroundStyle(Color.inkSoft).fixedSize(horizontal: false, vertical: true) }
        }
    }
}

/// Copies text and says so: the icon turns into a check for a moment.
struct CopyButton: View {
    var text: String
    var title = "复制"
    var iconOnly = false
    @State private var copied = false
    var body: some View {
        Group {
            if iconOnly {
                Button(action: copy) { Image(systemName: copied ? "checkmark" : "doc.on.doc") }.buttonStyle(.jurvisIcon())
            } else {
                Button(action: copy) { Label(copied ? "已复制" : title, systemImage: copied ? "checkmark" : "doc.on.doc") }.buttonStyle(.jurvis(.ghost, .small))
            }
        }
        .help(title).accessibilityLabel(copied ? "已复制" : title)
    }
    func copy() {
        copyText(text); copied = true
        Task { try? await Task.sleep(for: .seconds(1.2)); copied = false }
    }
}
