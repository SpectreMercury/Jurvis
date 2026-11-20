import SwiftUI

#if !UI_SMOKE
@main
#endif
struct JurvisApp: App {
    @StateObject private var store = AppStore()
    @Environment(\.scenePhase) private var phase
    var body: some Scene {
        WindowGroup {
            RootView().environmentObject(store).tint(Color.ink)
                .task { await store.start() }
                .onOpenURL { url in Task { await store.pair(url.absoluteString) } }
                #if os(iOS) && canImport(HealthKit)
                // Apple Health is read on the phone and sent to the Mac whenever the app comes to the front (once the user turned it on).
                .onChange(of: phase) { _, now in if now == .active { Task { await HealthKitSync.shared.syncIfDue(store: store) } } }
                #endif
                #if os(macOS)
                .frame(minWidth: 820, minHeight: 600)
                #endif
        }
        #if os(macOS)
        .defaultSize(width: 1180, height: 800)
        // A thin title bar with no title: the panels name themselves.
        .windowToolbarStyle(.unifiedCompact(showsTitle: false))
        .commands {
            CommandGroup(replacing: .newItem) {
                Button("新任务") { Task { await store.newChat() } }.keyboardShortcut("n")
            }
            CommandGroup(after: .sidebar) {
                ForEach(Array(Section.allCases.enumerated()), id: \.element) { index, section in
                    Button(section == .assistant ? "任务" : section.title) { store.section = section }.keyboardShortcut(KeyEquivalent(Swift.Character("\(index + 1)")), modifiers: .command)
                }
            }
        }
        #endif
    }
}

struct RootView: View {
    @EnvironmentObject var store: AppStore
    @AppStorage("sidebarOpen") private var sidebarOpen = true

    var body: some View {
        Group {
            #if os(iOS)
            if store.api == nil { PairingView() } else { PhoneRoot() }
            #else
            desktop
            #endif
        }
        .foregroundStyle(Color.ink)
        .overlay { if let viewing = store.viewing { MediaViewer(viewing: viewing).transition(.opacity) } }
        .animation(.easeOut(duration: 0.15), value: store.viewing != nil)
        .alert("没有完成", isPresented: Binding(get: { store.error != nil }, set: { if !$0 { store.error = nil } })) {
            Button("知道了", role: .cancel) { store.error = nil }
        } message: { Text(store.error ?? "") }
    }

    #if os(macOS)
    /// Two panes on a grey ground: navigation and the task.
    var desktop: some View {
        let section = store.section ?? .assistant
        return HStack(spacing: 8) {
            if sidebarOpen { Sidebar().frame(width: 244).padding(.trailing, -8) }
            NavigationStack {
                Group {
                    if section == .assistant { ChatView() } else { HubView(hub: section.hub) }
                }
                .background(Color.canvas)
                .navigationTitle(navigationTitle)
                .toolbar(removing: .title)
                .toolbar {
                    ToolbarItem(placement: .navigation) {
                        Button { withAnimation(.easeOut(duration: 0.18)) { sidebarOpen.toggle() } } label: { Image(systemName: "sidebar.left") }
                            .help(sidebarOpen ? "收起侧栏" : "显示侧栏").accessibilityLabel(sidebarOpen ? "收起侧栏" : "显示侧栏")
                    }
                }
                .toolbarBackground(Color.chrome, for: .windowToolbar)
                .toolbarBackgroundVisibility(.visible, for: .windowToolbar)
            }
            .panel()
        }
        .padding(.leading, sidebarOpen ? 0 : 8).padding(.trailing, 8).padding(.bottom, 8)
        .background(Color.chrome)
    }

    var navigationTitle: String {
        if store.section == .assistant || store.section == nil {
            return store.currentSession?.title ?? "Jurvis"
        }
        return store.section?.rawValue ?? "Jurvis"
    }
    #endif
}

/// One of the four places. Two of them hold several sections, switched with tabs on a grey track.
struct HubView: View {
    @EnvironmentObject var store: AppStore
    var hub: Hub

    var section: Section {
        let current = store.section ?? hub.first
        return current.hub == hub ? current : hub.first
    }

    static func tabTitle(_ section: Section) -> String { section == .settings ? "通用" : section.title }

    var body: some View {
        VStack(spacing: 0) {
            #if os(macOS)
            if hub.sections.count > 1 {
                SegmentedControl(options: hub.sections.map(Self.tabTitle),
                                 selection: Binding(get: { Self.tabTitle(section) }, set: { title in store.section = hub.sections.first { Self.tabTitle($0) == title } }),
                                 icons: Dictionary(uniqueKeysWithValues: hub.sections.map { (Self.tabTitle($0), $0.icon) }), fill: fillsTabs)
                    .padding(.horizontal, 16).padding(.top, 12).padding(.bottom, 4)
                    .frame(maxWidth: .infinity)
                    .background(Color.canvas)
            }
            #endif
            switch section {
            case .drama: DramaView()
            case .library: LibraryView()
            case .tasks: TasksView()
            case .home: HomeView()
            case .memory: MemoryView()
            case .goals: GoalsView()
            case .ledger: LedgerView()
            case .health: HealthView()
            case .files: FilesView()
            case .settings: SettingsView()
            case .assistant: ChatView()
            }
        }
        .background(Color.canvas)
        .environment(\.hubHeader, HubHeader(
            title: hub.rawValue, tabs: hub.sections.map(Self.tabTitle),
            icons: Dictionary(uniqueKeysWithValues: hub.sections.map { (Self.tabTitle($0), $0.icon) }),
            selection: Binding(get: { Self.tabTitle(section) }, set: { title in store.section = hub.sections.first { Self.tabTitle($0) == title } })))
    }

    var fillsTabs: Bool {
        #if os(iOS)
        return true
        #else
        return false
        #endif
    }
}

/// Recent tasks under their dates.
private func grouped(_ sessions: [Session]) -> [(String, [Session])] {
    let calendar = Calendar.current
    var buckets: [(String, [Session])] = [("今天", []), ("昨天", []), ("近 7 天", []), ("更早", [])]
    for session in sessions.prefix(200) {
        let date = Date(timeIntervalSince1970: session.updated_at)
        let index = calendar.isDateInToday(date) ? 0 : calendar.isDateInYesterday(date) ? 1
            : date > Date().addingTimeInterval(-7 * 86400) ? 2 : 3
        buckets[index].1.append(session)
    }
    return buckets.filter { !$0.1.isEmpty }
}

/// Asks before a task is deleted, wherever the list lives.
private struct DeleteTaskDialog: ViewModifier {
    @EnvironmentObject var store: AppStore
    @Binding var pending: Session?
    @Binding var deleting: Set<String>
    func body(content: Content) -> some View {
        content.confirmationDialog("删除这个任务？", isPresented: Binding(get: { pending != nil }, set: { if !$0 { pending = nil } }), titleVisibility: .visible) {
            Button("删除", role: .destructive) {
                if let session = pending {
                    deleting.insert(session.id)
                    Task { await store.deleteSession(session.id); deleting.remove(session.id) }
                }
            }
        } message: { Text("对话记录会从 Mac 上删除，生成的素材和文件会保留。") }
    }
}

#if os(macOS)
/// Navigation: the brand, the places, then every task with where it stands, and the model at the bottom.
struct Sidebar: View {
    @EnvironmentObject var store: AppStore
    @State private var query = ""
    @State private var pendingDelete: Session?
    @State private var deleting: Set<String> = []
    @FocusState private var searching: Bool

    var sessions: [Session] {
        let all = store.snapshot?.sessions ?? []
        return query.isEmpty ? all : all.filter { $0.title.localizedCaseInsensitiveContains(query) }
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            HStack(spacing: 8) {
                BrandMark(size: 22)
                Text("Jurvis").font(.jSection)
                Spacer()
            }
            .padding(.horizontal, 18).padding(.top, 2).padding(.bottom, 14)
            VStack(spacing: 2) {
                newTaskRow
                searchRow
                ForEach([Hub.library, .agenda, .home, .settings]) { hubRow($0) }
            }
            .padding(.horizontal, 10)
            Eyebrow(text: "任务").padding(.horizontal, 20).padding(.top, 18).padding(.bottom, 6)
            ScrollView {
                LazyVStack(alignment: .leading, spacing: 2) {
                    ForEach(sessions.prefix(200)) { session in sessionRow(session) }
                    if store.snapshot == nil {
                        LoadingState(text: "正在载入任务")
                    } else if sessions.isEmpty {
                        Text(query.isEmpty ? "还没有任务" : "没有匹配的任务").font(.jCallout).foregroundStyle(Color.inkSoft).padding(10)
                    }
                }
                .padding(.horizontal, 10).padding(.bottom, 10)
            }
            ConnectionIndicator().padding(.horizontal, 10).padding(.bottom, 4)
        }
        .background(Color.chrome)
        .modifier(DeleteTaskDialog(pending: $pendingDelete, deleting: $deleting))
    }

    func sessionRow(_ session: Session) -> some View {
        let selected = store.section == .assistant && store.selectedSession == session.id
        let job = store.chatJob(of: session)
        let waiting = job?.phase.hasPrefix("等待你确认") ?? false
        return Button { Task { await store.selectSession(session.id) } } label: {
            HStack(spacing: 10) {
                Group {
                    if waiting { StatusMark(state: .waiting, size: 16) }
                    else if job?.status == "running" { StatusMark(state: .running, size: 16) }
                    else if job != nil { StatusMark(state: .pending, size: 16) }
                    else {
                        Image(systemName: session.code_project_id != nil ? "chevron.left.forwardslash.chevron.right" : "checkmark.circle")
                            .font(.system(size: 14, weight: .light)).foregroundStyle(Color.inkSoft)
                    }
                }
                .frame(width: 18)
                Text(session.title).font(.jCallout).foregroundStyle(Color.ink).lineLimit(1).truncationMode(.tail)
                    .frame(maxWidth: .infinity, alignment: .leading).layoutPriority(1)
                if deleting.contains(session.id) { BusyRing() }
                else if waiting { Text("等你").font(.jSmall).foregroundStyle(Color.signal).fixedSize() }
                else if job?.status == "queued" { Text("排队").font(.jSmall).foregroundStyle(Color.inkFaint).fixedSize() }
            }
            .sidebarRow(selected: selected)
        }
        .buttonStyle(SidebarRowStyle())
        .contextMenu { Button("删除任务", role: .destructive) { pendingDelete = session } }
        .accessibilityAddTraits(selected ? .isSelected : [])
    }

    var newTaskRow: some View {
        let selected = (store.section == .assistant || store.section == nil) && store.selectedSession == nil
        return Button { Task { await store.newChat() } } label: {
            HStack(spacing: 10) {
                Image(systemName: "square.and.pencil").font(.system(size: 14, weight: .light)).frame(width: 18)
                Text("新任务").font(.jCallout).lineLimit(1)
                Spacer()
                Text("⌘N").font(.jSmall).foregroundStyle(Color.inkFaint)
            }
            .foregroundStyle(Color.ink)
            .sidebarRow(selected: selected)
        }
        .buttonStyle(SidebarRowStyle()).help("新任务（⌘N）")
        .accessibilityLabel("新任务").accessibilityAddTraits(selected ? .isSelected : [])
    }

    /// Search is a row like the others; typing in it filters the tasks below.
    var searchRow: some View {
        HStack(spacing: 10) {
            Image(systemName: "magnifyingglass").font(.system(size: 14, weight: .light)).frame(width: 18)
            TextField("搜索", text: $query).textFieldStyle(.plain).font(.jCallout).focused($searching)
            if !query.isEmpty {
                Button { query = "" } label: { Image(systemName: "xmark.circle").font(.system(size: 12)).foregroundStyle(Color.inkFaint) }
                    .buttonStyle(.plain).accessibilityLabel("清除搜索")
            }
        }
        .foregroundStyle(Color.ink)
        .sidebarRow(selected: !query.isEmpty)
        .onTapGesture { searching = true }
    }

    func hubRow(_ hub: Hub) -> some View {
        let selected = (store.section ?? .assistant).hub == hub
        let busy = hub == .settings ? store.activeJobs.filter { $0.kind != "chat" }.count : 0
        return Button { if !selected { store.section = hub.first } } label: {
            HStack(spacing: 10) {
                Image(systemName: hub.icon).font(.system(size: 14, weight: .light)).symbolVariant(selected ? .fill : .none).frame(width: 18)
                Text(hub.rawValue).font(.jCallout).lineLimit(1)
                Spacer()
                if busy > 0 { Text("\(busy) 个进行中").font(.jSmall).foregroundStyle(Color.signal).fixedSize() }
            }
            .foregroundStyle(Color.ink)
            .sidebarRow(selected: selected)
        }
        .buttonStyle(SidebarRowStyle())
        .accessibilityLabel(busy > 0 ? "\(hub.rawValue)，\(busy) 个进行中" : hub.rawValue)
        .accessibilityAddTraits(selected ? .isSelected : [])
    }
}

private extension View {
    func sidebarRow(selected: Bool) -> some View {
        padding(.horizontal, 10).frame(height: SidebarRowStyle.height)
            .background(selected ? Color.selection : .clear, in: RoundedRectangle(cornerRadius: 8, style: .continuous))
            .contentShape(Rectangle())
    }
}

/// Sidebar rows tint on hover and press, under the selected state.
private struct SidebarRowStyle: ButtonStyle {
    static let height: CGFloat = 34
    func makeBody(configuration: Configuration) -> some View { Row(configuration: configuration) }
    private struct Row: View {
        let configuration: Configuration
        @State private var hovering = false
        var body: some View {
            configuration.label
                .background(Color.selection.opacity(configuration.isPressed ? 1 : hovering ? 0.6 : 0), in: RoundedRectangle(cornerRadius: 8, style: .continuous))
                .onHover { hovering = $0 }
        }
    }
}

/// The model card at the foot of the sidebar: whether the engine is reachable and what it is busy with. It opens the queue.
struct ConnectionIndicator: View {
    @EnvironmentObject var store: AppStore
    var body: some View {
        let running = store.activeJobs.first { $0.status == "running" }
        Button { store.section = .tasks } label: {
            HStack(spacing: 10) {
                Group {
                    if store.connecting { BusyRing(size: 12) }
                    else if store.connected { Circle().fill(Color.signal).frame(width: 8, height: 8).background(Circle().fill(Color.signalSoft).frame(width: 14, height: 14)) }
                    else { Circle().strokeBorder(Color.ring, lineWidth: 1.5).frame(width: 9, height: 9) }
                }
                .frame(width: 14)
                Image(systemName: "cpu").font(.system(size: 14, weight: .light)).foregroundStyle(Color.inkSoft)
                VStack(alignment: .leading, spacing: 1) {
                    Text(title).font(.jCallout).foregroundStyle(Color.ink).lineLimit(1)
                    Text(detail(running)).font(.jSmall).foregroundStyle(store.connected || store.connecting ? Color.inkFaint : Color.danger).lineLimit(1).truncationMode(.tail)
                }
                Spacer(minLength: 0)
            }
            .padding(.horizontal, 10).frame(height: 48)
            .background(Color.card, in: RoundedRectangle(cornerRadius: 10, style: .continuous))
            .overlay(RoundedRectangle(cornerRadius: 10, style: .continuous).strokeBorder(Color.hairline))
            .contentShape(Rectangle())
        }
        .buttonStyle(PressStyle())
        .help(store.connectionStatus + " · 点开查看后台任务")
        .accessibilityElement(children: .combine)
        .accessibilityLabel("模型状态：" + detail(running))
    }
    var title: String {
        switch store.snapshot?.runtime.active {
        case "llm": return "本机语言模型"
        case "comfy": return "本机创作引擎"
        default: return "本机引擎"
        }
    }
    func detail(_ running: Job?) -> String {
        guard store.connected else { return store.connecting ? "正在连接" : "未连接" }
        if let running { return running.title + " · " + running.phase }
        return "待命 · 本机运行"
    }
}
#endif

#if os(iOS)
/// iPhone and iPad: four places behind a tab bar; a task opens over the list.
struct PhoneRoot: View {
    @EnvironmentObject var store: AppStore
    @State private var path: [String] = []

    var hub: Hub { (store.section ?? .assistant).hub }

    /// A screen this wide (iPad, a phone on its side) can spare a strip on the right for the tabs, as icons, and leave the bottom to the content.
    static let railWidth: CGFloat = 560

    var body: some View {
        GeometryReader { size in
            // iPhone Duo keeps the clock and status icons in a strip down the right edge, on both of its screens, which the system
            // reports as a trailing safe area far wider than the leading one. The tabs go into that strip, under the status icons,
            // instead of taking a row or a column of their own. The insets change as the phone folds, so this is read on every layout.
            let insets = size.safeAreaInsets
            let strip = insets.trailing - insets.leading >= 40 ? insets.trailing : 0
            let rail = strip == 0 && size.size.width >= Self.railWidth
            HStack(spacing: 0) {
                pages(rail: rail || strip > 0)
                if rail { TabRail() }
            }
            .overlay {
                if strip > 0 {
                    HStack(spacing: 0) { Spacer(minLength: 0); TabRail(width: strip) }.ignoresSafeArea(.container, edges: .trailing)
                }
            }
        }
        .background(Color.canvas)
        .task { await openForUICheck() }
    }

    @ViewBuilder func pages(rail: Bool) -> some View {
        Group {
            switch hub {
            case .tasks:
                NavigationStack(path: $path) {
                    PhoneTasksView(open: { path = ["run"] })
                        .safeAreaInset(edge: .bottom, spacing: 0) { if !rail { TabBar() } }
                        .navigationDestination(for: String.self) { _ in
                            ChatView().toolbar { ToolbarItem(placement: .topBarTrailing) { runMenu } }
                        }
                }
            default:
                NavigationStack {
                    HubView(hub: hub)
                        .safeAreaInset(edge: .bottom, spacing: 0) { if !rail { TabBar() } }
                        .toolbar(.hidden, for: .navigationBar)
                }
                .id(hub)
            }
        }
    }

    /// For UI checks in the simulator, where nothing can tap: `SIMCTL_CHILD_JURVIS_UI_START=task|new|<section>`
    /// opens that screen at launch. Unset in normal use.
    func openForUICheck() async {
        guard let start = AppStore.checkStart else { return }
        for _ in 0..<40 where store.snapshot == nil { try? await Task.sleep(for: .milliseconds(250)) }
        if let section = Section.allCases.first(where: { $0.rawValue == start }), section != .assistant { store.section = section; return }
        guard ["new", "task"].contains(start) else { return }
        if start == "new" { await store.newChat() }
        else if let session = store.snapshot?.sessions.first { await store.selectSession(session.id) }
        path = ["run"]
    }

    var runMenu: some View {
        Menu {
            Button { Task { await store.newChat() } } label: { Label("新任务", systemImage: "square.and.pencil") }
            if let id = store.selectedSession {
                Button(role: .destructive) { path = []; Task { await store.deleteSession(id) } } label: { Label("删除任务", systemImage: "trash") }
            }
        } label: { Image(systemName: "ellipsis").frame(width: 44, height: 44).contentShape(Rectangle()) }
        .accessibilityLabel("更多")
    }
}

/// 任务 · 作品库 · 家居 · 设置, each with its icon, above the home indicator.
struct TabBar: View {
    @EnvironmentObject var store: AppStore
    var body: some View {
        let current = (store.section ?? .assistant).hub
        HStack(spacing: 0) {
            ForEach(Hub.allCases) { hub in
                let selected = hub == current
                Button { if !selected { store.section = hub.first } } label: {
                    VStack(spacing: 3) {
                        Image(systemName: hub.icon).font(.system(size: 20, weight: selected ? .regular : .light)).frame(height: 24)
                        Text(hub.rawValue).font(.system(size: 11, weight: selected ? .medium : .regular)).lineLimit(1).fixedSize()
                    }
                    .foregroundStyle(selected ? Color.ink : .inkFaint)
                    .frame(maxWidth: .infinity).frame(height: 52)
                    .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
                .accessibilityLabel(hub.rawValue).accessibilityAddTraits(selected ? .isSelected : [])
            }
        }
        .padding(.horizontal, 8)
        .background(Color.canvas.ignoresSafeArea(edges: .bottom))
        .overlay(alignment: .top) { Rectangle().fill(Color.hairline).frame(height: 1) }
    }
}

/// The same four tabs down the right edge, icons only, for a screen wide enough to give them a strip of their own.
struct TabRail: View {
    @EnvironmentObject var store: AppStore
    var width: CGFloat = 64
    var body: some View {
        let current = (store.section ?? .assistant).hub
        VStack(spacing: 8) {
            ForEach(Hub.allCases) { hub in
                let selected = hub == current
                Button { if !selected { store.section = hub.first } } label: {
                    Image(systemName: hub.icon).font(.system(size: 20, weight: selected ? .regular : .light))
                        .foregroundStyle(selected ? Color.ink : .inkFaint)
                        .frame(width: 44, height: 44)
                        .background(selected ? Color.bubble : .clear, in: RoundedRectangle(cornerRadius: Metrics.controlRadius, style: .continuous))
                        .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
                .accessibilityLabel(hub.rawValue).accessibilityAddTraits(selected ? .isSelected : [])
            }
        }
        .frame(maxHeight: .infinity)
        .frame(width: width)
        .background(Color.canvas.ignoresSafeArea())
        .overlay(alignment: .leading) { Rectangle().fill(Color.hairline).frame(width: 1).ignoresSafeArea() }
    }
}

/// Every task: the ones waiting for a yes on top, then what is running, then the rest by date.
struct PhoneTasksView: View {
    @EnvironmentObject var store: AppStore
    var open: () -> Void
    @State private var query = ""
    @State private var searching = false
    @FocusState private var searchFocused: Bool
    @State private var pendingDelete: Session?
    @State private var deleting: Set<String> = []

    var sessions: [Session] {
        let all = store.snapshot?.sessions ?? []
        return query.isEmpty ? all : all.filter { $0.title.localizedCaseInsensitiveContains(query) }
    }

    var body: some View {
        let waiting = store.waitingSessions.filter { item in sessions.contains { $0.id == item.session.id } }
        let active = sessions.filter { session in store.chatJob(of: session) != nil && !waiting.contains { $0.session.id == session.id } }
        let rest = sessions.filter { store.chatJob(of: $0) == nil }
        ScrollView {
            LazyVStack(alignment: .leading, spacing: 20) {
                header
                if searching { searchField }
                ForEach(waiting, id: \.session.id) { item in waitingCard(item.session, item.job) }
                if !active.isEmpty { group("进行中", active) }
                ForEach(grouped(rest), id: \.0) { title, items in group(title, items) }
                if store.snapshot == nil {
                    LoadingState(text: "正在载入任务")
                } else if sessions.isEmpty {
                    EmptyState(icon: "checklist", title: query.isEmpty ? "还没有任务" : "没有匹配的任务", detail: "把想要的结果交给 Jurvis，它会列出计划、动手去做，需要你点头的时候再来问。")
                }
            }
            .padding(.horizontal, 16).padding(.top, 8).padding(.bottom, 12)
            .frame(maxWidth: 640).frame(maxWidth: .infinity)
        }
        .background(Color.canvas)
        .safeAreaInset(edge: .bottom, spacing: 0) { newTask }
        .toolbar(.hidden, for: .navigationBar)
        .modifier(DeleteTaskDialog(pending: $pendingDelete, deleting: $deleting))
    }

    /// The mark and name, whether the Mac is reachable, and search.
    var header: some View {
        HStack(spacing: 10) {
            BrandMark(size: 30)
            Text("Jurvis").font(.jDisplay).lineLimit(1).fixedSize()
            Spacer(minLength: 8)
            HStack(spacing: 6) {
                if store.connected { Circle().fill(Color.signal).frame(width: 6, height: 6) }
                else { Circle().strokeBorder(Color.ring, lineWidth: 1.5).frame(width: 7, height: 7) }
                Text(store.connected ? "Mac 已连接" : store.connecting ? "正在连接" : "未连接").font(.jSmall).foregroundStyle(Color.inkSoft).lineLimit(1).fixedSize()
            }
            .padding(.horizontal, 10).frame(height: 28).background(Color.bubble, in: Capsule())
            .accessibilityElement(children: .combine).accessibilityLabel("连接状态：" + store.connectionStatus)
            Button { withAnimation(.easeOut(duration: 0.15)) { searching.toggle() }; if !searching { query = "" } } label: { Image(systemName: "magnifyingglass") }
                .buttonStyle(.jurvisIcon(.ghost, .regular)).accessibilityLabel(searching ? "关闭搜索" : "搜索")
        }
        .frame(minHeight: 44)
    }

    var searchField: some View {
        HStack(spacing: 8) {
            Image(systemName: "magnifyingglass").font(.system(size: 15, weight: .light)).foregroundStyle(Color.inkSoft)
            TextField("搜索任务", text: $query).textFieldStyle(.plain).font(.jBody).focused($searchFocused).submitLabel(.search)
            if !query.isEmpty {
                Button { query = "" } label: { Image(systemName: "xmark.circle").foregroundStyle(Color.inkFaint).frame(width: 44, height: 44).contentShape(Rectangle()) }
                    .buttonStyle(.plain).accessibilityLabel("清除搜索")
            }
        }
        .padding(.leading, 14).frame(height: 44)
        .background(Color.bubble, in: RoundedRectangle(cornerRadius: 12, style: .continuous))
        .onAppear { searchFocused = true }
    }

    func show(_ session: Session) {
        Task { await store.selectSession(session.id) }
        open()
    }

    func waitingCard(_ session: Session, _ job: Job) -> some View {
        Button { show(session) } label: {
            VStack(alignment: .leading, spacing: 10) {
                RunStatusLine(text: "等你确认")
                Text(session.title).font(.jSection).foregroundStyle(Color.ink).lineLimit(2).multilineTextAlignment(.leading)
                Text(job.phase).font(.jSmall).foregroundStyle(Color.inkSoft).lineLimit(2)
                HStack(spacing: 6) { Text("去确认"); Image(systemName: "arrow.right").font(.system(size: 12)) }
                    .font(.jBody).foregroundStyle(Color.inkReversed)
                    .frame(maxWidth: .infinity).frame(height: 44).background(Color.ink, in: Capsule())
            }
            .padding(14).frame(maxWidth: .infinity, alignment: .leading)
            .surface(radius: 18, edge: .stroke)
        }
        .buttonStyle(PressStyle())
    }

    func group(_ title: String, _ items: [Session]) -> some View {
        VStack(alignment: .leading, spacing: 0) {
            Eyebrow(text: title).padding(.bottom, 2)
            ForEach(items) { session in row(session) }
        }
    }

    func row(_ session: Session) -> some View {
        let job = store.chatJob(of: session)
        return Button { show(session) } label: {
            HStack(spacing: 12) {
                if job?.status == "running" { IconWell(icon: "play", size: 32, tint: .signal, fill: .signalSoft) }
                else { IconWell(icon: job != nil ? "clock" : session.code_project_id != nil ? "chevron.left.forwardslash.chevron.right" : "checkmark", size: 32, tint: .inkSoft) }
                VStack(alignment: .leading, spacing: 2) {
                    Text(session.title).font(.jBody).foregroundStyle(Color.ink).lineLimit(1)
                    Text(job.map { $0.phase.isEmpty ? $0.statusLabel : $0.phase } ?? session.updated_at.relativeDate)
                        .font(.jMicro).foregroundStyle(Color.inkFaint).lineLimit(1)
                }
                Spacer(minLength: 8)
                if deleting.contains(session.id) { BusyRing() }
                else if job?.status == "queued" { Text("排队").font(.jMicro).foregroundStyle(Color.inkFaint).fixedSize() }
            }
            .frame(minHeight: 60).contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .contextMenu { Button("删除任务", role: .destructive) { pendingDelete = session } }
    }

    /// The way in to a new task, always under the thumb.
    var newTask: some View {
        Button { Task { await store.newChat() }; open() } label: {
            HStack(spacing: 10) {
                Image(systemName: "square.and.pencil").font(.system(size: 16, weight: .light)).foregroundStyle(Color.inkSoft)
                Text("交给 Jurvis 一个新任务").font(.jBody).foregroundStyle(Color.inkFaint).lineLimit(1)
                Spacer(minLength: 8)
                Image(systemName: "arrow.up").font(.system(size: 14, weight: .medium)).foregroundStyle(Color.inkReversed)
                    .frame(width: 36, height: 36).background(Color.ink, in: Circle())
            }
            .padding(.leading, 16).padding(.trailing, 8).frame(height: 52)
            .surface(radius: 26, edge: .stroke)
        }
        .buttonStyle(PressStyle())
        .accessibilityLabel("新任务")
        .padding(.horizontal, 12).padding(.vertical, 10)
        .frame(maxWidth: 640).frame(maxWidth: .infinity)
        .background(Color.canvas)
    }
}
#endif
