import SwiftUI

// MARK: - Tokens

extension Color {
    static var jurvisAccent: Color { dynamic(light: (0.13, 0.45, 0.39), dark: (0.38, 0.78, 0.67)) }
    static var warning: Color { .orange }
    /// Conversation surface: plain paper white / near-black, like a chat app.
    static var canvas: Color { dynamic(light: (1, 1, 1), dark: (0.09, 0.09, 0.1)) }
    /// Quiet fills for the composer, user bubbles and chips.
    static var bubble: Color { dynamic(light: (0.95, 0.95, 0.96), dark: (0.17, 0.17, 0.18)) }
    static var ink: Color { dynamic(light: (0.08, 0.08, 0.09), dark: (0.96, 0.96, 0.97)) }
    static var inkReversed: Color { dynamic(light: (1, 1, 1), dark: (0.08, 0.08, 0.09)) }
    static var card: Color {
        #if os(macOS)
        Color(nsColor: .controlBackgroundColor)
        #else
        Color(uiColor: .secondarySystemGroupedBackground)
        #endif
    }
    static var hairline: Color { Color.primary.opacity(0.08) }
    static var subtleFill: Color { Color.primary.opacity(0.04) }

    private static func dynamic(light: (Double, Double, Double), dark: (Double, Double, Double)) -> Color {
        #if os(macOS)
        Color(nsColor: NSColor(name: nil) { appearance in
            let c = appearance.bestMatch(from: [.darkAqua, .aqua]) == .darkAqua ? dark : light
            return NSColor(red: c.0, green: c.1, blue: c.2, alpha: 1)
        })
        #else
        Color(uiColor: UIColor { traits in
            let c = traits.userInterfaceStyle == .dark ? dark : light
            return UIColor(red: c.0, green: c.1, blue: c.2, alpha: 1)
        })
        #endif
    }
}

enum Metrics {
    static let radius: CGFloat = 14
    static let smallRadius: CGFloat = 9
    static let pagePadding: CGFloat = 24
    static let readable: CGFloat = 780
    static let wide: CGFloat = 1080
}

// MARK: - Layout

/// Standard scrolling page: header, then content, centred to a readable width.
struct Page<Content: View, Actions: View>: View {
    var title: String
    var subtitle: String? = nil
    var width: CGFloat = Metrics.wide
    @ViewBuilder var actions: () -> Actions
    @ViewBuilder var content: () -> Content

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 20) {
                VStack(alignment: .leading, spacing: 6) {
                    // On iPhone and iPad the title and actions live in the navigation bar.
                    #if os(macOS)
                    HStack(alignment: .center, spacing: 12) {
                        Text(title).font(.title2.weight(.semibold))
                        Spacer(minLength: 0)
                        HStack(spacing: 8) { actions() }
                    }
                    #endif
                    if let subtitle {
                        Text(subtitle).font(.callout).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
                    }
                }
                content()
            }
            .padding(Metrics.pagePadding)
            .frame(maxWidth: width, alignment: .leading)
            .frame(maxWidth: .infinity)
        }
        .background(Color.canvas)
        #if os(iOS)
        .toolbar { ToolbarItemGroup(placement: .topBarTrailing) { actions() } }
        #endif
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
            .background(Color.card, in: RoundedRectangle(cornerRadius: Metrics.radius))
            .overlay(RoundedRectangle(cornerRadius: Metrics.radius).strokeBorder(Color.hairline))
    }
}

struct SectionTitle: View {
    var title: String
    var detail: String? = nil
    var body: some View {
        HStack(alignment: .firstTextBaseline) {
            Text(title).font(.headline)
            Spacer()
            if let detail { Text(detail).font(.caption).foregroundStyle(.secondary) }
        }
    }
}

struct EmptyState: View {
    var icon: String
    var title: String
    var detail: String
    var body: some View {
        VStack(spacing: 10) {
            Image(systemName: icon).font(.system(size: 26, weight: .light)).foregroundStyle(Color.jurvisAccent)
            Text(title).font(.headline)
            Text(detail).font(.callout).foregroundStyle(.secondary).multilineTextAlignment(.center).frame(maxWidth: 420)
        }
        .padding(32)
        .frame(maxWidth: .infinity)
        .background(Color.subtleFill, in: RoundedRectangle(cornerRadius: Metrics.radius))
    }
}

struct Hint: View {
    var text: String
    var icon: String = "info.circle"
    var body: some View {
        Label(text, systemImage: icon).font(.caption).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
    }
}

/// The app icon artwork, used wherever the brand appears inside the app.
struct BrandMark: View {
    var size: CGFloat
    var body: some View {
        Image("BrandMark").resizable().interpolation(.high)
            .frame(width: size, height: size)
            .clipShape(RoundedRectangle(cornerRadius: size * 0.225, style: .continuous))
            .accessibilityHidden(true)
    }
}

// MARK: - Status

enum Tone {
    case neutral, active, success, warning
    var color: Color {
        switch self {
        case .neutral: return .secondary
        case .active: return .blue
        case .success: return .jurvisAccent
        case .warning: return .warning
        }
    }
}

struct Badge: View {
    var text: String
    var tone: Tone = .neutral
    var body: some View {
        Text(text)
            .font(.caption2.weight(.semibold))
            .foregroundStyle(tone.color)
            .padding(.horizontal, 7).padding(.vertical, 3)
            .background(tone.color.opacity(0.12), in: Capsule())
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
        case "chat": return "bubble.left"
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
                Text(placeholder).foregroundStyle(.tertiary).padding(.horizontal, 5).padding(.vertical, 8).allowsHitTesting(false)
            }
            TextEditor(text: $text).scrollContentBackground(.hidden).font(.body)
        }
        .frame(minHeight: minHeight)
        .padding(8)
        .background(Color.subtleFill, in: RoundedRectangle(cornerRadius: Metrics.smallRadius))
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
