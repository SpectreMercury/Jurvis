import SwiftUI
import UniformTypeIdentifiers

struct CodeProject: Codable, Identifiable, Hashable { var id: String; var name: String; var root: String }
struct CodeFile: Codable, Identifiable { var path: String; var bytes: Int; var id: String { path } }
struct CodeTree: Codable { var files: [CodeFile]; var truncated: Bool }
struct CodeSource: Codable, Identifiable { var path: String; var content: String; var id: String { path } }
struct BuildCommand: Codable, Identifiable { var id: String; var label: String }
struct CodeMatches: Codable { var matches: [CodeMatch]; var truncated: Bool }
struct CodeMatch: Codable, Identifiable { var path: String; var line: Int; var text: String; var id: String { "\(path):\(line)" } }

struct CodingView: View {
    @EnvironmentObject var store: AppStore
    @State private var projects: [CodeProject] = []
    @State private var projectID = ""
    @State private var files: [CodeFile] = []
    @State private var selected = Set<String>()
    @State private var commands: [BuildCommand] = []
    @State private var instruction = ""
    @State private var filter = ""
    @State private var query = ""
    @State private var matches: [CodeMatch] = []
    @State private var source: CodeSource?
    @State private var review: Job?
    @State private var importing = false
    @State private var pendingCommand: BuildCommand?
    @State private var submitting = false

    let limit = 12
    var project: CodeProject? { projects.first { $0.id == projectID } }
    var jobs: [Job] { (store.snapshot?.jobs ?? []).filter { $0.payload["code_project_id"].string == projectID } }
    var visibleFiles: [CodeFile] { filter.isEmpty ? files : files.filter { $0.path.localizedCaseInsensitiveContains(filter) } }

    var body: some View {
        Page(title: "Coding", subtitle: "连接本地项目，让 Jurvis 阅读代码并给出修改建议；你审阅差异后再应用，测试由你手动运行。") {
            if !projects.isEmpty {
                Picker("项目", selection: $projectID) { ForEach(projects) { Text($0.name).tag($0.id) } }.labelsHidden().frame(maxWidth: 220)
            }
            #if os(macOS)
            Button { importing = true } label: { Label("连接项目", systemImage: "folder.badge.plus") }.buttonStyle(.bordered)
            #endif
        } content: {
            if let project {
                Text(project.root).font(.caption.monospaced()).foregroundStyle(.secondary).textSelection(.enabled)
                taskCard
                ViewThatFits(in: .horizontal) {
                    HStack(alignment: .top, spacing: 16) { filesCard.frame(minWidth: 420); sideColumn.frame(width: 320) }
                    VStack(spacing: 16) { filesCard; sideColumn }
                }
            } else {
                EmptyState(icon: "chevron.left.forwardslash.chevron.right", title: "先连接一个代码项目",
                           detail: "在 Mac 上选择项目目录。之后配对的 iPhone、iPad 也能浏览这些项目，修改快照保存在 Mac。")
            }
        }
        .task { await loadProjects() }
        .onChange(of: projectID) { _, _ in selected = []; matches = []; filter = ""; Task { await loadProject() } }
        .fileImporter(isPresented: $importing, allowedContentTypes: [.folder]) { result in
            Task { await store.perform {
                let url = try result.get()
                let granted = url.startAccessingSecurityScopedResource(); defer { if granted { url.stopAccessingSecurityScopedResource() } }
                let project: CodeProject = try await store.requireAPI().request("/api/code/projects", method: "POST", body: ["root": url.path])
                await loadProjects(); projectID = project.id
            } }
        }
        .sheet(item: $source) { SourceViewer(source: $0) }
        .sheet(item: $review) { CodeReviewView(jobID: $0.id).environmentObject(store) }
        .confirmationDialog("运行项目脚本？", isPresented: Binding(get: { pendingCommand != nil }, set: { if !$0 { pendingCommand = nil } }), titleVisibility: .visible) {
            if let command = pendingCommand {
                Button("运行 \(command.label)") { Task { await store.perform { _ = try await store.submit("/api/code/projects/\(projectID)/run", body: ["command_id": command.id]) } } }
            }
        } message: { Text("脚本会在 Mac 的项目目录中执行，最长 5 分钟，没有沙箱。请只运行你信任的项目。") }
    }

    // MARK: Sections

    var taskCard: some View {
        Card(padding: 18) {
            SectionTitle(title: "想完成什么？", detail: selected.isEmpty ? "未选文件时读取说明文件和主要源码" : "已选 \(selected.count)/\(limit) 个文件")
            FieldEditor(placeholder: "例如：修复 calc.py 中加法的错误，并补充测试", text: $instruction, minHeight: 90)
            if !selected.isEmpty {
                FlowChips(items: selected.sorted()) { path in selected.remove(path) }
            }
            HStack {
                Hint(text: "生成的是修改建议，应用前会检查文件是否被其他编辑器改动。")
                Spacer()
                Button(submitting ? "正在提交…" : "生成修改建议") { Task { await propose() } }
                    .buttonStyle(.borderedProminent)
                    .disabled(instruction.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty || submitting || !store.connected)
            }
        }
    }

    var filesCard: some View {
        Card {
            SectionTitle(title: "项目文件", detail: "\(files.count) 个可读文件")
            HStack {
                TextField("筛选文件名", text: $filter).textFieldStyle(.roundedBorder)
                TextField("搜索代码内容", text: $query).textFieldStyle(.roundedBorder).onSubmit { Task { await search() } }
            }
            if !matches.isEmpty {
                VStack(alignment: .leading, spacing: 6) {
                    HStack { Text("\(matches.count) 处匹配").font(.caption.weight(.semibold)); Spacer(); Button("清除") { matches = [] }.buttonStyle(.borderless).font(.caption) }
                    ForEach(matches) { match in
                        Button { Task { await read(match.path) } } label: {
                            VStack(alignment: .leading, spacing: 2) {
                                Text("\(match.path):\(match.line)").font(.caption.weight(.medium))
                                Text(match.text.trimmingCharacters(in: .whitespaces)).font(.caption.monospaced()).foregroundStyle(.secondary).lineLimit(1)
                            }.frame(maxWidth: .infinity, alignment: .leading).contentShape(Rectangle())
                        }.buttonStyle(.plain)
                    }
                }
                .padding(10).background(Color.subtleFill, in: RoundedRectangle(cornerRadius: 8))
            }
            ScrollView {
                LazyVStack(alignment: .leading, spacing: 2) {
                    ForEach(visibleFiles) { file in fileRow(file) }
                }
            }
            .frame(minHeight: 200, maxHeight: 380)
            Hint(text: "隐藏目录、凭据文件、大于 100KB 的文件和符号链接不会交给模型。", icon: "lock")
        }
    }

    func fileRow(_ file: CodeFile) -> some View {
        let isOn = selected.contains(file.path)
        return HStack(spacing: 8) {
            Button {
                if isOn { selected.remove(file.path) } else if selected.count < limit { selected.insert(file.path) }
            } label: {
                Image(systemName: isOn ? "checkmark.square.fill" : "square").foregroundStyle(isOn ? Color.jurvisAccent : .secondary)
            }
            .buttonStyle(.plain).disabled(!isOn && selected.count >= limit)
            .accessibilityLabel(isOn ? "取消选择 \(file.path)" : "选择 \(file.path)")
            Text(file.path).font(.caption.monospaced()).lineLimit(1).truncationMode(.middle)
            Spacer()
            Text(file.bytes.fileSize).font(.caption2).foregroundStyle(.tertiary)
            Button { Task { await read(file.path) } } label: { Image(systemName: "eye") }.buttonStyle(.borderless).help("阅读").accessibilityLabel("阅读 \(file.path)")
        }
        .padding(.vertical, 4).padding(.horizontal, 6)
        .background(isOn ? Color.jurvisAccent.opacity(0.08) : .clear, in: RoundedRectangle(cornerRadius: 6))
    }

    var sideColumn: some View {
        VStack(spacing: 16) {
            if !commands.isEmpty {
                Card {
                    SectionTitle(title: "构建与测试")
                    ForEach(commands) { command in
                        Button { pendingCommand = command } label: { Label(command.label, systemImage: "play").frame(maxWidth: .infinity, alignment: .leading) }
                            .buttonStyle(.bordered).disabled(jobs.contains { $0.kind == "code_test" && $0.isActive })
                    }
                }
            }
            Card {
                SectionTitle(title: "记录", detail: jobs.isEmpty ? nil : "\(jobs.count)")
                if jobs.isEmpty { Text("还没有修改建议或测试记录。").font(.caption).foregroundStyle(.secondary) }
                ForEach(jobs.prefix(20)) { job in historyRow(job) }
            }
        }
    }

    func historyRow(_ job: Job) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack(spacing: 6) {
                Image(systemName: job.icon).foregroundStyle(Color.jurvisAccent)
                Text(job.kind == "code" ? "修改建议" : job.result["command"].string.isEmpty ? job.title : job.result["command"].string)
                    .font(.caption.weight(.semibold)).lineLimit(1)
                Badge(text: job.statusLabel, tone: job.tone)
                Spacer()
            }
            if job.kind == "code" {
                Text(job.result["summary"].string.isEmpty ? job.payload["instruction"].string : job.result["summary"].string)
                    .font(.caption).foregroundStyle(.secondary).lineLimit(3)
                if job.status == "succeeded" {
                    let count = Int(job.result["change_count"].number ?? 0)
                    Button(count > 0 ? "审阅 \(count) 个文件的修改" : "查看分析") { review = job }.buttonStyle(.borderless).font(.caption)
                }
            }
            if job.isActive { Text(job.phase).font(.caption2).foregroundStyle(.secondary) }
            if let error = job.error { Text(error).font(.caption2).foregroundStyle(Color.warning).lineLimit(3) }
            if job.kind == "code_test", !job.result["output"].string.isEmpty {
                DisclosureGroup("输出") {
                    ScrollView([.horizontal, .vertical]) { Text(job.result["output"].string).font(.system(.caption2, design: .monospaced)).textSelection(.enabled) }.frame(maxHeight: 240)
                }.font(.caption)
            }
            Divider()
        }
    }

    // MARK: Actions

    func loadProjects() async {
        guard let api = store.api else { return }
        await store.perform {
            projects = try await api.request("/api/code/projects")
            if projectID.isEmpty || !projects.contains(where: { $0.id == projectID }), let first = projects.first { projectID = first.id }
        }
    }
    func loadProject() async {
        guard !projectID.isEmpty, let api = store.api else { return }
        await store.perform {
            let tree: CodeTree = try await api.request("/api/code/projects/\(projectID)/files")
            files = tree.files
            commands = try await api.request("/api/code/projects/\(projectID)/commands")
        }
    }
    func queryPath(_ suffix: String, _ name: String, _ value: String) -> String {
        var c = URLComponents(); c.queryItems = [URLQueryItem(name: name, value: value)]
        return "/api/code/projects/\(projectID)/\(suffix)?" + (c.percentEncodedQuery ?? "")
    }
    func read(_ path: String) async { await store.perform { source = try await store.requireAPI().request(queryPath("file", "path", path)) } }
    func search() async {
        guard !query.isEmpty else { return }
        await store.perform { let result: CodeMatches = try await store.requireAPI().request(queryPath("search", "q", query)); matches = result.matches }
    }
    func propose() async {
        submitting = true; defer { submitting = false }
        await store.perform {
            _ = try await store.submit("/api/code/projects/\(projectID)/tasks", body: ["instruction": instruction, "files": selected.sorted()])
            instruction = ""
        }
    }
}

struct FlowChips: View {
    var items: [String]
    var remove: (String) -> Void
    var body: some View {
        ScrollView(.horizontal, showsIndicators: false) {
            HStack(spacing: 6) {
                ForEach(items, id: \.self) { item in
                    HStack(spacing: 4) {
                        Text(item).font(.caption.monospaced()).lineLimit(1)
                        Button { remove(item) } label: { Image(systemName: "xmark") }.buttonStyle(.borderless).font(.caption2)
                    }
                    .padding(.horizontal, 8).padding(.vertical, 4)
                    .background(Color.jurvisAccent.opacity(0.1), in: Capsule())
                }
            }
        }
    }
}

struct SourceViewer: View {
    @Environment(\.dismiss) var dismiss
    var source: CodeSource
    var body: some View {
        NavigationStack {
            ScrollView([.horizontal, .vertical]) {
                let lines = source.content.components(separatedBy: "\n")
                VStack(alignment: .leading, spacing: 0) {
                    ForEach(Array(lines.enumerated()), id: \.offset) { n, line in
                        HStack(alignment: .top, spacing: 12) {
                            Text("\(n + 1)").foregroundStyle(.tertiary).frame(minWidth: 34, alignment: .trailing)
                            Text(line.isEmpty ? " " : line)
                        }
                    }
                }
                .font(.system(.caption, design: .monospaced)).textSelection(.enabled).padding(20)
            }
            .navigationTitle(source.path)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) { Button("完成") { dismiss() } }
                ToolbarItem(placement: .primaryAction) { Button { copyText(source.content) } label: { Image(systemName: "doc.on.doc") }.help("复制") }
            }
        }
        #if os(macOS)
        .frame(width: 880, height: 660)
        #endif
    }
}

struct CodeReviewView: View {
    @EnvironmentObject var store: AppStore
    @Environment(\.dismiss) var dismiss
    let jobID: String
    @State private var job: Job?
    @State private var busy = false
    var applied: Bool { job?.result["applied_at"].number != nil }
    var reverted: Bool { job?.result["reverted_at"].number != nil }
    var changes: [JSONValue] { job?.result["changes"].array ?? [] }

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 16) {
                    if let job {
                        MarkdownText(text: job.result["summary"].string)
                        if changes.isEmpty { Hint(text: "这次没有文件修改。") }
                        ForEach(Array(changes.enumerated()), id: \.offset) { _, change in
                            DiffView(path: change["path"].string, diff: change["diff"].string, isNew: change["before"].isNull)
                        }
                    } else { ProgressView().frame(maxWidth: .infinity, minHeight: 200) }
                }.padding(20)
            }
            .safeAreaInset(edge: .bottom) { if job != nil, !changes.isEmpty { actionBar } }
            .navigationTitle("审阅修改")
            .toolbar { ToolbarItem(placement: .cancellationAction) { Button("完成") { dismiss() } } }
            .task { await load() }
        }
        #if os(macOS)
        .frame(width: 920, height: 720)
        #endif
    }

    var actionBar: some View {
        HStack {
            if reverted {
                Label("已撤销本次修改", systemImage: "arrow.uturn.backward").foregroundStyle(.secondary)
                Spacer()
            } else if applied {
                Label("已应用，建议运行项目测试", systemImage: "checkmark.circle.fill").foregroundStyle(Color.jurvisAccent)
                Spacer()
                Button("撤销本次修改") { Task { await change("revert") } }.buttonStyle(.bordered).disabled(busy)
            } else {
                Hint(text: "应用前会检查文件版本，避免覆盖你在其他编辑器中的改动。")
                Spacer()
                Button("应用 \(changes.count) 个文件") { Task { await change("apply") } }.buttonStyle(.borderedProminent).disabled(busy)
            }
        }
        .font(.callout).padding(14).background(.bar)
    }

    func load() async { await store.perform { job = try await store.requireAPI().request("/api/jobs/\(jobID)") } }
    func change(_ action: String) async {
        busy = true; defer { busy = false }
        await store.perform { let _: JSONValue = try await store.requireAPI().request("/api/code/jobs/\(jobID)/\(action)", method: "POST"); await load() }
    }
}

struct DiffView: View {
    var path: String
    var diff: String
    var isNew: Bool
    var body: some View {
        let lines = diff.components(separatedBy: "\n").filter { !$0.hasPrefix("---") && !$0.hasPrefix("+++") }
        VStack(alignment: .leading, spacing: 0) {
            HStack {
                Image(systemName: "doc.text")
                Text(path).font(.callout.monospaced().weight(.medium))
                if isNew { Badge(text: "新文件", tone: .success) }
                Spacer()
                Text("+\(lines.filter { $0.hasPrefix("+") }.count) −\(lines.filter { $0.hasPrefix("-") }.count)").font(.caption.monospaced()).foregroundStyle(.secondary)
            }
            .padding(10)
            Divider()
            ScrollView(.horizontal) {
                VStack(alignment: .leading, spacing: 0) {
                    ForEach(Array(lines.enumerated()), id: \.offset) { _, line in
                        Text(line.isEmpty ? " " : line)
                            .foregroundStyle(line.hasPrefix("@@") ? Color.blue : .primary)
                            .frame(maxWidth: .infinity, alignment: .leading)
                            .padding(.horizontal, 10)
                            .background(line.hasPrefix("+") ? Color.green.opacity(0.13) : line.hasPrefix("-") ? Color.red.opacity(0.12) : .clear)
                    }
                }
                .font(.system(.caption, design: .monospaced)).textSelection(.enabled).padding(.vertical, 6)
            }
        }
        .background(Color.card, in: RoundedRectangle(cornerRadius: 10))
        .overlay(RoundedRectangle(cornerRadius: 10).strokeBorder(Color.hairline))
    }
}
