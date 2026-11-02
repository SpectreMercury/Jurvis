import SwiftUI

#if !UI_SMOKE
@main
#endif
struct JurvisApp: App {
    @StateObject private var store = AppStore()
    var body: some Scene {
        WindowGroup {
            RootView().environmentObject(store).tint(.jurvisAccent)
                .task { await store.start() }
                .onOpenURL { url in Task { await store.pair(url.absoluteString) } }
                #if os(macOS)
                .frame(minWidth: 820, minHeight: 600)
                #endif
        }
        #if os(macOS)
        .defaultSize(width: 1180, height: 800)
        .commands {
            CommandGroup(replacing: .newItem) {
                Button("新对话") { Task { await store.newChat() } }.keyboardShortcut("n")
            }
            CommandGroup(after: .sidebar) {
                ForEach(Array(Section.allCases.enumerated()), id: \.element) { index, section in
                    Button(section.rawValue) { store.section = section }.keyboardShortcut(KeyEquivalent(Swift.Character("\(index + 1)")), modifiers: .command)
                }
            }
        }
        #endif
    }
}

struct RootView: View {
    @EnvironmentObject var store: AppStore
    @State private var compactColumn = NavigationSplitViewColumn.detail

    var body: some View {
        Group {
            #if os(iOS)
            if store.api == nil { PairingView() } else { workspace }
            #else
            workspace
            #endif
        }
        .alert("需要处理", isPresented: Binding(get: { store.error != nil }, set: { if !$0 { store.error = nil } })) {
            Button("知道了", role: .cancel) { store.error = nil }
        } message: { Text(store.error ?? "") }
    }

    var workspace: some View {
        NavigationSplitView(preferredCompactColumn: $compactColumn) {
            Sidebar(show: { compactColumn = .detail })
                .navigationSplitViewColumnWidth(min: 220, ideal: 260, max: 320)
        } detail: {
            NavigationStack {
                Group {
                    switch store.section ?? .assistant {
                    case .assistant: ChatView()
                    case .drama: DramaView()
                    case .library: LibraryView()
                    case .tasks: TasksView()
                    case .memory: MemoryView()
                    case .files: FilesView()
                    case .settings: SettingsView()
                    }
                }
                .background(Color.canvas)
                .navigationTitle(navigationTitle)
                #if os(iOS)
                .navigationBarTitleDisplayMode(.inline)
                .toolbar {
                    if store.section == .assistant || store.section == nil {
                        ToolbarItem(placement: .topBarTrailing) {
                            Button { Task { await store.newChat() } } label: { Image(systemName: "square.and.pencil") }.accessibilityLabel("新对话")
                        }
                    }
                }
                #else
                .toolbar { ToolbarItem(placement: .primaryAction) { ConnectionIndicator() } }
                #endif
            }
        }
    }

    var navigationTitle: String {
        if store.section == .assistant || store.section == nil {
            return store.currentSession?.title ?? "Jurvis"
        }
        return store.section?.rawValue ?? "Jurvis"
    }
}

/// Conversation history first, like a chat app; the other tools sit in a quiet footer.
struct Sidebar: View {
    @EnvironmentObject var store: AppStore
    var show: () -> Void
    @State private var query = ""
    @State private var pendingDelete: Session?

    var sessions: [Session] {
        let all = store.snapshot?.sessions ?? []
        return query.isEmpty ? all : all.filter { $0.title.localizedCaseInsensitiveContains(query) }
    }

    var groups: [(String, [Session])] {
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

    var selection: Binding<String?> {
        Binding(get: { store.section == .assistant ? store.selectedSession : nil },
                set: { id in if let id { Task { await store.selectSession(id) }; show() } })
    }

    var body: some View {
        List(selection: selection) {
            ForEach(groups, id: \.0) { title, items in
                SwiftUI.Section(title) {
                    ForEach(items) { session in
                        HStack(spacing: 7) {
                            if session.code_project_id != nil {
                                Image(systemName: "chevron.left.forwardslash.chevron.right").font(.caption2).foregroundStyle(.secondary)
                            }
                            Text(session.title).lineLimit(1)
                            Spacer(minLength: 4)
                            if isBusy(session) { ProgressView().controlSize(.mini) }
                        }
                        .tag(session.id)
                        .contextMenu { Button("删除对话", role: .destructive) { pendingDelete = session } }
                    }
                }
            }
            if sessions.isEmpty {
                Text(query.isEmpty ? "还没有对话" : "没有匹配的对话").font(.callout).foregroundStyle(.secondary)
            }
        }
        .listStyle(.sidebar)
        .searchable(text: $query, placement: .sidebar, prompt: "搜索对话")
        .safeAreaInset(edge: .top, spacing: 0) { header }
        .safeAreaInset(edge: .bottom, spacing: 0) { footer }
        #if os(iOS)
        .toolbar(.hidden, for: .navigationBar)
        #endif
        .confirmationDialog("删除这段对话？", isPresented: Binding(get: { pendingDelete != nil }, set: { if !$0 { pendingDelete = nil } }), titleVisibility: .visible) {
            Button("删除", role: .destructive) {
                if let session = pendingDelete { Task { await store.deleteSession(session.id) } }
            }
        } message: { Text("对话记录会从 Mac 上删除，生成的素材和任务会保留。") }
    }

    func isBusy(_ session: Session) -> Bool {
        store.activeJobs.contains { $0.kind == "chat" && $0.payload["session_id"].string == session.id }
    }

    var header: some View {
        HStack(spacing: 10) {
            BrandMark(size: 26)
            Text("Jurvis").font(.headline)
            Spacer()
            Button { Task { await store.newChat() }; show() } label: {
                Image(systemName: "square.and.pencil").font(.body.weight(.medium))
            }
            .buttonStyle(.borderless).help("新对话（⌘N）").accessibilityLabel("新对话")
        }
        .padding(.horizontal, 16).padding(.vertical, 10)
    }

    var footer: some View {
        VStack(spacing: 2) {
            Divider().padding(.bottom, 4)
            ForEach([Section.library, .drama, .tasks], id: \.self) { item in footerRow(item) }
            Menu {
                ForEach([Section.memory, .files, .settings], id: \.self) { item in
                    Button { store.section = item; show() } label: { Label(item.rawValue, systemImage: item.icon) }
                }
            } label: {
                Label("更多", systemImage: "ellipsis.circle").frame(maxWidth: .infinity, alignment: .leading)
            }
            .menuStyle(.button).buttonStyle(.plain).menuIndicator(.hidden)
            .padding(.horizontal, 10).padding(.vertical, 6)
        }
        .padding(.horizontal, 8).padding(.bottom, 8)
        .font(.callout)
        .background(.bar)
    }

    func footerRow(_ item: Section) -> some View {
        Button { store.section = item; show() } label: {
            HStack {
                Label(item.rawValue, systemImage: item.icon)
                Spacer()
                if item == .tasks, !store.activeJobs.isEmpty {
                    Text("\(store.activeJobs.count)").font(.caption2.monospacedDigit().weight(.semibold)).foregroundStyle(.secondary)
                }
            }
            .padding(.horizontal, 10).padding(.vertical, 6)
            .background(store.section == item ? Color.bubble : .clear, in: RoundedRectangle(cornerRadius: 8))
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
    }
}

/// Shows whether the Mac engine is reachable and what it is busy with.
struct ConnectionIndicator: View {
    @EnvironmentObject var store: AppStore
    var body: some View {
        HStack(spacing: 6) {
            if store.connecting {
                ProgressView().controlSize(.small)
            } else {
                Circle().fill(store.connected ? Color.green.opacity(0.8) : .warning).frame(width: 7, height: 7)
            }
            Text(detail).font(.caption).foregroundStyle(.secondary).lineLimit(1)
        }
        .padding(.horizontal, 6)
        .help(store.connectionStatus)
        .accessibilityElement(children: .combine)
    }
    var detail: String {
        guard store.connected else { return store.connecting ? "正在连接" : "未连接" }
        if let running = store.activeJobs.first(where: { $0.status == "running" }) { return running.title + " · " + running.phase }
        return "本机运行"
    }
}
