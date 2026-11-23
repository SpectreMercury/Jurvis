import SwiftUI
import ImageIO
import UniformTypeIdentifiers

// 日志: what Jurvis did (a work record the engine writes itself) and what it said to itself while doing it.
// The second part belongs to a character the user makes: its lines come up as bubbles beside it, and it can be
// asked what troubles it and what the user could do better. The engine side is engine/journal.py and engine/avatar.py.

// MARK: - What the engine sends

/// One line of the journal: a tool call or a correction (stream "work"), or something it said to itself ("mind").
struct JournalRow: Codable, Identifiable, Hashable {
    var id: Int; var at: Double; var stream: String; var kind: String; var owner: String; var text: String
    var name: String; var status: String; var effect: String; var origin: String
    var session_id: String?; var message_id: String?
    /// The face that goes with a line it said to itself.
    var mood: String?
    var isMind: Bool { stream == "mind" }
    /// Someone would have to change for this not to happen again.
    var flagged: Bool { !owner.isEmpty }
}
struct MindPage: Codable { var notes: [JournalRow] }
struct JournalTask: Codable, Hashable {
    var key: String; var session_id: String?; var origin: String; var started_at: Double; var ended_at: Double
    var work: [JournalRow]; var mind: [JournalRow]; var acted: Int; var failed: Int; var corrected: Int; var title: String
    var rows: [JournalRow] { (work + mind).sorted { $0.id < $1.id } }
    var calls: Int { work.filter { $0.kind == "tool" }.count }
}
struct JournalTotals: Codable, Hashable { var tasks: Int; var calls: Int; var acted: Int; var failed: Int; var corrected: Int }
struct JournalDay: Codable, Hashable { var day: String; var tasks: [JournalTask]; var totals: JournalTotals; var days: [String] }
struct ImproveItem: Codable, Identifiable, Hashable {
    var id: Int; var owner: String; var kind: String; var stream: String; var text: String; var count: Int; var last_at: Double
}
struct AvatarProgress: Codable, Hashable { var phase: String; var value: Double }
struct AvatarState: Codable, Hashable {
    var name: String; var description: String; var status: String; var candidates: [String]
    var base: String?; var faces: [String: String]; var error: String?; var progress: AvatarProgress?
    var drawing: Bool { status == "drafting" || status == "making" }
}

enum JournalWords {
    static let kinds = ["say": "边做边说", "thought": "随口一句", "doubt": "拿不准", "friction": "被挡了一下", "slip": "我的失误", "glad": "挺顺的"]
    static let origins = ["chat": "对话", "goal": "目标", "task": "后台任务", "schedule": "定时任务", "checkin": "主动问候"]
    static let owners = [("user", "你可以帮我的", "说得再清楚一点，我就不用猜"), ("model", "我自己要改的", "我犯的错和绕的路"),
                         ("jurvis", "Jurvis 该修的", "工具、应用或给我的说明挡了路"), ("external", "外面的服务", "网站、接口这些不在我们手里的")]
    static let asks = ["你最近有什么困扰？", "我哪里说得不够清楚？", "你自己哪里没做好？"]

    static func clock(_ at: Double) -> String { Date(timeIntervalSince1970: at).formatted(.dateTime.hour(.twoDigits(amPM: .omitted)).minute(.twoDigits)) }
    static func dated(_ at: Double) -> String { Date(timeIntervalSince1970: at).formatted(.dateTime.month(.defaultDigits).day().hour(.twoDigits(amPM: .omitted)).minute(.twoDigits)) }
    static var today: String {
        let parts = Calendar.current.dateComponents([.year, .month, .day], from: Date())
        return String(format: "%04d-%02d-%02d", parts.year ?? 0, parts.month ?? 0, parts.day ?? 0)
    }
}

// MARK: - The character and what it is saying

/// The character, its pictures, and the lines waiting to come up beside it. Kept apart from AppStore so that a bubble
/// or a change of face redraws the character and nothing else.
@MainActor final class Companion: ObservableObject {
    @Published var avatar: AvatarState?
    /// The latest few lines it said to itself, for the journal page.
    @Published var recent: [JournalRow] = []
    /// The lines showing beside the character right now.
    @Published var bubbles: [JournalRow] = []
    @Published var mood = "calm"
    @Published var pictures: [String: Image] = [:]
    private var queue: [JournalRow] = []
    private var lastNote = 0
    private var started = false
    private var pumping = false
    private var round = 0
    private var working = false

    var name: String { avatar?.name ?? "小维" }
    var drawing: Bool { avatar?.drawing ?? false }

    /// Called when the engine reports that something happened: learns who the character is and picks up the new lines.
    /// The first call only notes where the journal stands, so old lines do not replay as bubbles.
    func sync(_ api: API, working: Bool) async {
        self.working = working
        if let state: AvatarState = try? await api.request("/api/avatar"), state != avatar { avatar = state }
        if !started {
            if let page: MindPage = try? await api.request("/api/journal/mind?limit=4") {
                started = true; recent = page.notes; lastNote = page.notes.last?.id ?? 0
            }
        } else if let page: MindPage = try? await api.request("/api/journal/mind?after=\(lastNote)"), let last = page.notes.last {
            lastNote = last.id
            recent = Array((recent + page.notes).suffix(4))
            queue += page.notes
            pump()
        }
        settle()
        for id in (avatar?.faces.values.map { $0 } ?? []) + (avatar?.candidates ?? []) where pictures[id] == nil {
            guard let data = try? await api.assetData(id), let source = CGImageSourceCreateWithData(data as CFData, nil),
                  let image = CGImageSourceCreateImageAtIndex(source, 0, nil) else { continue }
            pictures[id] = Image(decorative: image, scale: 1)
        }
    }

    /// The lines come up one after another, each with its face; a while after the last one the character goes quiet again.
    private func pump() {
        guard !pumping, !queue.isEmpty else { return }
        pumping = true; round += 1
        let mine = round
        Task {
            while !queue.isEmpty {
                if queue.count > 4 { queue = Array(queue.suffix(4)) }  // it has been busy: only the latest few are worth a bubble
                let note = queue.removeFirst()
                withAnimation(.spring(duration: 0.3)) { bubbles = Array((bubbles + [note]).suffix(2)); mood = note.mood ?? "calm" }
                try? await Task.sleep(for: .milliseconds(2600))
            }
            pumping = false
            try? await Task.sleep(for: .milliseconds(5200))
            guard mine == round, !pumping else { return }
            withAnimation(.easeOut(duration: 0.2)) { bubbles = [] }
            settle()
        }
    }

    /// With nothing to say it looks busy while a reply is being written, and calm otherwise.
    private func settle() {
        guard bubbles.isEmpty, !pumping else { return }
        let next = working ? "thinking" : "calm"
        if mood != next { mood = next }
    }

    func reset() { avatar = nil; recent = []; bubbles = []; queue = []; lastNote = 0; started = false; mood = "calm"; pictures = [:] }
}

extension AppStore {
    /// Opens the conversation in which the character answers for its own work, and asks it something if there is something to ask.
    func talkToCompanion(_ text: String = "") async throws {
        let session: Session = try await requireAPI().request("/api/journal/talk", method: "POST")
        fresh = false; projectPicked = false  // what is said next is said to it, not the start of something else
        await open(stretch: session.id)
        if !text.isEmpty && !chatBusy { try await send(text, assets: []) }
    }
}

/// The character everyone starts with, until they make their own: a small round fellow whose face shows the mood.
struct PlainFigure: View {
    var mood: String
    var body: some View {
        Canvas { context, size in
            var g = context
            g.scaleBy(x: size.width / 100, y: size.height / 100)
            let ink = GraphicsContext.Shading.color(.ink), soft = GraphicsContext.Shading.color(.bubble)
            let pen = StrokeStyle(lineWidth: 3, lineCap: .round, lineJoin: .round)
            func line(_ build: (inout Path) -> Void) { var path = Path(); build(&path); g.stroke(path, with: ink, style: pen) }
            func dot(_ x: CGFloat, _ y: CGFloat, _ r: CGFloat = 3.2) { g.fill(Path(ellipseIn: CGRect(x: x - r, y: y - r, width: r * 2, height: r * 2)), with: ink) }
            func at(_ x: CGFloat, _ y: CGFloat) -> CGPoint { CGPoint(x: x, y: y) }

            var ears = Path()
            ears.move(to: at(26, 34)); ears.addLine(to: at(20, 14)); ears.addLine(to: at(40, 24))
            ears.move(to: at(74, 34)); ears.addLine(to: at(80, 14)); ears.addLine(to: at(60, 24))
            g.fill(ears, with: soft); g.stroke(ears, with: ink, style: pen)
            let head = Path(ellipseIn: CGRect(x: 14, y: 22, width: 72, height: 68))
            g.fill(head, with: soft); g.stroke(head, with: ink, style: pen)

            switch mood {
            case "thinking":
                dot(39, 48); dot(65, 48)
                line { $0.move(to: at(46, 68)); $0.addLine(to: at(55, 68)) }
            case "troubled":
                line { $0.move(to: at(31, 46)); $0.addLine(to: at(42, 49)); $0.move(to: at(69, 46)); $0.addLine(to: at(58, 49)) }
                dot(37, 55, 3); dot(63, 55, 3)
                line { $0.move(to: at(43, 70)); $0.addQuadCurve(to: at(57, 70), control: at(50, 64)) }
                var drop = Path()
                drop.move(to: at(78, 34)); drop.addQuadCurve(to: at(78, 44), control: at(82, 41)); drop.addQuadCurve(to: at(78, 34), control: at(74, 41))
                g.stroke(drop, with: ink, style: StrokeStyle(lineWidth: 2, lineJoin: .round))
            case "happy":
                line { $0.move(to: at(31, 53)); $0.addQuadCurve(to: at(43, 53), control: at(37, 46)); $0.move(to: at(57, 53)); $0.addQuadCurve(to: at(69, 53), control: at(63, 46)) }
                var smile = Path()
                smile.move(to: at(41, 64)); smile.addQuadCurve(to: at(59, 64), control: at(50, 76)); smile.closeSubpath()
                g.fill(smile, with: ink); g.stroke(smile, with: ink, style: pen)
            case "guilty":
                dot(41, 53); dot(67, 53)
                line { $0.move(to: at(42, 68)); $0.addQuadCurve(to: at(50, 68), control: at(46, 64)); $0.addQuadCurve(to: at(58, 68), control: at(54, 72)) }
            case "sleepy":
                line { $0.move(to: at(31, 52)); $0.addLine(to: at(43, 52)); $0.move(to: at(57, 52)); $0.addLine(to: at(69, 52)) }
                g.stroke(Path(ellipseIn: CGRect(x: 47, y: 63, width: 6, height: 8)), with: ink, style: pen)
            default:
                dot(37, 52); dot(63, 52)
                line { $0.move(to: at(44, 66)); $0.addQuadCurve(to: at(56, 66), control: at(50, 70)) }
            }
        }
    }
}

/// A place the character stands, with the face of its current mood (or of the mood given).
struct CompanionFigure: View {
    @EnvironmentObject var companion: Companion
    var size: CGFloat
    var mood: String? = nil
    var body: some View {
        let mood = mood ?? companion.mood
        let faces = companion.avatar?.faces ?? [:]
        // A face that has not been drawn or fetched yet is stood in for by the calm one.
        let picture = faces[mood].flatMap { companion.pictures[$0] } ?? faces["calm"].flatMap { companion.pictures[$0] }
        Group {
            if let picture { picture.resizable().interpolation(.high).scaledToFit() } else { PlainFigure(mood: mood) }
        }
        .frame(width: size, height: size)
        .accessibilityHidden(true)
    }
}

/// Something it said to itself. A line that names who should change carries an outline.
struct NoteBubble: View {
    var note: JournalRow
    var lines = 4
    var body: some View {
        let shape = RoundedRectangle(cornerRadius: 12, style: .continuous)
        Text(note.text).font(.jSmall).foregroundStyle(Color.ink).lineLimit(lines).multilineTextAlignment(.leading)
            .padding(.horizontal, 10).padding(.vertical, 7)
            .background(Color.bubble, in: shape)
            .overlay(shape.strokeBorder(note.flagged ? Color.stroke : .clear))
            .accessibilityLabel((JournalWords.kinds[note.kind] ?? "") + "：" + note.text)
    }
}

/// The lines coming up beside the character, newest last.
struct CompanionBubbles: View {
    @EnvironmentObject var companion: Companion
    var alignment: HorizontalAlignment = .leading
    var body: some View {
        VStack(alignment: alignment, spacing: 4) {
            ForEach(companion.bubbles) { note in
                NoteBubble(note: note, lines: 3)
                    .transition(.scale(scale: 0.85, anchor: alignment == .leading ? .bottomLeading : .topTrailing).combined(with: .opacity))
            }
        }
    }
}

#if os(macOS)
/// The character at the foot of the sidebar, with what it just said to itself. It opens the journal.
struct CompanionRow: View {
    @EnvironmentObject var store: AppStore
    @EnvironmentObject var companion: Companion
    var body: some View {
        Button { store.section = .journal } label: {
            HStack(alignment: .bottom, spacing: 8) {
                CompanionFigure(size: 48)
                CompanionBubbles().padding(.bottom, 4)
                Spacer(minLength: 0)
            }
            .padding(.horizontal, 6).frame(maxWidth: .infinity, alignment: .leading).contentShape(Rectangle())
        }
        .buttonStyle(PressStyle())
        .help("看看\(companion.name)做了什么、在想什么")
        .accessibilityLabel("\(companion.name)的日志")
    }
}
#endif

// MARK: - The page

struct JournalView: View {
    @EnvironmentObject var store: AppStore
    @EnvironmentObject var companion: Companion
    @AppStorage("journalView") private var view = JournalView.views[0]
    /// The day shown in the work record; nil is today.
    @State private var day: String?
    @State private var page: JournalDay?
    @State private var notes: [JournalRow]?
    @State private var groups: [String: [ImproveItem]]?
    /// The engine did not answer for the journal: it is older than this app, or not reachable.
    @State private var missing = false
    /// Task cards the user unfolded: they stay open when a new line arrives.
    @State private var opened: Set<String> = []
    @State private var editing = false

    static let views = ["做了什么", "心里话", "可以更好"]

    var body: some View {
        Page(title: "日志", width: 860) {
            stage
            SegmentedControl(options: Self.views, selection: $view, fill: fillsTabs)
            if missing {
                EmptyState(icon: "book.pages", title: "这台 Mac 上的引擎还没有日志", detail: "重启 Mac 上的 Jurvis 引擎，它就会开始记录做了什么、说了什么。")
            } else {
                switch view {
                case Self.views[1]: mind
                case Self.views[2]: improve
                default: work
                }
            }
        }
        // Every change the engine reports brings the open view up to date.
        .task(id: "\(view)|\(day ?? "")|\(store.snapshot?.last_event ?? 0)") { await load() }
        .sheet(isPresented: $editing) { AvatarEditor().environmentObject(store).environmentObject(companion) }
    }

    var fillsTabs: Bool {
        #if os(iOS)
        return true
        #else
        return false
        #endif
    }

    func load() async {
        guard let api = store.api else { return }
        do {
            switch view {
            case Self.views[1]:
                let found: MindPage = try await api.request("/api/journal/mind?limit=100")
                notes = found.notes.reversed()
            case Self.views[2]:
                groups = try await api.request("/api/journal/improvements")
            default:
                page = try await api.request("/api/journal" + (day.map { "?day=" + $0 } ?? ""))
            }
            if missing { missing = false }
        } catch {
            if Task.isCancelled || !store.connected { return }
            missing = page == nil && notes == nil && groups == nil
        }
    }

    // The character, what it last said, and the ways to talk to it.
    var stage: some View {
        Card {
            HStack(alignment: .top, spacing: 16) {
                CompanionFigure(size: figureSize)
                VStack(alignment: .leading, spacing: 10) {
                    HStack(spacing: 8) {
                        Text(companion.name).font(.jSection).lineLimit(1)
                        if companion.drawing { Badge(text: companion.avatar?.progress?.phase ?? "正在画", tone: .active) }
                        else if companion.avatar?.status == "choosing" { Badge(text: "画好了，等你选", tone: .success) }
                    }
                    if companion.recent.isEmpty {
                        Text("\(companion.name)干活时自言自语的话会出现在这里。它觉得哪里不对劲，也会说。")
                            .font(.jCallout).foregroundStyle(Color.inkSoft).fixedSize(horizontal: false, vertical: true)
                    } else {
                        VStack(alignment: .leading, spacing: 4) { ForEach(companion.recent.suffix(3)) { NoteBubble(note: $0) } }
                    }
                }
                Spacer(minLength: 0)
            }
            HStack(spacing: 8) {
                ActionButton(title: "和它聊聊", busyTitle: "正在打开", icon: "text.bubble", kind: .primary, scale: .small) { try await store.talkToCompanion() }
                Button { editing = true } label: { Label(companion.avatar?.base == nil ? "捏个形象" : "换个形象", systemImage: "paintbrush") }
                    .buttonStyle(.jurvis(.secondary, .small))
            }
            ScrollView(.horizontal, showsIndicators: false) {
                HStack(spacing: 8) {
                    ForEach(JournalWords.asks, id: \.self) { ask in
                        Button { Task { await store.perform { try await store.talkToCompanion(ask) } } } label: {
                            Text(ask).font(.jCallout).foregroundStyle(Color.ink).lineLimit(1).fixedSize()
                                .padding(.horizontal, 12).frame(height: 32)
                                .overlay(Capsule().strokeBorder(Color.stroke)).contentShape(Capsule())
                        }
                        .buttonStyle(PressStyle())
                    }
                }
                .padding(.vertical, 1)
            }
        }
    }

    var figureSize: CGFloat {
        #if os(iOS)
        return 88
        #else
        return 120
        #endif
    }

    // MARK: 做了什么

    @ViewBuilder var work: some View {
        if let page {
            let today = JournalWords.today
            let days = page.days.contains(today) ? page.days : [today] + page.days
            let at = days.firstIndex(of: page.day)
            let earlier = at.flatMap { days.indices.contains($0 + 1) ? days[$0 + 1] : nil }
            let later = at.flatMap { $0 > 0 ? days[$0 - 1] : nil }
            let totals = page.totals
            ViewThatFits(in: .horizontal) {
                HStack(spacing: 12) { dayPicker(page.day, earlier, later); Spacer(minLength: 8); totalsRow(totals) }
                VStack(alignment: .leading, spacing: 8) { dayPicker(page.day, earlier, later); totalsRow(totals) }
            }
            if page.tasks.isEmpty {
                EmptyState(icon: "list.bullet.rectangle", title: "这一天还没有记录", detail: "Jurvis 每动用一次工具，这里就多一行。这是引擎自己记的，模型改不了。")
            }
            ForEach(page.tasks.reversed(), id: \.key) { task in
                JournalTaskCard(task: task, open: Binding(get: { opened.contains(task.key) }, set: { if $0 { opened.insert(task.key) } else { opened.remove(task.key) } }))
            }
        } else {
            LoadingState(text: "正在载入记录")
        }
    }

    func dayPicker(_ shown: String, _ earlier: String?, _ later: String?) -> some View {
        HStack(spacing: 4) {
            Button { day = earlier } label: { Image(systemName: "chevron.left") }
                .buttonStyle(.jurvisIcon()).disabled(earlier == nil).accessibilityLabel("前一天")
            Text(shown == JournalWords.today ? "今天" : shown).font(.jHeading).monospacedDigit().lineLimit(1).fixedSize()
            Button { day = later == JournalWords.today ? nil : later } label: { Image(systemName: "chevron.right") }
                .buttonStyle(.jurvisIcon()).disabled(later == nil).accessibilityLabel("后一天")
        }
    }

    func totalsRow(_ totals: JournalTotals) -> some View {
        HStack(spacing: 6) {
            Badge(text: "\(totals.tasks) 件事")
            Badge(text: "\(totals.acted) 次改动或对外")
            if totals.failed > 0 { Badge(text: "\(totals.failed) 次没成", tone: .warning) }
            if totals.corrected > 0 { Badge(text: "被纠正 \(totals.corrected) 次", tone: .warning) }
        }
    }

    // MARK: 心里话

    @ViewBuilder var mind: some View {
        if let notes {
            if notes.isEmpty {
                EmptyState(icon: "bubble.left", title: "它还没说过什么", detail: "\(companion.name)在动手前说的话、做事时记下的疑虑，都会留在这里。")
            } else {
                Card(padding: 0) {
                    VStack(spacing: 0) {
                        ForEach(Array(notes.enumerated()), id: \.element.id) { index, note in
                            if index > 0 { Rectangle().fill(Color.hairline).frame(height: 1).padding(.leading, 14) }
                            JournalLine(row: note, time: JournalWords.dated(note.at), tag: JournalWords.kinds[note.kind] ?? note.kind)
                                .padding(.horizontal, 14).padding(.vertical, 10)
                        }
                    }
                }
            }
        } else {
            LoadingState(text: "正在载入")
        }
    }

    // MARK: 可以更好

    @ViewBuilder var improve: some View {
        if let groups {
            let listed = JournalWords.owners.filter { !(groups[$0.0] ?? []).isEmpty }
            if listed.isEmpty {
                EmptyState(icon: "checkmark.circle", title: "最近 30 天没什么要改的", detail: "工具出错、被引擎纠正、或者它自己觉得不合理的地方，会按“该谁改”归在这里。")
            }
            ForEach(listed, id: \.0) { owner, title, hint in
                VStack(alignment: .leading, spacing: 8) {
                    HStack(alignment: .firstTextBaseline, spacing: 6) {
                        Text(title).font(.jSection)
                        Text(hint).font(.jSmall).foregroundStyle(Color.inkFaint).lineLimit(1)
                    }
                    Card(padding: 0) {
                        VStack(spacing: 0) {
                            ForEach(Array((groups[owner] ?? []).enumerated()), id: \.element.id) { index, item in
                                if index > 0 { Rectangle().fill(Color.hairline).frame(height: 1).padding(.leading, 14) }
                                HStack(alignment: .firstTextBaseline, spacing: 10) {
                                    Text(item.stream == "mind" ? "“\(item.text)”" : item.text).font(.jCallout).foregroundStyle(Color.ink)
                                        .frame(maxWidth: .infinity, alignment: .leading).fixedSize(horizontal: false, vertical: true)
                                    if item.count > 1 { Badge(text: "\(item.count) 次", tone: .warning) }
                                    Text(JournalWords.dated(item.last_at)).font(.jMicro).monospacedDigit().foregroundStyle(Color.inkFaint).fixedSize()
                                }
                                .padding(.horizontal, 14).padding(.vertical, 10)
                            }
                        }
                    }
                }
            }
            if !listed.isEmpty { Hint(text: "想知道细节，直接问\(companion.name)：它会指着这些记录告诉你。") }
        } else {
            LoadingState(text: "正在载入")
        }
    }
}

/// One thing Jurvis was asked to do: the request, how it went in a line, and unfolded, every step of it.
struct JournalTaskCard: View {
    @EnvironmentObject var store: AppStore
    var task: JournalTask
    @Binding var open: Bool

    var facts: String {
        ["\(task.calls) 步", task.acted > 0 ? "\(task.acted) 次改动或对外" : "", task.failed > 0 ? "\(task.failed) 次没成" : "",
         task.corrected > 0 ? "被纠正 \(task.corrected) 次" : ""].filter { !$0.isEmpty }.joined(separator: " · ")
    }
    var span: String {
        JournalWords.clock(task.started_at) + (task.ended_at - task.started_at > 60 ? " – " + JournalWords.clock(task.ended_at) : "")
    }

    var body: some View {
        let origin = JournalWords.origins[task.origin] ?? task.origin
        VStack(alignment: .leading, spacing: 0) {
            Button { withAnimation(.easeOut(duration: 0.15)) { open.toggle() } } label: {
                HStack(alignment: .top, spacing: 10) {
                    VStack(alignment: .leading, spacing: 4) {
                        Text(task.title.isEmpty ? origin : task.title).font(.jHeading).foregroundStyle(Color.ink).lineLimit(2).multilineTextAlignment(.leading)
                        Text(span + " · " + origin + " · " + facts).font(.jSmall).monospacedDigit()
                            .foregroundStyle(task.failed + task.corrected > 0 ? Color.inkSoft : .inkFaint).lineLimit(2).multilineTextAlignment(.leading)
                    }
                    Spacer(minLength: 8)
                    Image(systemName: "chevron.right").font(.system(size: 12)).foregroundStyle(Color.inkFaint)
                        .rotationEffect(.degrees(open ? 90 : 0)).padding(.top, 3)
                }
                .padding(14).frame(maxWidth: .infinity, alignment: .leading).contentShape(Rectangle())
            }
            .buttonStyle(PressStyle())
            .accessibilityValue(open ? "已展开" : "已收起")
            if open {
                Rectangle().fill(Color.hairline).frame(height: 1)
                VStack(alignment: .leading, spacing: 9) {
                    ForEach(task.rows) { row in JournalLine(row: row, time: JournalWords.clock(row.at), tag: Self.tag(row)) }
                    if let session = task.session_id {
                        HStack {
                            Spacer()
                            Button { Task { await store.open(stretch: session) } } label: { Label("看这段对话", systemImage: "text.bubble") }
                                .buttonStyle(.jurvis(.secondary, .small))
                        }
                    }
                }
                .padding(14)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .surface(edge: task.failed + task.corrected > 0 ? .stroke : .hairline)
    }

    static func tag(_ row: JournalRow) -> String {
        if row.isMind { return JournalWords.kinds[row.kind] ?? "" }
        if row.kind == "harness" { return row.flagged ? "被纠正" : "" }
        return row.status == "error" ? "没成" : row.status == "declined" ? "你没同意" : ""
    }
}

/// A line of the journal: when, a mark for what kind of line it is, the words, and a tag when something went wrong.
struct JournalLine: View {
    var row: JournalRow
    var time: String
    var tag: String

    var wrong: Bool { row.status == "error" || (row.kind == "harness" && row.flagged) || (row.isMind && row.flagged) }

    var body: some View {
        HStack(alignment: .firstTextBaseline, spacing: 8) {
            Text(time).font(.jMicro).monospacedDigit().foregroundStyle(Color.inkFaint).fixedSize()
            mark.frame(width: 7, height: 7).alignmentGuide(.firstTextBaseline) { $0[.bottom] + 2 }
            Text(row.isMind ? "“\(row.text)”" : row.text).font(.jCallout)
                .foregroundStyle(row.isMind ? Color.inkSoft : .ink).italic(row.isMind)
                .frame(maxWidth: .infinity, alignment: .leading).fixedSize(horizontal: false, vertical: true)
                .textSelection(.enabled)
            if !tag.isEmpty { Badge(text: tag, tone: wrong ? .warning : .neutral) }
        }
    }

    /// Hollow for something it said; green for something it changed; red for a call that failed; ink where the engine stepped in.
    @ViewBuilder var mark: some View {
        if row.isMind { Circle().strokeBorder(Color.inkSoft, lineWidth: 1.5) }
        else if row.kind == "harness" { Circle().fill(row.flagged ? Color.ink : .ring) }
        else if row.status == "error" { Circle().fill(Color.danger) }
        else if row.status == "ok" && !["read", ""].contains(row.effect) { Circle().fill(Color.signal) }
        else { Circle().fill(Color.ring) }
    }
}

// MARK: - Making a character

/// Describe it or give a photo, look at a few drawings, pick one. Its other faces are drawn afterwards, on their own.
struct AvatarEditor: View {
    @EnvironmentObject var store: AppStore
    @EnvironmentObject var companion: Companion
    @Environment(\.dismiss) var dismiss
    @State private var name = ""
    @State private var description = ""
    @State private var photo: Asset?
    @State private var importing = false
    @State private var uploading = false
    @State private var picking: String?
    @State private var confirmingReset = false

    var state: AvatarState? { companion.avatar }

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 18) {
                    field("名字") {
                        HStack(spacing: 8) {
                            TextField("小维", text: $name).textFieldStyle(.plain).font(.jBody)
                                .padding(.horizontal, 12).frame(height: 40).surface(radius: Metrics.smallRadius, edge: .stroke)
                            ActionButton(title: "保存", busyTitle: "正在保存", doneTitle: "已保存") {
                                companion.avatar = try await store.requireAPI().request("/api/avatar", method: "PUT", body: ["name": String(name.prefix(20))])
                            }
                        }
                    }
                    field("它长什么样") {
                        FieldEditor(placeholder: "比如：一只戴红围巾的橘猫 / 短发戴圆眼镜、穿黄色开衫的女生 / 一只白色的萨摩耶", text: $description, minHeight: 76)
                            .onChange(of: description) { _, now in if now.count > 300 { description = String(now.prefix(300)) } }
                    }
                    HStack(spacing: 8) {
                        ActionButton(title: "画几张看看", busyTitle: "正在排队", icon: "paintbrush", kind: .primary) { try await draft() }
                            .disabled(companion.drawing || (description.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty && photo == nil))
                        Button { importing = true } label: { ButtonFace(title: uploading ? "正在上传" : "照着一张照片画", icon: "photo", busy: uploading) }
                            .buttonStyle(.jurvis(.secondary)).disabled(uploading)
                    }
                    if let photo {
                        HStack(spacing: 6) {
                            Text("照着这张画：" + photo.name).font(.jSmall).foregroundStyle(Color.inkSoft).lineLimit(1).truncationMode(.middle)
                            Button { self.photo = nil } label: { Image(systemName: "xmark.circle") }.buttonStyle(.plain).foregroundStyle(Color.inkFaint).accessibilityLabel("不用这张照片")
                        }
                    }
                    Hint(text: "形象由这台 Mac 上的画图模型画，不会传到别处。画的时候本机模型要让出显卡，用本机模型的对话会等它画完。")
                    status
                    candidates
                    if state?.base != nil {
                        HStack { Spacer(); Button("用回默认形象") { confirmingReset = true }.buttonStyle(.jurvis(.ghost, .small)).disabled(companion.drawing) }
                    }
                }
                .padding(20).frame(maxWidth: .infinity, alignment: .leading)
            }
            .background(Color.canvas)
            .navigationTitle("捏一个形象")
            .toolbar { ToolbarItem(placement: .cancellationAction) { Button("完成") { dismiss() } } }
            .onAppear { name = companion.name; description = state?.description ?? "" }
            .fileImporter(isPresented: $importing, allowedContentTypes: [.image]) { result in
                uploading = true
                Task {
                    await store.perform { photo = try await store.uploadImage(try result.get()) }
                    uploading = false
                }
            }
            .confirmationDialog("用回默认的小圆脸？", isPresented: $confirmingReset, titleVisibility: .visible) {
                Button("用回默认", role: .destructive) {
                    Task { await store.perform { companion.avatar = try await store.requireAPI().request("/api/avatar", method: "DELETE") } }
                }
            } message: { Text("现在的形象会被删掉。") }
        }
        #if os(macOS)
        .frame(width: 520, height: 620)
        #endif
    }

    func field<Content: View>(_ title: String, @ViewBuilder content: () -> Content) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            Text(title).font(.jSmall).foregroundStyle(Color.inkSoft)
            content()
        }
    }

    func draft() async throws {
        var body: [String: Any] = ["description": description.trimmingCharacters(in: .whitespacesAndNewlines)]
        if let photo { body["photo"] = photo.id }
        companion.avatar = try await store.requireAPI().request("/api/avatar/draft", method: "POST", body: body)
    }

    @ViewBuilder var status: some View {
        if let state {
            VStack(alignment: .leading, spacing: 6) {
                switch state.status {
                case "drafting":
                    progress(state.progress?.phase ?? "排队中，马上开始画", "每张大约一分半钟", state.progress?.value)
                case "choosing":
                    Text("画好了，点一张你喜欢的。").font(.jCallout)
                case "making":
                    progress("已经换上了。" + (state.progress?.phase ?? "正在画表情"), "五个表情大约十几分钟，可以先去做别的", state.progress?.value)
                case "ready" where state.base != nil:
                    HStack(spacing: 6) {
                        ForEach(["calm", "thinking", "troubled", "happy", "guilty", "sleepy"].filter { state.faces[$0] != nil }, id: \.self) { CompanionFigure(size: 44, mood: $0) }
                    }
                    Text("现在的形象有 \(state.faces.count) 个表情。想换就再描述一次。").font(.jSmall).foregroundStyle(Color.inkSoft)
                default:
                    EmptyView()
                }
                if let error = state.error, !error.isEmpty { Text(error).font(.jSmall).foregroundStyle(Color.danger).fixedSize(horizontal: false, vertical: true) }
            }
        }
    }

    func progress(_ text: String, _ hint: String, _ value: Double?) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack(spacing: 8) { BusyRing(); Text(text).font(.jCallout) }
            if let value { ProgressBar(value: value) }
            Text(hint).font(.jSmall).foregroundStyle(Color.inkFaint)
        }
    }

    @ViewBuilder var candidates: some View {
        let ids = state?.candidates ?? []
        if !ids.isEmpty {
            LazyVGrid(columns: Array(repeating: GridItem(.flexible(), spacing: 10), count: 3), spacing: 10) {
                ForEach(ids, id: \.self) { id in
                    let shape = RoundedRectangle(cornerRadius: Metrics.radius, style: .continuous)
                    Button { choose(id) } label: {
                        ZStack {
                            if let picture = companion.pictures[id] { picture.resizable().interpolation(.high).scaledToFit().padding(6) } else { BusyRing() }
                            if picking == id { shape.fill(Color.canvas.opacity(0.6)); BusyRing(size: 18) }
                        }
                        .frame(maxWidth: .infinity).aspectRatio(1, contentMode: .fit)
                        .background(Color.subtleFill, in: shape).overlay(shape.strokeBorder(Color.stroke))
                    }
                    .buttonStyle(PressStyle()).disabled(state?.status != "choosing" || picking != nil)
                    .accessibilityLabel("选这个形象")
                }
            }
        }
    }

    func choose(_ id: String) {
        picking = id
        Task {
            await store.perform { companion.avatar = try await store.requireAPI().request("/api/avatar/choose", method: "POST", body: ["asset_id": id]) }
            picking = nil
        }
    }
}
