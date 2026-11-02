import SwiftUI
import UniformTypeIdentifiers

struct ChatView: View {
    @EnvironmentObject var store: AppStore
    @State private var input = ""
    @State private var sending = false
    @State private var importing = false
    @State private var attachments: [Asset] = []
    @State private var preview: Asset?
    @FocusState private var focused: Bool

    var body: some View {
        VStack(spacing: 0) {
            ScrollViewReader { proxy in
                ScrollView {
                    LazyVStack(alignment: .leading, spacing: 22) {
                        if store.messages.isEmpty {
                            Welcome { input = $0; focused = true }.padding(.top, 40)
                        } else {
                            ForEach(store.messages) { message in
                                if message.isUser { UserMessage(message: message, open: { preview = $0 }) }
                                else { AssistantMessage(message: message, open: { preview = $0 }) }
                            }
                        }
                        Color.clear.frame(height: 1).id("bottom")
                    }
                    .padding(.horizontal, Metrics.pagePadding).padding(.vertical, 20)
                    .frame(maxWidth: Metrics.readable).frame(maxWidth: .infinity)
                }
                .scrollDismissesKeyboard(.interactively)
                .onChange(of: store.messages.last?.content) { _, _ in proxy.scrollTo("bottom", anchor: .bottom) }
                .onChange(of: store.messages.last?.steps.flatMap(\.calls).count) { _, _ in proxy.scrollTo("bottom", anchor: .bottom) }
                .onChange(of: store.messages.count) { _, _ in withAnimation(.easeOut(duration: 0.2)) { proxy.scrollTo("bottom", anchor: .bottom) } }
                .onAppear { proxy.scrollTo("bottom", anchor: .bottom) }
            }
            composer
                .frame(maxWidth: Metrics.readable + 20)
                .padding(.horizontal, 16).padding(.bottom, 14).padding(.top, 6)
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
        .onAppear { focused = true }
        // A draft belongs to the conversation it was typed in.
        .onChange(of: store.selectedSession) { _, _ in input = ""; attachments = [] }
    }

    var canSend: Bool {
        !input.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty && store.connected && !sending && !store.chatBusy
    }

    var composer: some View {
        VStack(alignment: .leading, spacing: 10) {
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
            TextField("给 Jurvis 发消息", text: $input, axis: .vertical)
                .textFieldStyle(.plain).font(.body).lineLimit(1...10)
                .focused($focused)
                .onSubmit { if canSend { Task { await send() } } }
                .accessibilityLabel("消息输入框")
            HStack(spacing: 12) {
                Button { importing = true } label: { Image(systemName: "paperclip") }
                    .buttonStyle(.borderless).help("添加图片（最多 4 张）").disabled(attachments.count >= 4)
                Text("Qwen 3.8 · 在你的 Mac 上运行").font(.caption).foregroundStyle(.tertiary)
                Spacer()
                if let job = store.chatJob {
                    Button { Task { await store.cancel(job) } } label: {
                        Image(systemName: "stop.fill").frame(width: 18, height: 18)
                    }.buttonStyle(.bordered).clipShape(Circle()).help("停止回复").accessibilityLabel("停止回复")
                } else {
                    Button { Task { await send() } } label: {
                        Group { if sending { ProgressView().controlSize(.small) } else { Image(systemName: "arrow.up").fontWeight(.semibold) } }
                            .frame(width: 18, height: 18)
                    }
                    .buttonStyle(.borderedProminent).clipShape(Circle())
                    .disabled(!canSend).keyboardShortcut(.return, modifiers: .command)
                    .help("发送（Return）").accessibilityLabel("发送消息")
                }
            }
        }
        .padding(.horizontal, 14).padding(.vertical, 12)
        .background(Color.card, in: RoundedRectangle(cornerRadius: 18))
        .overlay(RoundedRectangle(cornerRadius: 18).strokeBorder(focused ? Color.jurvisAccent.opacity(0.5) : Color.hairline))
        .shadow(color: .black.opacity(0.05), radius: 8, y: 2)
    }

    func send() async {
        let text = input.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty else { return }
        sending = true; defer { sending = false }
        let sentAttachments = attachments
        input = ""; attachments = []
        do { try await store.send(text, assets: sentAttachments.map(\.id)) }
        catch { input = text; attachments = sentAttachments; store.report(error) }
    }
}

// MARK: - Welcome

struct Welcome: View {
    var pick: (String) -> Void
    let starters: [(String, String, String)] = [
        ("checklist", "梳理今天的工作", "帮我梳理今天的工作，先问我最重要的目标和截止时间。"),
        ("photo", "生成一张图片", "生成一张雨后上海街道的电影感照片，暖色店铺灯光，地面有细腻倒影。"),
        ("film", "策划一部短剧", "帮我策划一部 30 秒短剧：一个每天修表的老人，在旧手表里发现来自未来的留言。写实电影风格，中文对白。"),
        ("chevron.left.forwardslash.chevron.right", "看看我的代码", "列出我连接的代码项目，并概括第一个项目的结构。"),
    ]
    var body: some View {
        VStack(alignment: .leading, spacing: 22) {
            VStack(alignment: .leading, spacing: 6) {
                Text("有什么可以帮你？").font(.title.weight(.semibold))
                Text("Jurvis 可以对话、读图、计算、管理文件和记忆，也能直接为你生成图片、视频、短剧和代码修改建议。")
                    .font(.callout).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
            }
            LazyVGrid(columns: [GridItem(.adaptive(minimum: 220), spacing: 10)], spacing: 10) {
                ForEach(starters, id: \.1) { icon, title, prompt in
                    Button { pick(prompt) } label: {
                        HStack(alignment: .top, spacing: 10) {
                            Image(systemName: icon).foregroundStyle(Color.jurvisAccent).frame(width: 20)
                            VStack(alignment: .leading, spacing: 3) {
                                Text(title).font(.callout.weight(.medium)).foregroundStyle(.primary)
                                Text(prompt).font(.caption).foregroundStyle(.secondary).lineLimit(2).multilineTextAlignment(.leading)
                            }
                            Spacer(minLength: 0)
                        }
                        .padding(12).frame(maxWidth: .infinity, alignment: .leading)
                        .background(Color.card, in: RoundedRectangle(cornerRadius: 12))
                        .overlay(RoundedRectangle(cornerRadius: 12).strokeBorder(Color.hairline))
                    }.buttonStyle(.plain)
                }
            }
            Hint(text: "对话、素材和记忆都保存在你的 Mac 上。", icon: "lock")
        }
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
                .background(Color.jurvisAccent.opacity(0.12), in: RoundedRectangle(cornerRadius: 16))
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

struct ToolTrace: View {
    var calls: [ToolCall]
    var open: (Asset) -> Void
    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            ForEach(Array(calls.enumerated()), id: \.element.id) { index, call in
                if index > 0 { Divider().padding(.leading, 34) }
                ToolCallRow(call: call, open: open)
            }
        }
        .background(Color.subtleFill, in: RoundedRectangle(cornerRadius: 12))
        .overlay(RoundedRectangle(cornerRadius: 12).strokeBorder(Color.hairline))
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

            if let id = call.job_id, let job = store.job(id) { JobInline(job: job, open: open).padding(.leading, 28) }
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
        case "code_propose": return "wand.and.stars"
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
        case "code": Button("去审阅") { store.section = .coding }.buttonStyle(.borderless).font(.caption)
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
