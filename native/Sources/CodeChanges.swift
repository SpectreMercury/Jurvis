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
    @State private var busy = false
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
                ProgressView().controlSize(.small)
            }
            ForEach(Array(changes.enumerated()), id: \.offset) { _, change in
                DiffView(path: change["path"].string, diff: change["diff"].string, isNew: change["before"].isNull)
            }
            if full != nil { actions }
            if let failure { Label(failure, systemImage: "exclamationmark.triangle").font(.caption).foregroundStyle(Color.warning).textSelection(.enabled) }
            if let run = testRun { TestRunView(job: run) }
        }
        .task { await load() }
    }

    @ViewBuilder var actions: some View {
        HStack(spacing: 10) {
            if reverted {
                Label("已撤销", systemImage: "arrow.uturn.backward").foregroundStyle(.secondary)
            } else if applied {
                Label("已写入文件", systemImage: "checkmark.circle.fill").foregroundStyle(Color.jurvisAccent)
                Button("撤销") { Task { await change("revert") } }.buttonStyle(.bordered).disabled(busy)
            } else {
                Button { Task { await change("apply") } } label: { Label("应用修改", systemImage: "checkmark") }
                    .buttonStyle(.borderedProminent).disabled(busy)
            }
            if !reverted, !commands.isEmpty {
                Menu {
                    ForEach(commands) { command in Button(command.label) { Task { await run(command) } } }
                } label: { Label("运行测试", systemImage: "play") }
                .fixedSize()
                .disabled(busy || testRun?.isActive == true || !applied)
                .help(applied ? "在 Mac 的项目目录里运行（无沙箱，最长 5 分钟）" : "先应用修改再运行测试")
            }
            Spacer()
        }
        .font(.callout)
    }

    func load() async {
        guard let api = store.api else { return }
        do {
            full = try await api.request("/api/jobs/\(jobID)")
            if !projectID.isEmpty { commands = (try? await api.request("/api/code/projects/\(projectID)/commands")) ?? [] }
        } catch { failure = error.localizedDescription }
    }

    func change(_ action: String) async {
        busy = true; failure = nil; defer { busy = false }
        do {
            let _: JSONValue = try await store.requireAPI().request("/api/code/jobs/\(jobID)/\(action)", method: "POST")
            await store.refresh()
        } catch { failure = error.localizedDescription }
    }

    func run(_ command: BuildCommand) async {
        busy = true; failure = nil; defer { busy = false }
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
                if job.isActive { ProgressView().controlSize(.small) }
                else { Image(systemName: job.status == "succeeded" ? "checkmark.circle.fill" : "xmark.octagon.fill")
                        .foregroundStyle(job.status == "succeeded" ? Color.jurvisAccent : Color.warning) }
                Text(job.result["command"].string.isEmpty ? "测试" : job.result["command"].string).font(.callout.monospaced())
                Text(job.isActive ? "运行中…" : job.status == "succeeded" ? "通过" : (job.error ?? job.statusLabel))
                    .font(.caption).foregroundStyle(.secondary).lineLimit(1)
                Spacer()
                if job.isActive { Button("停止") { Task { await store.cancel(job) } }.buttonStyle(.borderless).font(.caption) }
                if !job.result["output"].string.isEmpty {
                    Button(expanded ? "收起输出" : "查看输出") { expanded.toggle() }.buttonStyle(.borderless).font(.caption)
                }
            }
            if expanded || (job.status == "failed" && !job.result["output"].string.isEmpty) {
                ScrollView { Text(job.result["output"].string).font(.system(.caption2, design: .monospaced)).textSelection(.enabled).frame(maxWidth: .infinity, alignment: .leading) }
                    .frame(maxHeight: 220).padding(8)
                    .background(Color.subtleFill, in: RoundedRectangle(cornerRadius: 8))
            }
        }
        .padding(10)
        .background(Color.card, in: RoundedRectangle(cornerRadius: 10))
        .overlay(RoundedRectangle(cornerRadius: 10).strokeBorder(Color.hairline))
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
                Text(path).font(.callout.monospaced().weight(.medium)).lineLimit(1).truncationMode(.middle)
                if isNew { Badge(text: "新文件", tone: .success) }
                Spacer()
                Text("+\(lines.filter { $0.hasPrefix("+") }.count) −\(lines.filter { $0.hasPrefix("-") }.count)").font(.caption.monospaced()).foregroundStyle(.secondary)
                Button { copyText(diff) } label: { Image(systemName: "doc.on.doc") }.buttonStyle(.borderless).help("复制差异")
            }
            .padding(10)
            Divider()
            ScrollView(.horizontal) {
                VStack(alignment: .leading, spacing: 0) {
                    ForEach(Array(shown.enumerated()), id: \.offset) { _, line in
                        Text(line)
                            .foregroundStyle(line.hasPrefix("@@") ? Color.blue : .primary)
                            .frame(maxWidth: .infinity, alignment: .leading)
                            .padding(.horizontal, 10)
                            .background(line.hasPrefix("+") ? Color.green.opacity(0.13) : line.hasPrefix("-") ? Color.red.opacity(0.12) : .clear)
                    }
                }
                .font(.system(.caption, design: .monospaced)).textSelection(.enabled).padding(.vertical, 6)
            }
            if lines.count > preview {
                Divider()
                Button(expanded ? "收起" : "显示全部 \(lines.count) 行") { expanded.toggle() }
                    .buttonStyle(.borderless).font(.caption).padding(8)
            }
        }
        .background(Color.card, in: RoundedRectangle(cornerRadius: 10))
        .overlay(RoundedRectangle(cornerRadius: 10).strokeBorder(Color.hairline))
    }
}
