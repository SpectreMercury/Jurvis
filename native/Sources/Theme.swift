import SwiftUI

// MARK: - Tokens

/// Black, white and one green. Ink carries every action; green only ever means
/// running, done, or waiting for you. Nothing else in the app has a colour.
extension Color {
    // Values follow Vercel's Geist: near-black ink on white, hairline borders, no drop shadows.
    /// The ground of the window: sidebar and the frame around the panels.
    static var chrome: Color { dynamic(0xFAFAFA, 0x000000) }
    /// The surface of every panel and screen.
    static var canvas: Color { dynamic(0xFFFFFF, 0x0A0A0A) }
    /// Raised surfaces: cards, the composer.
    static var card: Color { dynamic(0xFFFFFF, 0x111111) }
    /// Fill: user bubbles, segmented tracks, icon wells.
    static var bubble: Color { dynamic(0xF2F2F2, 0x1A1A1A) }
    /// Behind pictures, dark in either appearance so artwork always reads the same.
    static var stage: Color { dynamic(0x0A0A0A, 0x0A0A0A) }
    static var ink: Color { dynamic(0x171717, 0xEDEDED) }
    static var inkReversed: Color { dynamic(0xFFFFFF, 0x0A0A0A) }
    static var inkSoft: Color { dynamic(0x4D4D4D, 0xA1A1A1) }
    static var inkFaint: Color { dynamic(0x707070, 0x8F8F8F) }
    /// The one green: running, done, needs you.
    static var signal: Color { dynamic(0x297A3A, 0x62C073) }
    static var signalText: Color { signal }
    static var signalSoft: Color { dynamic(0xEBFAEB, 0x0F2E18) }
    static var success: Color { signal }
    /// Errors, as text only.
    static var danger: Color { dynamic(0xCB2A2F, 0xFF6166) }
    static var hairline: Color { dynamic(0xEBEBEB, 0x242424) }
    static var stroke: Color { dynamic(0xDEDEDE, 0x333333) }
    static var subtleFill: Color { dynamic(0xFAFAFA, 0x111111) }
    /// A selected or hovered row.
    static var selection: Color { dynamic(0xEBEBEB, 0x1F1F1F) }
    /// The ring of a step that has not started.
    static var ring: Color { dynamic(0xC9C9C9, 0x5A5A5A) }
    static var shadow: Color { .clear }

    private static func dynamic(_ light: UInt32, _ dark: UInt32) -> Color {
        func parts(_ hex: UInt32) -> (Double, Double, Double) {
            (Double(hex >> 16 & 0xFF) / 255, Double(hex >> 8 & 0xFF) / 255, Double(hex & 0xFF) / 255)
        }
        #if os(macOS)
        return Color(nsColor: NSColor(name: nil) { appearance in
            let c = parts(appearance.bestMatch(from: [.darkAqua, .aqua]) == .darkAqua ? dark : light)
            return NSColor(red: c.0, green: c.1, blue: c.2, alpha: 1)
        })
        #else
        return Color(uiColor: UIColor { traits in
            let c = parts(traits.userInterfaceStyle == .dark ? dark : light)
            return UIColor(red: c.0, green: c.1, blue: c.2, alpha: 1)
        })
        #endif
    }
}

/// The system sans in two weights only, regular and medium. Sizes 12 to 15, titles up to 26.
/// The Mac uses fixed sizes; iOS keeps Dynamic Type through the matching text styles.
extension Font {
    #if os(macOS)
    static let jMicro = Font.system(size: 12)
    static let jSmall = Font.system(size: 12)
    static let jCallout = Font.system(size: 13)
    static let jBody = Font.system(size: 14)
    static let jHeading = Font.system(size: 14, weight: .medium)
    static let jSection = Font.system(size: 15, weight: .medium)
    static let jTitle = Font.system(size: 20, weight: .medium)
    static let jDisplay = Font.system(size: 26, weight: .medium)
    static let jMono = Font.system(size: 12.5, design: .monospaced)
    static let jLabel = Font.system(size: 12)
    #else
    static let jMicro = Font.caption
    static let jSmall = Font.footnote
    static let jCallout = Font.subheadline
    static let jBody = Font.subheadline
    static let jHeading = Font.subheadline.weight(.medium)
    static let jSection = Font.body.weight(.medium)
    static let jTitle = Font.title3.weight(.medium)
    static let jDisplay = Font.title2.weight(.medium)
    static let jMono = Font.system(.footnote, design: .monospaced)
    static let jLabel = Font.caption
    #endif
}

enum Metrics {
    static let radius: CGFloat = 12
    static let smallRadius: CGFloat = 8
    static let composerRadius: CGFloat = 16
    static let pageRadius: CGFloat = 12
    #if os(macOS)
    static let pagePadding: CGFloat = 28
    /// Corner radius of buttons and other controls.
    static let controlRadius: CGFloat = 6
    #else
    static let pagePadding: CGFloat = 16
    static let controlRadius: CGFloat = 10
    #endif
    static let readable: CGFloat = 720
    static let wide: CGFloat = 1080
    /// Widths that pick the layout. Size decides, never the device.
    static let phoneWidth: CGFloat = 500
}

// MARK: - Layout

/// Standard scrolling page: a title, then content, centred to a readable width.
struct Page<Content: View, Actions: View>: View {
    var title: String
    var subtitle: String? = nil
    var width: CGFloat = Metrics.wide
    @ViewBuilder var actions: () -> Actions
    @ViewBuilder var content: () -> Content
    @Environment(\.hubHeader) private var hub

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 20) {
                VStack(alignment: .leading, spacing: 8) {
                    #if os(macOS)
                    HStack(alignment: .firstTextBaseline, spacing: 12) {
                        Text(title).font(.jTitle).lineLimit(1)
                        Spacer(minLength: 0)
                        HStack(spacing: 8) { actions() }
                    }
                    #else
                    // On iPhone and iPad the page draws its own header: the place, its actions, then its sections.
                    HStack(spacing: 12) {
                        Text(hub?.title ?? title).font(.jDisplay).lineLimit(1)
                        Spacer(minLength: 0)
                        HStack(spacing: 8) { actions() }
                    }
                    .frame(minHeight: 44)
                    if let hub, hub.tabs.count > 1 {
                        UnderlineTabs(options: hub.tabs, selection: hub.selection, icons: hub.icons).padding(.bottom, 6)
                    }
                    #endif
                    if let subtitle {
                        Text(subtitle).font(.jCallout).foregroundStyle(Color.inkSoft).fixedSize(horizontal: false, vertical: true)
                    }
                }
                content()
            }
            .padding(Metrics.pagePadding)
            .frame(maxWidth: width, alignment: .leading)
            .frame(maxWidth: .infinity)
        }
        .background(Color.canvas)
    }
}

/// What a page on iPhone and iPad shows above its content: the place it belongs to and that place's sections.
struct HubHeader {
    var title: String
    var tabs: [String] = []
    var icons: [String: String] = [:]
    var selection: Binding<String> = .constant("")
}

private struct HubHeaderKey: EnvironmentKey {
    static var defaultValue: HubHeader? { nil }
}

extension EnvironmentValues {
    var hubHeader: HubHeader? {
        get { self[HubHeaderKey.self] }
        set { self[HubHeaderKey.self] = newValue }
    }
}

/// Sections of a place as text tabs on a hairline; the current one is underlined in ink.
struct UnderlineTabs: View {
    var options: [String]
    @Binding var selection: String
    var icons: [String: String] = [:]
    var body: some View {
        HStack(spacing: 22) {
            ForEach(options, id: \.self) { option in
                let on = option == selection
                Button { selection = option } label: {
                    HStack(spacing: 6) {
                        if let icon = icons[option] { Image(systemName: icon).font(.system(size: 14)) }
                        Text(option).lineLimit(1).fixedSize()
                    }
                    .font(.jBody).foregroundStyle(on ? Color.ink : .inkSoft)
                    .frame(height: 42)
                    .overlay(alignment: .bottom) { Rectangle().fill(on ? Color.ink : .clear).frame(height: 2) }
                    .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
                .accessibilityLabel(option).accessibilityAddTraits(on ? .isSelected : [])
            }
            Spacer(minLength: 0)
        }
        .background(alignment: .bottom) { Rectangle().fill(Color.hairline).frame(height: 1) }
    }
}

extension Page where Actions == EmptyView {
    init(title: String, subtitle: String? = nil, width: CGFloat = Metrics.wide, @ViewBuilder content: @escaping () -> Content) {
        self.init(title: title, subtitle: subtitle, width: width, actions: { EmptyView() }, content: content)
    }
}

struct Card<Content: View>: View {
    var padding: CGFloat = 16
    @ViewBuilder var content: () -> Content
    var body: some View {
        VStack(alignment: .leading, spacing: 12) { content() }
            .padding(padding)
            .frame(maxWidth: .infinity, alignment: .leading)
            .surface()
    }
}

extension View {
    /// The one card treatment: a hairline border, no shadow.
    func surface(radius: CGFloat = Metrics.radius, edge: Color = .hairline, lifted: Bool = true) -> some View {
        background {
            RoundedRectangle(cornerRadius: radius, style: .continuous).fill(Color.card)
        }
        .overlay(RoundedRectangle(cornerRadius: radius, style: .continuous).strokeBorder(edge))
    }

    /// A pane of the window: white on the grey ground, with a hairline around it.
    func panel() -> some View {
        background(Color.canvas)
            .clipShape(RoundedRectangle(cornerRadius: Metrics.pageRadius, style: .continuous))
            .overlay(RoundedRectangle(cornerRadius: Metrics.pageRadius, style: .continuous).strokeBorder(Color.hairline))
    }
}

/// A small quiet caption: group names, counts, dates.
struct Eyebrow: View {
    var text: String
    var body: some View {
        Text(text).font(.jLabel).monospacedDigit().foregroundStyle(Color.inkFaint)
    }
}

struct SectionTitle: View {
    var title: String
    var detail: String? = nil
    var body: some View {
        HStack(alignment: .firstTextBaseline) {
            Text(title).font(.jSection)
            Spacer()
            if let detail { Eyebrow(text: detail) }
        }
    }
}

struct EmptyState: View {
    var icon: String
    var title: String
    var detail: String
    var body: some View {
        VStack(spacing: 10) {
            Image(systemName: icon).font(.system(size: 18, weight: .light)).foregroundStyle(Color.inkSoft)
                .frame(width: 44, height: 44).background(Color.bubble, in: Circle())
            Text(title).font(.jHeading)
            Text(detail).font(.jCallout).foregroundStyle(Color.inkSoft).multilineTextAlignment(.center).frame(maxWidth: 420)
        }
        .padding(.vertical, 48).padding(.horizontal, 32)
        .frame(maxWidth: .infinity)
    }
}

/// A place with nothing in it yet: what will land here, and the one thing to do about it.
struct WelcomeCard<Action: View>: View {
    var icon: String
    var title: String
    var detail: String
    var hint: String? = nil
    @ViewBuilder var action: () -> Action
    var body: some View {
        let shape = RoundedRectangle(cornerRadius: Metrics.radius, style: .continuous)
        let well = RoundedRectangle(cornerRadius: 14, style: .continuous)
        VStack(spacing: 0) {
            Image(systemName: icon).font(.system(size: 22, weight: .light)).foregroundStyle(Color.ink)
                .frame(width: 52, height: 52).background(Color.card, in: well).overlay(well.strokeBorder(Color.stroke))
            Text(title).font(.jSection).padding(.top, 16)
            Text(detail).font(.jCallout).foregroundStyle(Color.inkSoft).multilineTextAlignment(.center).frame(maxWidth: 300).padding(.top, 6)
            action().padding(.top, 20)
            if let hint { Text(hint).font(.jSmall).foregroundStyle(Color.inkFaint).multilineTextAlignment(.center).padding(.top, 10) }
        }
        .padding(.horizontal, 24).padding(.vertical, 40)
        .frame(maxWidth: .infinity)
        .background(Color.subtleFill, in: shape)
        .overlay(shape.strokeBorder(Color.stroke, style: StrokeStyle(lineWidth: 1, dash: [5, 4])))
    }
}

struct Hint: View {
    var text: String
    var icon: String = "info.circle"
    var body: some View {
        Label(text, systemImage: icon).font(.jSmall).foregroundStyle(Color.inkSoft).fixedSize(horizontal: false, vertical: true)
    }
}

/// The Jurvis mark: the butler's portrait on a white tile, the same in both appearances.
struct BrandMark: View {
    var size: CGFloat
    var body: some View {
        let shape = RoundedRectangle(cornerRadius: size * 0.2237, style: .continuous)
        Image("BrandMark").resizable().interpolation(.high).scaledToFill()
            .frame(width: size, height: size).clipShape(shape)
            .overlay(shape.strokeBorder(Color.stroke, lineWidth: 0.5))
            .accessibilityHidden(true)
    }
}

/// An icon in a soft round well, as on cards and list rows.
struct IconWell: View {
    var icon: String
    var size: CGFloat = 28
    var tint: Color = .ink
    var fill: Color = .bubble
    var body: some View {
        Image(systemName: icon).font(.system(size: size * 0.46, weight: .regular)).foregroundStyle(tint)
            .frame(width: size, height: size).background(fill, in: Circle())
            .accessibilityHidden(true)
    }
}

// MARK: - Status

enum Tone {
    case neutral, active, success, warning
    var color: Color {
        switch self {
        case .neutral: return .inkSoft
        case .active, .success: return .signal
        case .warning: return .danger
        }
    }
}

/// A status pill. Green is kept for running and done; everything else is grey, and errors colour the text only.
struct Badge: View {
    var text: String
    var tone: Tone = .neutral
    var body: some View {
        HStack(spacing: 6) {
            if tone == .active { Circle().fill(Color.signal).frame(width: 6, height: 6) }
            Text(text).font(.jSmall).lineLimit(1)
        }
        .foregroundStyle(tone.color)
        .padding(.horizontal, 8).frame(height: 22)
        .background(tone == .active || tone == .success ? Color.signalSoft : Color.bubble, in: Capsule())
        .fixedSize()
    }
}

/// Where one step stands, as a small round mark.
enum StepState { case done, running, pending, failed, waiting }

struct StatusMark: View {
    var state: StepState
    var size: CGFloat = 18
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    var body: some View {
        ZStack {
            switch state {
            case .done:
                Circle().fill(Color.signal)
                Image(systemName: "checkmark").font(.system(size: size * 0.5, weight: .medium)).foregroundStyle(Color.inkReversed)
            case .running:
                TimelineView(.animation(minimumInterval: 1 / 20, paused: reduceMotion)) { context in
                    Circle().strokeBorder(Color.signal, style: StrokeStyle(lineWidth: 1.5, dash: [3, 2.6]))
                        .rotationEffect(.degrees(reduceMotion ? 0 : context.date.timeIntervalSinceReferenceDate.truncatingRemainder(dividingBy: 6) * 60))
                }
                Circle().fill(Color.signal).frame(width: size / 3, height: size / 3)
            case .waiting:
                Circle().fill(Color.signalSoft)
                Circle().fill(Color.signal).frame(width: size / 3, height: size / 3)
            case .pending:
                Circle().strokeBorder(Color.ring, lineWidth: 1.5)
            case .failed:
                Circle().strokeBorder(Color.ring, lineWidth: 1.5)
                Image(systemName: "exclamationmark").font(.system(size: size * 0.5, weight: .medium)).foregroundStyle(Color.danger)
            }
        }
        .frame(width: size, height: size)
        .accessibilityHidden(true)
    }
}

extension Job {
    var tone: Tone {
        switch status {
        case "running": return .active
        case "succeeded": return .success
        case "failed", "interrupted": return .warning
        default: return .neutral
        }
    }
    var icon: String {
        switch kind {
        case "chat": return "text.bubble"
        case "image": return "photo"
        case "video", "export": return "film"
        case "plan": return "text.book.closed"
        case "code": return "chevron.left.forwardslash.chevron.right"
        case "code_test": return "hammer"
        case "frames": return "rectangle.split.2x1"
        case "install": return "arrow.down.circle"
        default: return "sparkle"
        }
    }
}

// MARK: - Small helpers

struct FieldEditor: View {
    var placeholder: String
    @Binding var text: String
    var minHeight: CGFloat = 90
    var body: some View {
        ZStack(alignment: .topLeading) {
            if text.isEmpty {
                Text(placeholder).foregroundStyle(Color.inkFaint).padding(.horizontal, 5).padding(.vertical, 8).allowsHitTesting(false)
            }
            TextEditor(text: $text).scrollContentBackground(.hidden).font(.jBody)
        }
        .frame(minHeight: minHeight)
        .padding(8)
        .background(Color.bubble, in: RoundedRectangle(cornerRadius: Metrics.smallRadius, style: .continuous))
        .accessibilityLabel(placeholder)
    }
}

func copyText(_ text: String) {
    #if os(macOS)
    NSPasteboard.general.clearContents()
    NSPasteboard.general.setString(text, forType: .string)
    #else
    UIPasteboard.general.string = text
    #endif
}

extension Double {
    var relativeDate: String {
        Date(timeIntervalSince1970: self).formatted(.relative(presentation: .named))
    }
}

extension Int {
    var fileSize: String { ByteCountFormatter.string(fromByteCount: Int64(self), countStyle: .file) }
}
