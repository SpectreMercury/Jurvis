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
                .frame(minWidth: 920, minHeight: 620)
                #endif
        }
        #if os(macOS)
        .defaultSize(width: 1200, height: 820)
        .commands {
            CommandGroup(replacing: .newItem) {
                Button("新对话") { Task { await store.newChat() } }.keyboardShortcut("n")
            }
            CommandGroup(after: .sidebar) {
                ForEach(Array(Section.allCases.enumerated()), id: \.element) { index, section in
                    if index < 9 { Button(section.rawValue) { store.section = section }.keyboardShortcut(KeyEquivalent(Swift.Character("\(index + 1)")), modifiers: .command) }
                }
            }
        }
        #endif
    }
}

/// Sidebar selection: a workspace, or one saved conversation.
enum Destination: Hashable {
    case section(Section)
    case session(String)
}

struct RootView: View {
    @EnvironmentObject var store: AppStore
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

    var selection: Binding<Destination?> {
        Binding(
            get: {
                if store.section == .assistant, let id = store.selectedSession { return .session(id) }
                return store.section.map(Destination.section)
            },
            set: { value in
                switch value {
                case .session(let id): Task { await store.selectSession(id) }
                case .section(.assistant): Task { await store.newChat() }
                case .section(let section): store.section = section
                case nil: store.section = nil
                }
            })
    }

    var workspace: some View {
        NavigationSplitView {
            Sidebar(selection: selection)
                .navigationSplitViewColumnWidth(min: 210, ideal: 240, max: 300)
        } detail: {
            NavigationStack {
                Group {
                    switch store.section ?? .assistant {
                    case .assistant: ChatView()
                    case .coding: CodingView()
                    case .studio: StudioView()
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
                #endif
                #if os(macOS)
                .toolbar { ToolbarItem(placement: .primaryAction) { ConnectionIndicator() } }
                #endif
            }
        }
        .navigationSplitViewStyle(.balanced)
    }

    var navigationTitle: String {
        if store.section == .assistant, let id = store.selectedSession {
            return store.snapshot?.sessions.first { $0.id == id }?.title ?? "对话"
        }
        return store.section?.rawValue ?? "助理"
    }
}

struct Sidebar: View {
    @EnvironmentObject var store: AppStore
    @Binding var selection: Destination?
    @State private var pendingDelete: Session?

    var body: some View {
        List(selection: $selection) {
            SwiftUI.Section {
                row(.assistant, title: "新对话", icon: "square.and.pencil")
                ForEach([Section.coding, .studio, .drama], id: \.self) { row($0) }
            }
            SwiftUI.Section("资料") {
                ForEach([Section.library, .tasks, .memory, .files], id: \.self) { row($0) }
            }
            if let sessions = store.snapshot?.sessions, !sessions.isEmpty {
                SwiftUI.Section("最近对话") {
                    ForEach(sessions.prefix(30)) { session in
                        HStack(spacing: 8) {
                            Text(session.title).lineLimit(1)
                            Spacer(minLength: 4)
                            if isBusy(session) { ProgressView().controlSize(.mini) }
                        }
                        .tag(Destination.session(session.id))
                        .contextMenu {
                            Button("删除对话", role: .destructive) { pendingDelete = session }
                        }
                    }
                }
            }
            SwiftUI.Section { row(.settings) }
        }
        .listStyle(.sidebar)
        .safeAreaInset(edge: .top) { header }
        .confirmationDialog("删除这段对话？", isPresented: Binding(get: { pendingDelete != nil }, set: { if !$0 { pendingDelete = nil } }), titleVisibility: .visible) {
            Button("删除", role: .destructive) {
                if let session = pendingDelete { Task { await store.deleteSession(session.id) } }
            }
        } message: { Text("对话记录会从 Mac 上删除，生成的素材和任务会保留。") }
    }

    func row(_ section: Section, title: String? = nil, icon: String? = nil) -> some View {
        HStack {
            Label(title ?? section.rawValue, systemImage: icon ?? section.icon)
            Spacer()
            if section == .tasks, !store.activeJobs.isEmpty {
                Text("\(store.activeJobs.count)").font(.caption.monospacedDigit().weight(.semibold))
                    .foregroundStyle(.white).padding(.horizontal, 6).padding(.vertical, 1)
                    .background(Color.jurvisAccent, in: Capsule())
            }
        }
        .tag(Destination.section(section))
    }

    func isBusy(_ session: Session) -> Bool {
        store.activeJobs.contains { $0.kind == "chat" && $0.payload["session_id"].string == session.id }
    }

    var header: some View {
        HStack(spacing: 10) {
            BrandMark(size: 30)
            VStack(alignment: .leading, spacing: 1) {
                Text("Jurvis").font(.headline)
                Text(store.connectionStatus).font(.caption2).foregroundStyle(.secondary).lineLimit(1)
            }
            Spacer()
        }
        .padding(.horizontal, 16).padding(.vertical, 10)
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
                Circle().fill(store.connected ? Color.jurvisAccent : .warning).frame(width: 7, height: 7)
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
        switch store.snapshot?.runtime.active {
        case "llm": return "语言模型就绪"
        case "comfy": return "创作引擎就绪"
        default: return "待命"
        }
    }
}
