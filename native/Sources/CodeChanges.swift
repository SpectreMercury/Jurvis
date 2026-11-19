import SwiftUI
import UniformTypeIdentifiers

struct CodeProject: Codable, Identifiable, Hashable, Equatable { var id: String; var name: String; var root: String }
struct BuildCommand: Codable, Identifiable { var id: String; var label: String }

// MARK: - Inline change set

/// A change Jurvis wrote in the conversation: the diff, then Apply / Undo and Run tests in place.
struct ChangeSetCard: View {
    @EnvironmentObject var store: AppStore
    let jobID: String
    @State private var full: Job?
    @State private var commands: [BuildCommand] = []
    @State private var starting = false
    @State private var failure: String?

    /// Live status comes from the polled snapshot; the diff itself from the full job.
    var live: Job? { store.job(jobID) ?? full }
    var applied: Bool { live?.result["applied_at"].number != nil }
    var reverted: Bool { live?.result["reverted_at"].number != nil }
    var changes: [JSONValue] { full?.result["changes"].array ?? [] }
    var projectID: String { live?.result["code_project_id"].string ?? "" }
    var testRun: Job? {
        (store.snapshot?.jobs ?? []).first { $0.kind == "code_test" && $0.payload["change_id"].string == jobID }
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            if full == nil {
                BusyRing()
            }
            ForEach(Array(changes.enumerated()), id: \.offset) { _, change in
                DiffView(path: change["path"].string, diff: change["diff"].string, isNew: change["before"].isNull)
            }
            if full != nil { actions }
            if let failure { Label(failure, systemImage: "exclamationmark.triangle").font(.jSmall).foregroundStyle(Color.danger).textSelection(.enabled) }
            if let run = testRun { TestRunView(job: run) }
        }
        .task { await load() }
    }

    @ViewBuilder var actions: some View {
        HStack(spacing: 10) {
            if reverted {
                Label("已撤销", systemImage: "arrow.uturn.backward").font(.jCallout).foregroundStyle(Color.inkSoft)
            } else if applied {
                Label("已写入文件", systemImage: "checkmark.circle.fill").font(.jCallout).foregroundStyle(Color.signal)
                ActionButton(title: "撤销", busyTitle: "正在撤销", scale: .small) { await change("revert") }
            } else {
                ActionButton(title: "应用修改", busyTitle: "正在应用", icon: "checkmark", kind: .primary, scale: .small) { await change("apply") }
            }
            if !reverted, !commands.isEmpty {
                let testing = starting || testRun?.isActive == true
                Menu {
                    ForEach(commands) { command in Button(command.label) { Task { await run(command) } } }
                } label: { ButtonFace(title: testing ? "测试中" : "运行测试", icon: "play", busy: testing) }
                .menuStyle(.button).buttonStyle(.jurvis(.secondary, .small)).menuIndicator(.hidden)
                .fixedSize()
                .disabled(testing || !applied)
                .help(applied ? "在 Mac 的项目目录里运行（无沙箱，最长 5 分钟）" : "先应用修改再运行测试")
            }
            Spacer()
        }
    }

    func load() async {
        guard let api = store.api else { return }
        do {
            full = try await api.request("/api/jobs/\(jobID)")
            if !projectID.isEmpty { commands = (try? await api.request("/api/code/projects/\(projectID)/commands")) ?? [] }
        } catch { failure = error.localizedDescription }
    }

    func change(_ action: String) async {
        failure = nil
        do { let _: JSONValue = try await store.requireAPI().request("/api/code/jobs/\(jobID)/\(action)", method: "POST") }
        catch { failure = error.localizedDescription }
    }

    func run(_ command: BuildCommand) async {
        starting = true; failure = nil; defer { starting = false }
        do { _ = try await store.submit("/api/code/projects/\(projectID)/run", body: ["command_id": command.id, "change_id": jobID]) }
        catch { failure = error.localizedDescription }
    }
}

struct TestRunView: View {
    @EnvironmentObject var store: AppStore
    var job: Job
    @State private var expanded = false
    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack(spacing: 8) {
                if job.isActive { SignalDot() }
                else { Image(systemName: job.status == "succeeded" ? "checkmark.circle.fill" : "xmark.octagon.fill")
                        .foregroundStyle(job.status == "succeeded" ? Color.success : Color.danger) }
                Text(job.result["command"].string.isEmpty ? "测试" : job.result["command"].string).font(.jCallout.monospaced())
                Text(job.isActive ? "运行中…" : job.status == "succeeded" ? "通过" : (job.error ?? job.statusLabel))
                    .font(.jSmall).foregroundStyle(Color.inkSoft).lineLimit(1)
                Spacer()
                if job.isActive { ActionButton(title: "停止", busyTitle: "正在停止", kind: .ghost, scale: .small, isBusy: store.isCancelling(job)) { await store.cancel(job) } }
                if !job.result["output"].string.isEmpty {
                    Button(expanded ? "收起输出" : "查看输出") { expanded.toggle() }.buttonStyle(.jurvis(.ghost, .small))
                }
            }
            if expanded || (job.status == "failed" && !job.result["output"].string.isEmpty) {
                ScrollView { Text(job.result["output"].string).font(.jMono).textSelection(.enabled).frame(maxWidth: .infinity, alignment: .leading) }
                    .frame(maxHeight: 220).padding(8)
                    .background(Color.bubble, in: RoundedRectangle(cornerRadius: 8, style: .continuous))
            }
        }
        .padding(10)
        .surface(radius: Metrics.smallRadius)
    }
}

struct DiffView: View {
    var path: String
    var diff: String
    var isNew: Bool
    @State private var expanded = false
    let preview = 40

    var body: some View {
        let lines = diff.components(separatedBy: "\n").filter { !$0.hasPrefix("---") && !$0.hasPrefix("+++") && !$0.isEmpty }
        let shown = expanded ? lines : Array(lines.prefix(preview))
        VStack(alignment: .leading, spacing: 0) {
            HStack {
                Image(systemName: "doc.text")
                Text(path).font(.jCallout.monospaced().weight(.medium)).lineLimit(1).truncationMode(.middle)
                if isNew { Badge(text: "新文件") }
                Spacer()
                Text("+\(lines.filter { $0.hasPrefix("+") }.count) −\(lines.filter { $0.hasPrefix("-") }.count)").font(.jSmall.monospaced()).foregroundStyle(Color.inkSoft)
                CopyButton(text: diff, title: "复制差异", iconOnly: true)
            }
            .padding(10)
            Divider()
            ScrollView(.horizontal) {
                VStack(alignment: .leading, spacing: 0) {
                    ForEach(Array(shown.enumerated()), id: \.offset) { _, line in
                        Text(line)
                            .foregroundStyle(line.hasPrefix("@@") ? Color.inkSoft : .ink)
                            .frame(maxWidth: .infinity, alignment: .leading)
                            .padding(.horizontal, 10)
                            .foregroundStyle(line.hasPrefix("-") ? Color.inkFaint : Color.ink).strikethrough(line.hasPrefix("-"), color: Color.inkFaint)
                            .background(line.hasPrefix("+") ? Color.bubble : .clear)
                    }
                }
                .font(.jMono).textSelection(.enabled).padding(.vertical, 6)
            }
            if lines.count > preview {
                Divider()
                Button(expanded ? "收起" : "显示全部 \(lines.count) 行") { expanded.toggle() }
                    .buttonStyle(.jurvis(.ghost, .small)).padding(4)
            }
        }
        .surface(radius: Metrics.smallRadius)
    }
}
