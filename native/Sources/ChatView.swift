import SwiftUI
import UniformTypeIdentifiers

/// What the next message should make; picked with the chips under the composer.
enum ComposerMode: String, CaseIterable, Identifiable {
    case image, video, drama
    var id: String { rawValue }
    var title: String { ["image": "图片", "video": "视频", "drama": "短剧"][rawValue]! }
    var icon: String { ["image": "photo", "video": "film", "drama": "theatermasks"][rawValue]! }
    var placeholder: String { ["image": "描述你想要的画面", "video": "描述镜头、动作和声音", "drama": "说说故事、时长和风格"][rawValue]! }
}

/// The whole app is this conversation: an empty chat is a centred composer, like a chat app.
struct ChatView: View {
    @EnvironmentObject var store: AppStore
    @State private var input = ""
    @State private var sending = false
    @State private var importing = false
    @State private var connecting = false
    @State private var attachments: [Asset] = []
    @State private var mode: ComposerMode?
    @State private var preview: Asset?
    @FocusState private var focused: Bool

    var body: some View {
        Group {
            if store.messages.isEmpty && store.selectedSession == nil {
                home
            } else {
                conversation
            }
        }
        .background(Color.canvas)
        .sheet(item: $preview) { MediaDetail(asset: $0).environmentObject(store) }
        .fileImporter(isPresented: $importing, allowedContentTypes: [.image], allowsMultipleSelection: true) { result in
            Task { await store.perform {
                for url in try result.get().prefix(max(0, 4 - attachments.count)) {
                    attachments.append(try await store.uploadImage(url))
                }
            } }
        }
        .fileImporter(isPresented: $connecting, allowedContentTypes: [.folder]) { result in
            Task { await store.perform {
                let url = try result.get()
                let granted = url.startAccessingSecurityScopedResource(); defer { if granted { url.stopAccessingSecurityScopedResource() } }
                let project: CodeProject = try await store.requireAPI().request("/api/code/projects", method: "POST", body: ["root": url.path])
                await store.loadCodeProjects()
                await store.newChat(project: project.id)
            } }
        }
        #if os(macOS)
        .onAppear { focused = true }  // on iPhone this would pop the keyboard and swallow the next tap
        #endif
        // A draft belongs to the conversation it was typed in.
        .onChange(of: store.selectedSession) { _, _ in input = ""; attachments = [] }
    }

    var home: some View {
        VStack(spacing: 26) {
            Spacer(minLength: 40)
            VStack(spacing: 14) {
                BrandMark(size: 52)
                Text(store.activeProject.map { "聊聊 \($0.name) 的代码" } ?? "有什么可以帮你？")
                    .font(.system(size: 28, weight: .semibold)).multilineTextAlignment(.center)
            }
            composer.frame(maxWidth: 720)
            Text(store.activeProject == nil ? "对话、读图、计算、记忆，也能生成图片、视频和短剧，或修改你的代码。全部在你的 Mac 上运行。"
                                             : "Jurvis 会先阅读代码，修改以差异形式出现在对话里，你点「应用修改」才会写入文件。")
                .font(.footnote).foregroundStyle(.secondary).multilineTextAlignment(.center).frame(maxWidth: 520)
            Spacer(minLength: 60)
        }
        .padding(.horizontal, 20)
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }

    var conversation: some View {
        VStack(spacing: 0) {
            ScrollViewReader { proxy in
                ScrollView {
                    LazyVStack(alignment: .leading, spacing: 26) {
                        ForEach(store.messages) { message in
                            if message.isUser { UserMessage(message: message, open: { preview = $0 }) }
                            else { AssistantMessage(message: message, open: { preview = $0 }) }
                        }
                        Color.clear.frame(height: 1).id("bottom")
                    }
                    .padding(.horizontal, 20).padding(.top, 24).padding(.bottom, 12)
                    .frame(maxWidth: Metrics.readable).frame(maxWidth: .infinity)
                }
                .scrollDismissesKeyboard(.interactively)
                .onChange(of: store.messages.last?.content) { _, _ in proxy.scrollTo("bottom", anchor: .bottom) }
                .onChange(of: store.messages.last?.steps.flatMap(\.calls).count) { _, _ in proxy.scrollTo("bottom", anchor: .bottom) }
                .onChange(of: store.messages.count) { _, _ in withAnimation(.easeOut(duration: 0.2)) { proxy.scrollTo("bottom", anchor: .bottom) } }
                .onAppear { proxy.scrollTo("bottom", anchor: .bottom) }
            }
            composer
                .frame(maxWidth: Metrics.readable)
                .padding(.horizontal, 16).padding(.bottom, 14).padding(.top, 4)
        }
    }

    var canSend: Bool {
        !input.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty && store.connected && !sending && !store.chatBusy
    }

    var placeholder: String {
        if let mode { return mode.placeholder }
        if let project = store.activeProject { return "问问 \(project.name) 的代码，或说说要改什么" }
        return "问 Jurvis 任何事"
    }

    var composer: some View {
        VStack(alignment: .leading, spacing: 12) {
            if !attachments.isEmpty {
                ScrollView(.horizontal, showsIndicators: false) {
                    HStack(spacing: 8) {
                        ForEach(attachments) { asset in
                            MediaThumb(asset: asset).frame(width: 56, height: 56)
                                .overlay(alignment: .topTrailing) {
                                    Button { attachments.removeAll { $0.id == asset.id } } label: {
                                        Image(systemName: "xmark.circle.fill").symbolRenderingMode(.palette).foregroundStyle(.white, .black.opacity(0.6))
                                    }.buttonStyle(.plain).padding(3).accessibilityLabel("移除 \(asset.name)")
                                }
                        }
                    }
                }
            }
            TextField(placeholder, text: $input, axis: .vertical)
                .textFieldStyle(.plain).font(.body).lineLimit(1...10)
                .focused($focused)
                .onSubmit { if canSend { Task { await send() } } }
                .accessibilityLabel("消息输入框")
            HStack(spacing: 8) {
                Button { importing = true } label: { Image(systemName: "plus").font(.body.weight(.medium)).frame(width: 30, height: 30) }
                    .buttonStyle(.plain).background(Color.canvas.opacity(0.6), in: Circle()).overlay(Circle().strokeBorder(Color.hairline))
                    .help("添加图片（最多 4 张）").accessibilityLabel("添加图片").disabled(attachments.count >= 4)
                // Labels when they fit, icons only on narrow screens.
                ViewThatFits(in: .horizontal) {
                    chips(compact: false)
                    chips(compact: true)
                }
                Spacer(minLength: 4)
                sendButton
            }
        }
        .padding(.horizontal, 16).padding(.top, 14).padding(.bottom, 10)
        .background(Color.bubble, in: RoundedRectangle(cornerRadius: 26, style: .continuous))
        .overlay(RoundedRectangle(cornerRadius: 26, style: .continuous).strokeBorder(focused ? Color.primary.opacity(0.18) : Color.hairline))
    }

    func chips(compact: Bool) -> some View {
        HStack(spacing: 6) {
            ForEach(ComposerMode.allCases) { item in
                Chip(title: item.title, icon: item.icon, on: mode == item, compact: compact && mode != item) { mode = mode == item ? nil : item }
            }
            projectChip(compact: compact)
        }
        .fixedSize()
    }

    /// Binds a new conversation to a code project; an existing one shows its project.
    @ViewBuilder func projectChip(compact: Bool) -> some View {
        if store.selectedSession != nil {
            if let project = store.activeProject { Chip(title: project.name, icon: "chevron.left.forwardslash.chevron.right", on: true, compact: compact) {} .disabled(true) }
        } else {
            Menu {
                ForEach(store.codeProjects) { project in
                    Button { store.codeProjectID = project.id } label: {
                        if project.id == store.codeProjectID { Label(project.name, systemImage: "checkmark") } else { Text(project.name) }
                    }
                }
                if store.codeProjectID != nil { Button("不关联项目") { store.codeProjectID = nil } }
                #if os(macOS)
                Divider()
                Button { connecting = true } label: { Label("连接项目文件夹…", systemImage: "folder.badge.plus") }
                #endif
            } label: {
                Chip(title: store.activeProject?.name ?? "代码", icon: "chevron.left.forwardslash.chevron.right", on: store.activeProject != nil, compact: compact && store.activeProject == nil) {}
                    .allowsHitTesting(false)
            }
            .menuStyle(.button).buttonStyle(.plain).menuIndicator(.hidden).fixedSize()
            .help("让这段对话关联一个代码项目")
        }
    }

    @ViewBuilder var sendButton: some View {
        if let job = store.chatJob {
            Button { Task { await store.cancel(job) } } label: {
                Image(systemName: "stop.fill").font(.caption).foregroundStyle(Color.inkReversed).frame(width: 34, height: 34).background(Color.ink, in: Circle())
            }.buttonStyle(.plain).help("停止回复").accessibilityLabel("停止回复")
        } else {
            Button { Task { await send() } } label: {
                Group { if sending { ProgressView().controlSize(.small) } else { Image(systemName: "arrow.up").font(.body.weight(.semibold)) } }
                    .foregroundStyle(Color.inkReversed).frame(width: 34, height: 34)
                    .background(canSend ? Color.ink : Color.ink.opacity(0.25), in: Circle())
            }
            .buttonStyle(.plain).disabled(!canSend).keyboardShortcut(.return, modifiers: .command)
            .help("发送（Return）").accessibilityLabel("发送消息")
        }
    }

    func send() async {
        let text = input.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty else { return }
        sending = true; defer { sending = false }
        let sentAttachments = attachments
        input = ""; attachments = []
        do { try await store.send(text, assets: sentAttachments.map(\.id), mode: mode?.rawValue) }
        catch { input = text; attachments = sentAttachments; store.report(error) }
    }
}

struct Chip: View {
    var title: String
    var icon: String
    var on: Bool
    var compact = false
    var action: () -> Void
    var body: some View {
        Button(action: action) {
            Group {
                if compact { Image(systemName: icon).frame(width: 18) } else { Label(title, systemImage: icon) }
            }
            .font(.callout).lineLimit(1)
                .padding(.horizontal, compact ? 6 : 11).frame(height: 30)
                .foregroundStyle(on ? Color.inkReversed : .primary)
                .background(on ? Color.ink : Color.canvas.opacity(0.6), in: Capsule())
                .overlay(Capsule().strokeBorder(on ? .clear : Color.hairline))
                .contentShape(Capsule())
        }
        .buttonStyle(.plain)
        .help(title).accessibilityLabel(title)
        .accessibilityAddTraits(on ? .isSelected : [])
    }
}

// MARK: - Messages

struct UserMessage: View {
    @EnvironmentObject var store: AppStore
    var message: Message
    var open: (Asset) -> Void
    var body: some View {
        VStack(alignment: .trailing, spacing: 8) {
            let assets = message.attachmentIDs.compactMap { store.asset($0) }
            if !assets.isEmpty {
                HStack(spacing: 6) {
                    ForEach(assets) { asset in
                        Button { open(asset) } label: { MediaThumb(asset: asset).frame(width: 84, height: 84) }.buttonStyle(.plain)
                    }
                }
            }
            Text(message.content)
                .textSelection(.enabled)
                .padding(.horizontal, 14).padding(.vertical, 10)
                .background(Color.bubble, in: RoundedRectangle(cornerRadius: 20, style: .continuous))
                .contextMenu { Button("复制") { copyText(message.content) } }
        }
        .frame(maxWidth: .infinity, alignment: .trailing)
        .padding(.leading, 60)
    }
}

struct AssistantMessage: View {
    @EnvironmentObject var store: AppStore
    var message: Message
    var open: (Asset) -> Void

    var body: some View {
        let steps = message.steps
        VStack(alignment: .leading, spacing: 12) {
            if steps.isEmpty {
                if !message.content.isEmpty { MarkdownText(text: message.content) }
            } else {
                ForEach(Array(steps.enumerated()), id: \.offset) { _, step in
                    if !step.text.isEmpty { MarkdownText(text: step.text) }
                    if !step.calls.isEmpty { ToolTrace(calls: step.calls, open: open) }
                }
            }
            footer(hasContent: !message.content.isEmpty || !steps.isEmpty)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    @ViewBuilder func footer(hasContent: Bool) -> some View {
        if message.isLive {
            HStack(spacing: 8) {
                ProgressView().controlSize(.small)
                Text(liveStatus).font(.caption).foregroundStyle(.secondary)
            }
        } else {
            if !message.errorText.isEmpty {
                Label(message.errorText, systemImage: "exclamationmark.triangle").font(.caption).foregroundStyle(Color.warning).textSelection(.enabled)
            }
            if message.status == "cancelled" { Text("已停止").font(.caption).foregroundStyle(.secondary) }
            if message.status == "interrupted" { Text("引擎重启中断了这条回复").font(.caption).foregroundStyle(.secondary) }
            HStack(spacing: 14) {
                if hasContent {
                    Button { copyText(message.content) } label: { Label("复制", systemImage: "doc.on.doc") }
                }
                if ["failed", "cancelled", "interrupted"].contains(message.status), store.messages.last?.id == message.id {
                    Button { Task { await store.retryMessage(message) } } label: { Label("重试", systemImage: "arrow.clockwise") }
                }
                if message.metadata["truncated"] == .bool(true) {
                    Text("回复达到长度上限").foregroundStyle(.secondary)
                }
            }
            .font(.caption).buttonStyle(.borderless).foregroundStyle(.secondary)
        }
    }

    var liveStatus: String {
        if message.status == "queued" { return "排队中，前面的任务完成后开始" }
        if let job = store.snapshot?.jobs.first(where: { $0.kind == "chat" && $0.payload["message_id"].string == message.id }), !job.phase.isEmpty {
            return job.phase
        }
        return "正在思考"
    }
}

// MARK: - Tool trace

/// One quiet line per step ("阅读源码 · 修改代码"), expandable for arguments and results.
/// What a tool produced for the user (images, videos, code changes) always shows in full below it.
struct ToolTrace: View {
    @EnvironmentObject var store: AppStore
    var calls: [ToolCall]
    var open: (Asset) -> Void
    @State private var expanded = false

    var running: ToolCall? { calls.first(where: \.isRunning) }
    var failed: Bool { calls.contains(where: \.failed) }
    var title: String {
        if let running { return "正在" + running.label + "…" }
        return Array(NSOrderedSet(array: calls.map(\.label))).compactMap { $0 as? String }.joined(separator: " · ")
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            Button { withAnimation(.easeOut(duration: 0.15)) { expanded.toggle() } } label: {
                HStack(spacing: 7) {
                    if running != nil { ProgressView().controlSize(.mini) }
                    else { Image(systemName: failed ? "exclamationmark.circle" : "checkmark.circle").foregroundStyle(failed ? Color.warning : .secondary) }
                    Text(title).lineLimit(1)
                    Image(systemName: "chevron.right").font(.caption2.weight(.semibold)).rotationEffect(.degrees(expanded ? 90 : 0))
                }
                .font(.callout).foregroundStyle(.secondary).contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .accessibilityLabel("工具：\(title)")

            if expanded {
                VStack(alignment: .leading, spacing: 0) {
                    ForEach(calls) { call in ToolCallRow(call: call, open: open) }
                }
                .padding(.leading, 4)
                .overlay(alignment: .leading) { Rectangle().fill(Color.hairline).frame(width: 2) }
            }
            ForEach(calls) { call in
                if call.name == "edit_code", let id = call.job_id { ChangeSetCard(jobID: id) }
                else if let id = call.job_id, let job = store.job(id) { JobInline(job: job, open: open) }
            }
        }
    }
}

struct ToolCallRow: View {
    @EnvironmentObject var store: AppStore
    var call: ToolCall
    var open: (Asset) -> Void
    @State private var expanded = false

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Button { withAnimation(.easeOut(duration: 0.15)) { expanded.toggle() } } label: {
                HStack(spacing: 10) {
                    Image(systemName: ToolCallRow.icon(call.name)).font(.callout).foregroundStyle(Color.jurvisAccent).frame(width: 18)
                    Text(call.label).font(.callout.weight(.medium))
                    Text(argumentPreview).font(.caption).foregroundStyle(.secondary).lineLimit(1).truncationMode(.tail)
                    Spacer(minLength: 6)
                    status
                    Image(systemName: "chevron.right").font(.caption2.weight(.semibold)).foregroundStyle(.tertiary)
                        .rotationEffect(.degrees(expanded ? 90 : 0))
                }
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .accessibilityLabel("\(call.label)，\(call.summary ?? call.status)")

            if expanded { details.padding(.leading, 28) }
        }
        .padding(.horizontal, 12).padding(.vertical, 9)
    }

    @ViewBuilder var status: some View {
        if call.isRunning {
            ProgressView().controlSize(.mini)
        } else {
            HStack(spacing: 5) {
                if let summary = call.summary { Text(summary).font(.caption).foregroundStyle(call.failed ? Color.warning : .secondary).lineLimit(1) }
                Image(systemName: call.failed ? "exclamationmark.circle.fill" : "checkmark.circle.fill")
                    .font(.caption).foregroundStyle(call.failed ? Color.warning : Color.jurvisAccent)
            }
        }
    }

    var argumentPreview: String {
        guard case .object(let args)? = call.arguments else { return "" }
        let primary = ["expression", "prompt", "query", "name", "title", "path", "instruction", "job_id"].first { args[$0] != nil }
        if let primary { return args[primary]!.preview }
        return call.arguments?.preview ?? ""
    }

    var details: some View {
        VStack(alignment: .leading, spacing: 8) {
            if let args = call.arguments, case .object(let o) = args, !o.isEmpty {
                CodeBlock(title: "参数", text: prettyJSON(args))
            }
            if let output = call.output, !output.isEmpty {
                CodeBlock(title: call.failed ? "错误" : "结果", text: prettyJSON(output))
            }
            if let duration = call.duration { Text(String(format: "耗时 %.2f 秒", duration)).font(.caption2).foregroundStyle(.tertiary) }
        }
    }

    func prettyJSON(_ value: JSONValue) -> String {
        guard let data = try? JSONEncoder().encode(value) else { return value.preview }
        return prettyJSON(String(decoding: data, as: UTF8.self))
    }
    func prettyJSON(_ text: String) -> String {
        guard let data = text.data(using: .utf8), let obj = try? JSONSerialization.jsonObject(with: data),
              let out = try? JSONSerialization.data(withJSONObject: obj, options: [.prettyPrinted, .sortedKeys, .withoutEscapingSlashes]) else { return text }
        return String(decoding: out, as: UTF8.self)
    }

    static func icon(_ name: String) -> String {
        switch name {
        case "calculate": return "function"
        case "list_files", "read_file": return "doc.text"
        case "save_file": return "square.and.arrow.down"
        case "recall": return "magnifyingglass"
        case "remember": return "brain"
        case "create_image": return "photo"
        case "create_video": return "film"
        case "create_drama": return "film.stack"
        case "job_status": return "checklist"
        case "code_search": return "text.magnifyingglass"
        case "code_propose", "edit_code": return "wand.and.stars"
        default: return name.hasPrefix("code") ? "chevron.left.forwardslash.chevron.right" : "wrench.and.screwdriver"
        }
    }
}

/// Live view of a job a tool queued: progress while it runs, outputs when done.
struct JobInline: View {
    @EnvironmentObject var store: AppStore
    var job: Job
    var open: (Asset) -> Void
    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(spacing: 8) {
                Badge(text: job.statusLabel, tone: job.tone)
                if job.isActive { Text(job.phase).font(.caption).foregroundStyle(.secondary).lineLimit(1) }
                if let error = job.error { Text(error).font(.caption).foregroundStyle(Color.warning).lineLimit(2) }
                Spacer()
                if job.isActive {
                    Button("取消") { Task { await store.cancel(job) } }.buttonStyle(.borderless).font(.caption)
                }
                destination
            }
            let outputs = store.outputs(of: job)
            if !outputs.isEmpty {
                ScrollView(.horizontal, showsIndicators: false) {
                    HStack(spacing: 8) {
                        ForEach(outputs) { asset in
                            Button { open(asset) } label: { MediaThumb(asset: asset).frame(width: 180, height: 180) }.buttonStyle(.plain)
                        }
                    }
                }
            }
        }
    }
    @ViewBuilder var destination: some View {
        switch job.kind {
        case "plan": Button("打开短剧") { store.section = .drama }.buttonStyle(.borderless).font(.caption)
        default: EmptyView()
        }
    }
}

// MARK: - Markdown

/// Block-level Markdown: headings, lists, quotes, fenced code; inline styles via AttributedString.
struct MarkdownText: View {
    var text: String
    enum Block: Hashable {
        case paragraph(String), heading(Int, String), bullet(String, String), quote(String), code(String, String), rule
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 9) {
            ForEach(Array(MarkdownText.parse(text).enumerated()), id: \.offset) { _, block in
                switch block {
                case .paragraph(let s): inline(s).lineSpacing(4)
                case .heading(let level, let s): inline(s).font(level == 1 ? .title3.weight(.semibold) : .headline).padding(.top, 4)
                case .bullet(let marker, let s):
                    HStack(alignment: .firstTextBaseline, spacing: 8) {
                        Text(marker).foregroundStyle(.secondary).monospacedDigit()
                        inline(s).lineSpacing(3)
                    }
                case .quote(let s):
                    inline(s).foregroundStyle(.secondary).padding(.leading, 10)
                        .overlay(alignment: .leading) { Rectangle().fill(Color.hairline).frame(width: 3) }
                case .code(let lang, let s): CodeBlock(title: lang.isEmpty ? nil : lang, text: s)
                case .rule: Divider()
                }
            }
        }
        .textSelection(.enabled)
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    func inline(_ s: String) -> Text {
        let options = AttributedString.MarkdownParsingOptions(interpretedSyntax: .inlineOnlyPreservingWhitespace)
        return Text((try? AttributedString(markdown: s, options: options)) ?? AttributedString(s))
    }

    static func parse(_ text: String) -> [Block] {
        var blocks: [Block] = [], paragraph: [String] = []
        var lines = text.components(separatedBy: "\n")[...]
        func flush() { if !paragraph.isEmpty { blocks.append(.paragraph(paragraph.joined(separator: "\n"))); paragraph = [] } }
        while let line = lines.popFirst() {
            let trimmed = line.trimmingCharacters(in: .whitespaces)
            if trimmed.hasPrefix("```") {
                flush()
                var code: [String] = []
                while let next = lines.popFirst(), !next.trimmingCharacters(in: .whitespaces).hasPrefix("```") { code.append(next) }
                blocks.append(.code(String(trimmed.dropFirst(3)).trimmingCharacters(in: .whitespaces), code.joined(separator: "\n")))
            } else if trimmed.isEmpty {
                flush()
            } else if let level = trimmed.firstIndex(where: { $0 != "#" }).map({ trimmed.distance(from: trimmed.startIndex, to: $0) }),
                      (1...4).contains(level), trimmed.dropFirst(level).first == " " {
                flush(); blocks.append(.heading(level, String(trimmed.dropFirst(level + 1))))
            } else if ["- ", "* ", "• "].contains(where: trimmed.hasPrefix) {
                flush(); blocks.append(.bullet("•", String(trimmed.dropFirst(2))))
            } else if let dot = trimmed.firstIndex(of: "."), trimmed[..<dot].allSatisfy(\.isNumber), !trimmed[..<dot].isEmpty,
                      trimmed[trimmed.index(after: dot)...].first == " " {
                flush(); blocks.append(.bullet(String(trimmed[...dot]), String(trimmed[trimmed.index(dot, offsetBy: 2)...])))
            } else if trimmed.hasPrefix("> ") {
                flush(); blocks.append(.quote(String(trimmed.dropFirst(2))))
            } else if trimmed == "---" || trimmed == "***" {
                flush(); blocks.append(.rule)
            } else {
                paragraph.append(line)
            }
        }
        flush()
        return blocks
    }
}

struct CodeBlock: View {
    var title: String?
    var text: String
    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            HStack {
                Text(title ?? "").font(.caption2.weight(.semibold)).foregroundStyle(.secondary)
                Spacer()
                Button { copyText(text) } label: { Image(systemName: "doc.on.doc") }.buttonStyle(.borderless).font(.caption).help("复制")
            }
            .padding(.horizontal, 10).padding(.vertical, 5)
            Divider()
            ScrollView(.horizontal) {
                Text(text).font(.system(.caption, design: .monospaced)).textSelection(.enabled).padding(10)
            }
            .frame(maxHeight: 320)
        }
        .background(Color.primary.opacity(0.035), in: RoundedRectangle(cornerRadius: 8))
        .overlay(RoundedRectangle(cornerRadius: 8).strokeBorder(Color.hairline))
    }
}
