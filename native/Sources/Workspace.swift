import SwiftUI

// MARK: - What a task is doing

extension AppStore {
    /// Tasks that stopped to ask for a yes or no. The engine says so in the job's phase.
    var waitingSessions: [(session: Session, job: Job)] {
        activeJobs.filter { $0.kind == "chat" && $0.phase.hasPrefix("等待你确认") }.compactMap { job in
            snapshot?.sessions.first { $0.id == job.payload["session_id"].string }.map { (session: $0, job: job) }
        }
    }

    func chatJob(of session: Session) -> Job? {
        activeJobs.first { $0.kind == "chat" && $0.payload["session_id"].string == session.id }
    }

    /// One line for the open task: waiting for you, or running and how far along the plan it is.
    var runStatus: String? {
        if chatJob?.phase.hasPrefix("等待你确认") ?? false { return "等你确认" }
        guard chatBusy else { return nil }
        if let plan = messages.last?.plan, !plan.isEmpty {
            return "进行中 · 第 \(min(plan.filter(\.done).count + 1, plan.count)) / \(plan.count) 步"
        }
        return "进行中"
    }

    /// The plan the open task is following, from the latest reply that has one.
    var currentPlan: [PlanStep] { messages.last { !$0.plan.isEmpty }?.plan ?? [] }
}

extension Message {
    /// Workspace files this reply read or wrote, oldest first.
    var touchedFiles: [String] {
        steps.flatMap(\.calls).filter { ["save_file", "append_file", "edit_file", "read_file"].contains($0.name) }
            .compactMap { $0.arguments?["name"].string }.filter { !$0.isEmpty }
    }
}

// MARK: - Workspace

/// "Jurvis 的工作台": the document the task is writing, the files in the workspace, the pages it read, and the plan.
/// It follows the task: whenever a tool call lands, the file being written is read again.
struct WorkspacePanel: View {
    @EnvironmentObject var store: AppStore
    /// Set when the panel is a sheet, to show a way out.
    var onClose: (() -> Void)? = nil
    @State private var tab = "文档"
    @State private var files: [WorkspaceFile] = []
    @State private var document: WorkspaceContent?
    /// A file the user picked; nil follows whatever the task touched last.
    @State private var picked: String?
    @State private var loaded = false
    @State private var showPlan = false

    var sources: [JSONValue] { store.messages.last { !$0.isUser && !$0.metadata["sources"].array.isEmpty }?.metadata["sources"].array ?? [] }
    var tabs: [String] { sources.isEmpty ? ["文档", "文件"] : ["文档", "文件", "来源"] }
    /// Changes whenever the task has done something that may have changed a file.
    var reloadKey: String {
        let last = store.messages.last
        return [store.selectedSession ?? "", last?.id ?? "", last?.status ?? "", String(last?.steps.flatMap(\.calls).count ?? 0), picked ?? ""].joined(separator: "|")
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            ViewThatFits(in: .horizontal) {
                HStack(spacing: 10) { title; switcher(fill: false) }
                VStack(alignment: .leading, spacing: 10) { title; switcher(fill: true) }
            }
            Group {
                switch tabs.contains(tab) ? tab : "文档" {
                case "文件": fileList
                case "来源": sourceList
                default: documentView
                }
            }
            .frame(maxWidth: .infinity, maxHeight: .infinity)
            .clipShape(RoundedRectangle(cornerRadius: 12, style: .continuous))
            .overlay(RoundedRectangle(cornerRadius: 12, style: .continuous).strokeBorder(Color.hairline))
            planRow
        }
        .padding(14)
        .background(Color.canvas)
        .task(id: reloadKey) { await load() }
    }

    var title: some View {
        HStack(spacing: 10) {
            if let onClose {
                Button(action: onClose) { Image(systemName: "chevron.down") }.buttonStyle(.jurvisIcon(.ghost, .regular)).accessibilityLabel("收起工作台")
            } else {
                Image(systemName: "macwindow").font(.system(size: 14)).frame(width: 32, height: 32)
                    .background(Color.bubble, in: RoundedRectangle(cornerRadius: 10, style: .continuous)).accessibilityHidden(true)
            }
            VStack(alignment: .leading, spacing: 1) {
                Text("Jurvis 的工作台").font(.jHeading).lineLimit(1)
                if let status = store.runStatus, store.chatBusy {
                    RunStatusLine(text: store.chatJob?.phase.isEmpty == false ? store.chatJob!.phase : status)
                } else {
                    Text(loaded ? "\(files.count) 个文件" : "正在读取").font(.jMicro).foregroundStyle(Color.inkFaint).lineLimit(1)
                }
            }
            .frame(minWidth: 96, alignment: .leading)
            Spacer(minLength: 0)
        }
    }

    func switcher(fill: Bool) -> some View {
        SegmentedControl(options: tabs, selection: $tab, icons: ["文档": "doc.text", "文件": "folder", "来源": "globe"], fill: fill)
    }

    // MARK: Document

    @ViewBuilder var documentView: some View {
        if let document {
            VStack(spacing: 0) {
                HStack(spacing: 8) {
                    Image(systemName: "doc.text").font(.system(size: 12)).foregroundStyle(Color.inkSoft)
                    Text(document.name).font(.jMono).lineLimit(1).truncationMode(.middle)
                    Spacer(minLength: 8)
                    Text("\(document.content.count) 字").font(.jMicro).foregroundStyle(Color.inkSoft).fixedSize()
                    ShareLink(item: document.content) { Image(systemName: "square.and.arrow.up") }
                        .buttonStyle(.jurvisIcon(.ghost, .small)).accessibilityLabel("分享或导出")
                }
                .padding(.leading, 12).padding(.trailing, 4).frame(height: 40)
                .background(Color.subtleFill)
                .overlay(alignment: .bottom) { Rectangle().fill(Color.hairline).frame(height: 1) }
                ScrollView {
                    Group {
                        if document.name.hasSuffix(".md") || document.name.hasSuffix(".txt") { MarkdownText(text: document.content) }
                        else { Text(document.content).font(.jMono).textSelection(.enabled) }
                    }
                    .frame(maxWidth: .infinity, alignment: .leading).padding(.horizontal, 22).padding(.vertical, 20)
                }
            }
        } else if !loaded {
            LoadingState(text: "正在读取工作台")
        } else {
            EmptyState(icon: "doc.text", title: "还没有文档", detail: "Jurvis 写下或读过的文件会出现在这里。")
        }
    }

    // MARK: Files

    @ViewBuilder var fileList: some View {
        if files.isEmpty {
            if loaded { EmptyState(icon: "folder", title: "还没有文件", detail: "让 Jurvis 把写好的内容保存下来，或在作品库导入文本。") }
            else { LoadingState(text: "正在读取文件") }
        } else {
            ScrollView {
                LazyVStack(spacing: 0) {
                    ForEach(Array(files.enumerated()), id: \.element.id) { index, file in
                        if index > 0 { Rectangle().fill(Color.hairline).frame(height: 1).padding(.leading, 40) }
                        Button { picked = file.name; tab = "文档" } label: {
                            HStack(spacing: 10) {
                                Image(systemName: "doc.text").font(.system(size: 13)).foregroundStyle(Color.inkSoft).frame(width: 18)
                                Text(file.name).font(.jMono).foregroundStyle(Color.ink).lineLimit(1).truncationMode(.middle)
                                Spacer(minLength: 8)
                                Text(file.bytes.fileSize).font(.jMicro).foregroundStyle(Color.inkFaint).fixedSize()
                            }
                            .padding(.horizontal, 12).frame(minHeight: 44)
                            .background(file.name == document?.name ? Color.selection : .clear)
                            .contentShape(Rectangle())
                        }
                        .buttonStyle(.plain)
                        .accessibilityAddTraits(file.name == document?.name ? .isSelected : [])
                    }
                }
            }
        }
    }

    // MARK: Sources

    var sourceList: some View {
        ScrollView {
            LazyVStack(alignment: .leading, spacing: 0) {
                ForEach(Array(sources.enumerated()), id: \.offset) { index, source in
                    if let url = URL(string: source["url"].string), ["http", "https"].contains(url.scheme ?? "") {
                        if index > 0 { Rectangle().fill(Color.hairline).frame(height: 1).padding(.leading, 40) }
                        Link(destination: url) {
                            HStack(spacing: 10) {
                                Image(systemName: "globe").font(.system(size: 13)).foregroundStyle(Color.inkSoft).frame(width: 18)
                                VStack(alignment: .leading, spacing: 1) {
                                    Text(source["title"].string.isEmpty ? (url.host ?? url.absoluteString) : source["title"].string)
                                        .font(.jCallout).foregroundStyle(Color.ink).lineLimit(1)
                                    Text(url.host ?? "").font(.jMicro).foregroundStyle(Color.inkFaint).lineLimit(1)
                                }
                                Spacer(minLength: 8)
                                Image(systemName: "arrow.up.right").font(.system(size: 10)).foregroundStyle(Color.inkFaint)
                            }
                            .padding(.horizontal, 12).frame(minHeight: 52).contentShape(Rectangle())
                        }
                        .buttonStyle(.plain)
                    }
                }
            }
        }
    }

    // MARK: Plan

    /// One row for the plan: the step in hand and how far along it is. It opens to the whole list.
    @ViewBuilder var planRow: some View {
        let plan = store.currentPlan
        if !plan.isEmpty {
            let done = plan.filter(\.done).count
            let live = store.chatBusy
            let current = plan.first { !$0.done }
            VStack(alignment: .leading, spacing: 0) {
                if showPlan {
                    ScrollView { PlanList(plan: plan, live: live).padding(.horizontal, 12).padding(.top, 8) }.frame(maxHeight: 220)
                    Rectangle().fill(Color.hairline).frame(height: 1)
                }
                Button { withAnimation(.easeOut(duration: 0.15)) { showPlan.toggle() } } label: {
                    HStack(spacing: 10) {
                        Image(systemName: "checklist").font(.system(size: 13)).foregroundStyle(Color.inkSoft)
                        Text(current.map { (live ? "" : "下一步：") + $0.step } ?? "计划已全部完成").font(.jCallout).foregroundStyle(Color.ink).lineLimit(1)
                        Spacer(minLength: 8)
                        Text("\(done) / \(plan.count)").font(.jMicro.monospacedDigit()).foregroundStyle(Color.inkFaint).fixedSize()
                        Image(systemName: "chevron.up").font(.system(size: 10)).foregroundStyle(Color.inkFaint).rotationEffect(.degrees(showPlan ? 180 : 0))
                    }
                    .padding(.horizontal, 12).frame(minHeight: 44).contentShape(Rectangle())
                }
                .buttonStyle(.plain)
                .accessibilityLabel("计划，已完成 \(done) 步，共 \(plan.count) 步")
            }
            .background(Color.subtleFill, in: RoundedRectangle(cornerRadius: 12, style: .continuous))
        }
    }

    func load() async {
        guard let api = store.api else { return }
        if let listing: FileList = try? await api.request("/api/files"), listing.files.map(\.name) != files.map(\.name) || listing.files.map(\.bytes) != files.map(\.bytes) {
            files = listing.files
        }
        let touched = store.messages.suffix(6).flatMap(\.touchedFiles)
        let name = picked ?? touched.last { name in files.contains { $0.name == name } } ?? files.first?.name
        if let name, let encoded = name.addingPercentEncoding(withAllowedCharacters: .urlPathAllowed),
           let content: WorkspaceContent = try? await api.request("/api/files/" + encoded) {
            if content.name != document?.name || content.content != document?.content { document = content }
        } else if name == nil {
            document = nil
        }
        loaded = true
    }
}

/// On a phone the workspace is one row above the composer; it opens as a sheet.
struct WorkspaceBar: View {
    @EnvironmentObject var store: AppStore
    var action: () -> Void

    var body: some View {
        let plan = store.currentPlan
        let step = plan.first { !$0.done }?.step
        let file = store.messages.last { !$0.isUser }?.touchedFiles.last
        Button(action: action) {
            HStack(spacing: 10) {
                Image(systemName: "doc.text").font(.system(size: 14)).frame(width: 32, height: 32)
                    .background(Color.card, in: RoundedRectangle(cornerRadius: 10, style: .continuous))
                    .overlay(RoundedRectangle(cornerRadius: 10, style: .continuous).strokeBorder(Color.hairline))
                VStack(alignment: .leading, spacing: 1) {
                    Text(step.map { (store.chatBusy ? "正在：" : "下一步：") + $0 } ?? "Jurvis 的工作台").font(.jBody).lineLimit(1)
                    Text(file ?? (plan.isEmpty ? "文档、文件和来源" : "计划 · 已完成 \(plan.filter(\.done).count) / \(plan.count) 步"))
                        .font(file == nil ? .jMicro : .system(.caption, design: .monospaced)).foregroundStyle(Color.inkFaint).lineLimit(1).truncationMode(.middle)
                }
                Spacer(minLength: 8)
                Image(systemName: "chevron.up").font(.system(size: 12)).foregroundStyle(Color.inkSoft)
            }
            .foregroundStyle(Color.ink)
            .padding(.horizontal, 12).frame(height: 52)
            .background(Color.subtleFill, in: RoundedRectangle(cornerRadius: 16, style: .continuous))
            .overlay(RoundedRectangle(cornerRadius: 16, style: .continuous).strokeBorder(Color.hairline))
            .contentShape(Rectangle())
        }
        .buttonStyle(PressStyle())
        .accessibilityLabel("打开工作台")
    }
}

// MARK: - Adaptive run

/// A task and its workspace, arranged by the room there is. Size decides, never the device:
/// - narrower than 500pt: the conversation alone, the workspace behind a bar (a phone, a folded outer screen);
/// - 700pt or wider, or squarish and at least 600pt: conversation left, workspace right (an unfolded inner screen, an iPad);
/// - in between and tall: workspace on top, conversation below.
struct TaskRunView: View {
    enum Arrangement { case single, sideBySide, stacked }
    @EnvironmentObject var store: AppStore
    @State private var size = CGSize(width: 390, height: 800)
    @State private var showingWorkspace = AppStore.checkStart == "workspace"

    static func arrangement(for size: CGSize) -> Arrangement {
        let squarish = size.height > 0 && size.width / size.height > 0.8
        if size.width >= Metrics.twoPaneWidth || (size.width >= 600 && squarish) { return .sideBySide }
        if size.width >= Metrics.phoneWidth && size.height >= 700 { return .stacked }
        return .single
    }

    var body: some View {
        let running = store.selectedSession != nil || !store.visibleMessages.isEmpty
        let arrangement = running ? Self.arrangement(for: size) : .single
        let layout = arrangement == .stacked ? AnyLayout(VStackLayout(spacing: 0)) : AnyLayout(HStackLayout(spacing: 0))
        layout {
            if arrangement == .stacked {
                WorkspacePanel().frame(height: size.height * 0.44)
                    .overlay(alignment: .bottom) { Rectangle().fill(Color.hairline).frame(height: 1) }
            }
            ChatView(onWorkspace: running && arrangement == .single ? { showingWorkspace = true } : nil)
            if arrangement == .sideBySide {
                WorkspacePanel().frame(width: min(max(size.width * 0.46, 300), 520))
                    .overlay(alignment: .leading) { Rectangle().fill(Color.hairline).frame(width: 1) }
            }
        }
        .background(Color.canvas)
        .onGeometryChange(for: CGSize.self) { $0.size } action: { size = $0 }
        .sheet(isPresented: $showingWorkspace) {
            WorkspacePanel(onClose: { showingWorkspace = false }).environmentObject(store)
                .presentationDragIndicator(.visible)
        }
    }
}
