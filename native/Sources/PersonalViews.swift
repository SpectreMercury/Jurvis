import SwiftUI
import UniformTypeIdentifiers
import CoreImage.CIFilterBuiltins

struct MemoryView: View {
    @EnvironmentObject var store: AppStore
    @State private var editing: Memory?
    @State private var adding = false
    var body: some View {
        Page(title: "记忆", subtitle: "启用的记忆会作为背景信息带入每次对话。它们同时以 Markdown 保存在 Mac 上，可以用 Obsidian 打开：在那里改动一行，就是改动一条记忆。", width: 860) {
            #if os(macOS)
            if let vault = store.snapshot?.vault {
                Button { NSWorkspace.shared.open(URL(fileURLWithPath: vault)) } label: { Label("打开笔记文件夹", systemImage: "folder") }.buttonStyle(.jurvis(.secondary))
            }
            #endif
            Button { adding = true } label: { Label("添加", systemImage: "plus") }.buttonStyle(.jurvis(.primary))
        } content: {
            if store.snapshot == nil {
                LoadingState(text: "正在载入记忆")
            } else if store.snapshot?.memories.isEmpty == true {
                EmptyState(icon: "brain", title: "还没有记忆", detail: "比如你的工作偏好、常用格式或长期目标。")
            }
            ForEach(store.snapshot?.memories ?? []) { memory in
                MemoryCard(memory: memory) { editing = memory }
            }
        }
        .sheet(isPresented: $adding) { MemoryEditor(memory: nil).environmentObject(store) }
        .sheet(item: $editing) { MemoryEditor(memory: $0).environmentObject(store) }
    }
}

struct MemoryCard: View {
    @EnvironmentObject var store: AppStore
    var memory: Memory
    var edit: () -> Void
    /// The switch moves on the click; this holds its position until the Mac has answered.
    @State private var pendingEnabled: Bool?

    var enabled: Bool { pendingEnabled ?? (memory.enabled == 1) }

    var body: some View {
        Card {
            HStack(spacing: 8) {
                Text(memory.title).font(.jHeading).lineLimit(1)
                if !enabled { Badge(text: "已停用") }
                Spacer()
                if pendingEnabled != nil { BusyRing() }
                Toggle("用于对话", isOn: Binding(get: { enabled }, set: { save(enabled: $0) }))
                    .labelsHidden().toggleStyle(.switch).controlSize(.small).disabled(pendingEnabled != nil)
            }
            Text(memory.content).font(.jCallout).foregroundStyle(Color.inkSoft).textSelection(.enabled)
            HStack(spacing: 6) {
                Text(memory.updated_at.relativeDate).font(.jSmall).foregroundStyle(Color.inkFaint)
                Spacer()
                Button(action: edit) { Label("编辑", systemImage: "pencil") }.buttonStyle(.jurvis(.ghost, .small))
                ActionButton(title: "删除", busyTitle: "正在删除", icon: "trash", kind: .danger, scale: .small) {
                    let _: JSONValue = try await store.requireAPI().request("/api/memories/\(memory.id)", method: "DELETE")
                }
            }
        }
    }

    func save(enabled: Bool) {
        pendingEnabled = enabled
        Task {
            await store.perform { let _: JSONValue = try await store.requireAPI().request("/api/memories/\(memory.id)", method: "PUT", body: ["title": memory.title, "content": memory.content, "enabled": enabled]) }
            pendingEnabled = nil
        }
    }
}

struct MemoryEditor: View {
    @EnvironmentObject var store: AppStore
    @Environment(\.dismiss) var dismiss
    var memory: Memory?
    @State private var title = ""
    @State private var content = ""
    var body: some View {
        NavigationStack {
            Form {
                TextField("标题", text: $title)
                SwiftUI.Section("内容") { TextEditor(text: $content).frame(minHeight: 140) }
            }
            .sheetForm()
            .navigationTitle(memory == nil ? "添加记忆" : "编辑记忆")
            .safeAreaInset(edge: .bottom, spacing: 0) {
                SheetFooter {
                    Button("取消") { dismiss() }.buttonStyle(.jurvis(.ghost))
                    ActionButton(title: "保存", busyTitle: "正在保存", kind: .primary) { try await save() }
                        .disabled(title.trimmingCharacters(in: .whitespaces).isEmpty || content.trimmingCharacters(in: .whitespaces).isEmpty)
                }
            }
            .onAppear { title = memory?.title ?? ""; content = memory?.content ?? "" }
        }
        #if os(macOS)
        .frame(width: 520, height: 400)
        #endif
    }
    func save() async throws {
        let api = try store.requireAPI()
        if let memory {
            let _: JSONValue = try await api.request("/api/memories/\(memory.id)", method: "PUT", body: ["title": title, "content": content, "enabled": memory.enabled == 1])
        } else {
            let _: Memory = try await api.request("/api/memories", method: "POST", body: ["title": title, "content": content])
        }
        dismiss()
    }
}

extension View {
    /// Native form rows on the app's paper, for editing sheets.
    func sheetForm() -> some View {
        formStyle(.grouped).scrollContentBackground(.hidden).background(Color.canvas)
    }
}

struct FilesView: View {
    @EnvironmentObject var store: AppStore
    @State private var importing = false
    @State private var uploading = false
    @State private var opening: String?
    @State private var loaded = false
    @State private var selected: WorkspaceContent?
    var body: some View {
        Page(title: "文件", width: 860) {
            if !store.files.isEmpty {
                Button { importing = true } label: { ButtonFace(title: uploading ? "正在导入" : "导入", icon: "plus", busy: uploading) }
                    .buttonStyle(.jurvis(.secondary, .small)).disabled(uploading).accessibilityLabel("导入文件")
            }
        } content: {
            if !loaded && store.files.isEmpty {
                LoadingState(text: "正在载入文件")
            } else if store.files.isEmpty {
                WelcomeCard(icon: "doc.text", title: "还没有文件", detail: "Jurvis 写好的文稿和你导入的文本都放在这里，它在任务里可以直接读取。",
                            hint: "支持 TXT、Markdown、CSV、JSON") {
                    Button { importing = true } label: { ButtonFace(title: uploading ? "正在导入" : "导入文件", icon: "square.and.arrow.down", busy: uploading) }
                        .buttonStyle(.jurvis(.primary)).disabled(uploading)
                }
            } else {
                HStack {
                    Eyebrow(text: "\(store.files.count) 个文件")
                    Spacer()
                    Eyebrow(text: store.files.reduce(0) { $0 + $1.bytes }.fileSize)
                }
                .padding(.horizontal, 4).padding(.bottom, -8)
                Card(padding: 0) {
                    VStack(spacing: 0) {
                        ForEach(Array(store.files.enumerated()), id: \.element.id) { index, file in
                            if index > 0 { Rectangle().fill(Color.hairline).frame(height: 1).padding(.leading, 60) }
                            Button { Task { await open(file) } } label: {
                                HStack(spacing: 12) {
                                    Image(systemName: Self.icon(file.name)).font(.system(size: 15, weight: .light)).foregroundStyle(Color.ink)
                                        .frame(width: 34, height: 34).background(Color.bubble, in: RoundedRectangle(cornerRadius: 8, style: .continuous))
                                    VStack(alignment: .leading, spacing: 2) {
                                        Text(file.name).font(.jBody).foregroundStyle(Color.ink).lineLimit(1).truncationMode(.middle)
                                        Text(Self.kind(file.name) + " · " + file.bytes.fileSize).font(.jMicro).foregroundStyle(Color.inkFaint).lineLimit(1)
                                    }
                                    Spacer(minLength: 8)
                                    if opening == file.name { BusyRing() }
                                    else { Image(systemName: "chevron.right").font(.system(size: 12)).foregroundStyle(Color.inkFaint) }
                                }
                                .padding(.horizontal, 14).frame(minHeight: 60).contentShape(Rectangle())
                            }
                            .buttonStyle(PressStyle()).disabled(opening != nil)
                        }
                    }
                }
            }
        }
        .task { await store.reloadSettings(); loaded = true }
        .fileImporter(isPresented: $importing, allowedContentTypes: [.plainText, .text, .json, .commaSeparatedText]) { result in
            uploading = true
            Task {
                await store.perform {
                    let url = try result.get()
                    let granted = url.startAccessingSecurityScopedResource(); defer { if granted { url.stopAccessingSecurityScopedResource() } }
                    let _: JSONValue = try await store.requireAPI().upload("/api/files", data: Data(contentsOf: url), name: url.lastPathComponent, mime: "text/plain")
                    await store.reloadSettings()
                }
                uploading = false
            }
        }
        .sheet(item: $selected) { file in
            NavigationStack {
                ScrollView {
                    Group {
                        if file.name.hasSuffix(".md") { MarkdownText(text: file.content) }
                        else { Text(file.content).font(file.name.hasSuffix(".json") || file.name.hasSuffix(".csv") ? .jMono : .jBody).textSelection(.enabled) }
                    }.frame(maxWidth: .infinity, alignment: .leading).padding(24)
                }
                .background(Color.canvas)
                .navigationTitle(file.name)
                .toolbar {
                    ToolbarItem(placement: .cancellationAction) { Button("完成") { selected = nil } }
                    ToolbarItem(placement: .primaryAction) { ShareLink(item: file.content) { Image(systemName: "square.and.arrow.up") } }
                }
            }
            #if os(macOS)
            .frame(width: 680, height: 620)
            #endif
        }
    }
    static func icon(_ name: String) -> String {
        switch (name as NSString).pathExtension.lowercased() {
        case "csv": return "tablecells"
        case "json": return "curlybraces"
        case "md": return "text.document"
        default: return "doc.text"
        }
    }
    static func kind(_ name: String) -> String {
        switch (name as NSString).pathExtension.lowercased() {
        case "csv": return "表格"
        case "json": return "JSON"
        case "md": return "Markdown"
        case "txt": return "文本"
        default: return "文件"
        }
    }
    func open(_ file: WorkspaceFile) async {
        opening = file.name; defer { opening = nil }
        await store.perform { selected = try await store.requireAPI().request("/api/files/" + file.name.addingPercentEncoding(withAllowedCharacters: .urlPathAllowed)!) }
    }
}

struct SettingsView: View {
    @EnvironmentObject var store: AppStore
    @State private var showingInvite = false
    @State private var loaded = false

    var body: some View {
        Page(title: "设置", width: 760) {
            VStack(alignment: .leading, spacing: 26) {
                connection
                AssistantModelSettings()
                VoiceSettings()
                HealthConnectors()
                MailSettings()
                CareSettings()
                ReachSettings()
                PhoneSettings()
                models
                extensions
                devices
                RowGroup(title: "关于 Jurvis", footnote: "原生 Apple 应用 · 本地个人助理。连接使用 TLS 加密并校验 Mac 证书指纹，移动设备凭据保存在钥匙串中。") {
                    InfoRow(title: "版本", value: store.snapshot?.version ?? "0.1.0")
                }
            }
        }
        .task { await store.reloadSettings(); loaded = true }
        .sheet(isPresented: $showingInvite) { if let invite = store.invitation { InvitationView(invitation: invite) } }
    }

    var connection: some View {
        RowGroup(title: "连接与运行", footnote: "模型按任务切换，任务记录保存在 Mac。移动端需要与 Mac 网络互通。") {
            InfoRow(title: "状态", value: store.connectionStatus)
            if let runtime = store.snapshot?.runtime {
                InfoRow(title: "Mac 内存", value: String(format: "%.1f / %.0f GB", runtime.memory_used_gib, runtime.memory_total_gib))
                InfoRow(title: "当前引擎", value: runtime.active == "llm" ? "语言模型" : runtime.active == "comfy" ? "创作引擎" : "待命")
            }
        }
    }

    @ViewBuilder var models: some View {
        RowGroup(title: "本地模型", footnote: store.modelState?.media_runtime_installed == false ? "请在 Mac 项目目录运行 scripts/setup_media.py 安装创作引擎。" : nil) {
            if !loaded && store.modelState == nil { LoadingState(text: "正在检查模型") }
            ForEach(store.modelState?.models ?? []) { model in
                InfoRow(title: model.title, detail: model.verified ? "已完成真实任务验证" : model.installed ? "权重已发现 · 等待运行验证" : "未找到权重") {
                    if model.verified { StatusMark(state: .done, size: 16) } else { StatusMark(state: .pending, size: 16) }
                }
            }
        }
    }

    var extensions: some View {
        RowGroup(title: "创作扩展", footnote: "文件校验确认版本一致；生成效果以实际审片为准。扩展安装在 Mac 执行。") {
            if !loaded && store.components.isEmpty { LoadingState(text: "正在检查扩展") }
            ForEach(store.components) { component in
                let installing = store.activeJobs.first { $0.kind == "install" && $0.payload["component_id"].string == component.id }
                VStack(alignment: .leading, spacing: 0) {
                    InfoRow(title: component.title, detail: ByteCountFormatter.string(fromByteCount: Int64(component.bytes), countStyle: .file) + " · " + (component.installed ? "文件已校验" : installing?.phase ?? "未安装")) {
                        if let url = URL(string: component.source) {
                            Link(destination: url) { Label("来源", systemImage: "arrow.up.right") }.buttonStyle(.jurvis(.ghost, .small))
                        }
                        #if os(macOS)
                        if !component.installed {
                            ActionButton(title: "安装 / 校验", busyTitle: "安装中", icon: "arrow.down.circle", scale: .small, isBusy: installing != nil) {
                                _ = try await store.submit("/api/components/\(component.id)/install", body: [:])
                            }
                        }
                        #endif
                    }
                    if !component.installed {
                        Text("手动下载位置：\(component.directory)/\(component.filename)").font(.jMicro).foregroundStyle(Color.inkFaint).textSelection(.enabled)
                            .padding(.horizontal, 16).padding(.bottom, 10)
                    }
                }
            }
        }
    }

    var devices: some View {
        RowGroup(title: "设备") {
            #if os(macOS)
            InfoRow(title: "配对 iPhone 或 iPad", detail: "生成一次性二维码，5 分钟内有效") {
                ActionButton(title: "创建邀请", busyTitle: "正在创建", icon: "qrcode", scale: .small) {
                    store.invitation = try await store.requireAPI().request("/api/pairing", method: "POST"); showingInvite = true
                }
            }
            #endif
            ForEach(store.devices.filter { $0.revoked == 0 }) { device in
                InfoRow(title: device.name, detail: "最近连接：" + Date(timeIntervalSince1970: device.last_seen).formatted(date: .abbreviated, time: .shortened)) {
                    #if os(macOS)
                    ActionButton(title: "撤销", busyTitle: "正在撤销", icon: "xmark", kind: .danger, scale: .small) {
                        let _: JSONValue = try await store.requireAPI().request("/api/devices/\(device.id)", method: "DELETE")
                        await store.reloadSettings()
                    }
                    #endif
                }
            }
            #if os(iOS)
            InfoRow(title: "断开连接", detail: "移除本机保存的凭据") {
                Button { store.disconnect() } label: { Label("断开", systemImage: "xmark") }.buttonStyle(.jurvis(.danger, .small))
            }
            #endif
        }
    }
}

struct InvitationView: View {
    @Environment(\.dismiss) var dismiss
    let invitation: Invitation
    var qr: CGImage? {
        let filter = CIFilter.qrCodeGenerator()
        filter.message = Data(invitation.link.utf8)
        guard let image = filter.outputImage?.transformed(by: CGAffineTransform(scaleX: 8, y: 8)) else { return nil }
        return CIContext().createCGImage(image, from: image.extent)
    }
    var body: some View {
        NavigationStack {
            VStack(spacing: 18) {
                Text("把 Jurvis 带到手边").font(.jSection)
                Text("在 iPhone 或 iPad 的 Jurvis 中扫描二维码。\n设备需与这台 Mac 网络互通。").font(.jCallout).foregroundStyle(Color.inkSoft).multilineTextAlignment(.center)
                if let qr {
                    Image(decorative: qr, scale: 1).interpolation(.none).resizable().scaledToFit().frame(width: 240, height: 240)
                        .padding(12).background(.white, in: RoundedRectangle(cornerRadius: Metrics.radius, style: .continuous))
                }
                Text(invitation.url).font(.jMono).foregroundStyle(Color.inkSoft).textSelection(.enabled)
                if (invitation.urls ?? []).contains(where: { URL(string: $0)?.host.map { !API.isIPAddress($0) } ?? false }) {
                    Label("已包含这台 Mac 的公网地址，出门后手机无需安装 Tailscale 也能连接", systemImage: "globe").font(.jSmall).foregroundStyle(Color.signal)
                } else if (invitation.urls ?? []).contains(where: { URL(string: $0)?.host.map(API.isTailscale) ?? false }) {
                    Label("已包含这台 Mac 的 Tailscale 地址，出门后也能连接", systemImage: "network").font(.jSmall).foregroundStyle(Color.signal)
                } else {
                    Text("想在外面使用：在 Mac 和手机上登录同一个 Tailscale 账号，再重新生成邀请。").font(.jSmall).foregroundStyle(Color.inkSoft).multilineTextAlignment(.center)
                }
                Text("邀请有效期 5 分钟，仅可使用一次。请只分享给自己的设备。").font(.jSmall).foregroundStyle(Color.inkSoft)
                CopyButton(text: invitation.link, title: "复制配对链接")
            }
            .padding(30).frame(maxWidth: .infinity, maxHeight: .infinity).background(Color.canvas)
            .navigationTitle("设备配对")
            .toolbar { ToolbarItem(placement: .cancellationAction) { Button("完成") { dismiss() } } }
        }
        #if os(macOS)
        .frame(width: 480, height: 570)
        #endif
    }
}

struct PairingView: View {
    @EnvironmentObject var store: AppStore
    @State private var link = ""
    @State private var scanning = false
    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 26) {
                    BrandMark(size: 60)
                    VStack(alignment: .leading, spacing: 6) {
                        Text("连接你的 Mac").font(.jDisplay)
                        Text("Jurvis 的模型和数据都在 Mac 上运行。配对后，iPhone 和 iPad 就能使用助理、创作和短剧。").font(.jCallout).foregroundStyle(Color.inkSoft)
                    }
                    VStack(alignment: .leading, spacing: 14) {
                        Label("在 Mac 上打开 Jurvis", systemImage: "1.circle")
                        Label("进入设置，选择设备配对", systemImage: "2.circle")
                        Label("扫描二维码，或粘贴配对链接", systemImage: "3.circle")
                    }
                    .font(.jCallout)
                    #if os(iOS)
                    Button { scanning = true } label: { ButtonFace(title: store.connecting ? "正在连接" : "扫描配对二维码", icon: "qrcode.viewfinder", busy: store.connecting) }
                        .buttonStyle(.jurvis(.primary, .large, fill: true)).disabled(store.connecting)
                    #endif
                    TextField("jurvis://pair?…", text: $link, axis: .vertical)
                        .textFieldStyle(.plain).font(.jMono).lineLimit(2...4).autocorrectionDisabled()
                        .padding(12).surface(radius: Metrics.smallRadius, edge: .stroke)
                    ActionButton(title: "使用配对链接连接", busyTitle: "正在连接", icon: "link", scale: .large, fill: true, isBusy: store.connecting) { await store.pair(link) }
                        .disabled(link.isEmpty)
                    Label("加密连接 · 证书指纹校验 · 凭据保存在钥匙串", systemImage: "lock.shield").font(.jSmall).foregroundStyle(Color.inkSoft)
                }
                .padding(32).frame(maxWidth: 560).frame(maxWidth: .infinity)
            }
            .background(Color.canvas)
            #if os(iOS)
            .sheet(isPresented: $scanning) { ScannerView { value in scanning = false; link = value; Task { await store.pair(value) } } }
            #endif
        }
    }
}
