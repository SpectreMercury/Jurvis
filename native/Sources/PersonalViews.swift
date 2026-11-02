import SwiftUI
import UniformTypeIdentifiers
import CoreImage.CIFilterBuiltins

struct MemoryView: View {
    @EnvironmentObject var store: AppStore
    @State private var editing: Memory?
    @State private var adding = false
    var body: some View {
        Page(title: "记忆", subtitle: "启用的记忆会作为背景信息带入每次对话。你也可以在对话里说「请记住……」。", width: 860) {
            Button { adding = true } label: { Label("添加", systemImage: "plus") }.buttonStyle(.borderedProminent)
        } content: {
            if store.snapshot?.memories.isEmpty != false {
                EmptyState(icon: "brain", title: "还没有记忆", detail: "比如你的工作偏好、常用格式或长期目标。")
            }
            ForEach(store.snapshot?.memories ?? []) { memory in
                Card {
                    HStack {
                        Text(memory.title).font(.headline)
                        if memory.enabled == 0 { Badge(text: "已停用") }
                        Spacer()
                        Toggle("用于对话", isOn: Binding(get: { memory.enabled == 1 }, set: { save(memory, enabled: $0) })).labelsHidden().toggleStyle(.switch).controlSize(.small)
                    }
                    Text(memory.content).font(.callout).foregroundStyle(.secondary).textSelection(.enabled)
                    HStack(spacing: 14) {
                        Text(memory.updated_at.relativeDate).foregroundStyle(.tertiary)
                        Spacer()
                        Button("编辑") { editing = memory }
                        Button("删除", role: .destructive) { Task { await store.perform { let _: JSONValue = try await store.requireAPI().request("/api/memories/\(memory.id)", method: "DELETE") } } }
                    }.font(.caption).buttonStyle(.borderless)
                }
            }
        }
        .sheet(isPresented: $adding) { MemoryEditor(memory: nil).environmentObject(store) }
        .sheet(item: $editing) { MemoryEditor(memory: $0).environmentObject(store) }
    }
    func save(_ memory: Memory, enabled: Bool) {
        Task { await store.perform { let _: JSONValue = try await store.requireAPI().request("/api/memories/\(memory.id)", method: "PUT", body: ["title": memory.title, "content": memory.content, "enabled": enabled]) } }
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
            .formStyle(.grouped).navigationTitle(memory == nil ? "添加记忆" : "编辑记忆")
            .toolbar {
                ToolbarItem(placement: .cancellationAction) { Button("取消") { dismiss() } }
                ToolbarItem(placement: .confirmationAction) {
                    Button("保存") { Task { await save() } }.disabled(title.trimmingCharacters(in: .whitespaces).isEmpty || content.trimmingCharacters(in: .whitespaces).isEmpty)
                }
            }
            .onAppear { title = memory?.title ?? ""; content = memory?.content ?? "" }
        }
        #if os(macOS)
        .frame(width: 520, height: 380)
        #endif
    }
    func save() async {
        await store.perform {
            let api = try store.requireAPI()
            if let memory {
                let _: JSONValue = try await api.request("/api/memories/\(memory.id)", method: "PUT", body: ["title": title, "content": content, "enabled": memory.enabled == 1])
            } else {
                let _: Memory = try await api.request("/api/memories", method: "POST", body: ["title": title, "content": content])
            }
            dismiss()
        }
    }
}

struct FilesView: View {
    @EnvironmentObject var store: AppStore
    @State private var importing = false
    @State private var selected: WorkspaceContent?
    var body: some View {
        Page(title: "文件", subtitle: "助理可以阅读这里的文本，也会把你要求保存的写作结果放在这里。支持 TXT、Markdown、CSV、JSON。", width: 860) {
            Button { importing = true } label: { Label("导入", systemImage: "square.and.arrow.down") }.buttonStyle(.borderedProminent)
        } content: {
            if store.files.isEmpty { EmptyState(icon: "folder", title: "还没有文件", detail: "导入文本文件，或让助理把写好的内容保存下来。") }
            if !store.files.isEmpty {
                Card(padding: 0) {
                    VStack(spacing: 0) {
                        ForEach(Array(store.files.enumerated()), id: \.element.id) { index, file in
                            if index > 0 { Divider().padding(.leading, 44) }
                            Button { Task { await open(file) } } label: {
                                HStack(spacing: 12) {
                                    Image(systemName: "doc.text").foregroundStyle(Color.jurvisAccent).frame(width: 20)
                                    Text(file.name).foregroundStyle(.primary)
                                    Spacer()
                                    Text(file.bytes.fileSize).font(.caption).foregroundStyle(.secondary)
                                    Image(systemName: "chevron.right").font(.caption).foregroundStyle(.tertiary)
                                }.padding(.horizontal, 14).padding(.vertical, 11).contentShape(Rectangle())
                            }.buttonStyle(.plain)
                        }
                    }
                }
            }
        }
        .task { await store.reloadSettings() }
        .fileImporter(isPresented: $importing, allowedContentTypes: [.plainText, .text, .json, .commaSeparatedText]) { result in
            Task { await store.perform {
                let url = try result.get()
                let granted = url.startAccessingSecurityScopedResource(); defer { if granted { url.stopAccessingSecurityScopedResource() } }
                let _: JSONValue = try await store.requireAPI().upload("/api/files", data: Data(contentsOf: url), name: url.lastPathComponent, mime: "text/plain")
                await store.reloadSettings()
            } }
        }
        .sheet(item: $selected) { file in
            NavigationStack {
                ScrollView {
                    Group {
                        if file.name.hasSuffix(".md") { MarkdownText(text: file.content) }
                        else { Text(file.content).font(file.name.hasSuffix(".json") || file.name.hasSuffix(".csv") ? .system(.callout, design: .monospaced) : .body).textSelection(.enabled) }
                    }.frame(maxWidth: .infinity, alignment: .leading).padding(24)
                }
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
    func open(_ file: WorkspaceFile) async {
        await store.perform { selected = try await store.requireAPI().request("/api/files/" + file.name.addingPercentEncoding(withAllowedCharacters: .urlPathAllowed)!) }
    }
}

struct SettingsView:View {
    @EnvironmentObject var store:AppStore
    @State private var showingInvite=false
    var body:some View {
        Form {
            SwiftUI.Section("连接与运行") {
                LabeledContent("状态",value:store.connectionStatus)
                if let runtime=store.snapshot?.runtime {LabeledContent("Mac 内存",value:String(format:"%.1f / %.0f GB",runtime.memory_used_gib,runtime.memory_total_gib));LabeledContent("当前引擎",value:runtime.active=="llm" ? "语言模型" : runtime.active=="comfy" ? "创作引擎" : "待命")}
                Text("模型按任务切换，任务记录保存在 Mac。移动端需要与 Mac 网络互通。").font(.caption).foregroundStyle(.secondary)
            }
            SwiftUI.Section("本地模型") {
                ForEach(store.modelState?.models ?? []) {model in
                    HStack {VStack(alignment:.leading,spacing:4){Text(model.title);Text(model.verified ? "已完成真实任务验证" : model.installed ? "权重已发现 · 等待运行验证" : "未找到权重").font(.caption).foregroundStyle(.secondary)};Spacer();Image(systemName:model.verified ? "checkmark.circle.fill" : "circle.dotted").foregroundStyle(model.verified ? Color.jurvisAccent : .secondary)}
                }
                if store.modelState?.media_runtime_installed == false {Text("请在 Mac 项目目录运行 scripts/setup_media.py 安装创作引擎。").font(.caption).foregroundStyle(.orange)}
            }
            SwiftUI.Section("创作扩展") {
                ForEach(store.components) { component in
                    VStack(alignment:.leading,spacing:8) {
                        HStack { Text(component.title);Spacer();Text(component.installed ? "文件已校验" : "未安装").font(.caption).foregroundStyle(component.installed ? Color.jurvisAccent : .secondary) }
                        Text(ByteCountFormatter.string(fromByteCount:Int64(component.bytes),countStyle:.file)).font(.caption).foregroundStyle(.secondary)
                        HStack {
                            if let url=URL(string:component.source) { Link("来源与版本",destination:url) }
                            Spacer()
                            #if os(macOS)
                            if !component.installed { Button("安装 / 校验"){Task{await store.perform{_ = try await store.submit("/api/components/\(component.id)/install",body:[:])}}}.disabled(store.activeJobs.contains{$0.kind=="install" && $0.payload["component_id"].string==component.id}) }
                            #endif
                        }.font(.caption)
                        if !component.installed { Text("手动下载位置：\(component.directory)/\(component.filename)").font(.caption2).foregroundStyle(.secondary).textSelection(.enabled) }
                    }.padding(.vertical,6)
                }
                Text("文件校验确认版本一致；生成效果以实际审片为准。扩展安装在 Mac 执行。").font(.caption).foregroundStyle(.secondary)
            }
            SwiftUI.Section("设备") {
                #if os(macOS)
                Button{Task{await store.perform{store.invitation=try await store.requireAPI().request("/api/pairing",method:"POST");showingInvite=true}}}label:{Label("配对 iPhone 或 iPad",systemImage:"qrcode")}
                #endif
                ForEach(store.devices.filter{$0.revoked==0}) {device in
                    HStack{VStack(alignment:.leading,spacing:4){Text(device.name);Text("最近连接："+Date(timeIntervalSince1970:device.last_seen).formatted(date:.abbreviated,time:.shortened)).font(.caption).foregroundStyle(.secondary)};Spacer()
                        #if os(macOS)
                        Button("撤销",role:.destructive){Task{await store.perform{let _:JSONValue=try await store.requireAPI().request("/api/devices/\(device.id)",method:"DELETE");await store.reloadSettings()}}}
                        #endif
                    }
                }
                #if os(iOS)
                Button("断开并移除本机凭据",role:.destructive){store.disconnect()}
                #endif
            }
            SwiftUI.Section("关于 Jurvis") {
                LabeledContent("版本",value:store.snapshot?.version ?? "0.1.0")
                Text("原生 Apple 应用 · 本地个人助理\n连接使用 TLS 加密并校验 Mac 证书指纹。移动设备凭据保存在钥匙串中。").font(.caption).foregroundStyle(.secondary)
            }
        }.formStyle(.grouped).task{await store.reloadSettings()}
        .sheet(isPresented:$showingInvite){if let invite=store.invitation{InvitationView(invitation:invite)}}
    }
}

struct InvitationView:View {
    @Environment(\.dismiss) var dismiss
    let invitation:Invitation
    var qr:CGImage? {let f=CIFilter.qrCodeGenerator();f.message=Data(invitation.link.utf8);guard let image=f.outputImage?.transformed(by:CGAffineTransform(scaleX:8,y:8))else{return nil};return CIContext().createCGImage(image,from:image.extent)}
    var body:some View {
        NavigationStack{
            VStack(spacing:20){Text("把 Jurvis 带到手边").font(.title2.weight(.semibold));Text("在 iPhone 或 iPad 的 Jurvis 中扫描二维码。\n设备需与这台 Mac 网络互通。").font(.subheadline).foregroundStyle(.secondary).multilineTextAlignment(.center)
                if let qr{Image(decorative:qr,scale:1).interpolation(.none).resizable().scaledToFit().frame(width:250,height:250).padding(12).background(.white,in:RoundedRectangle(cornerRadius:16))}
                Text(invitation.url).font(.caption.monospaced()).textSelection(.enabled)
                Text("邀请有效期 5 分钟，仅可使用一次。请只分享给自己的设备。").font(.caption).foregroundStyle(.secondary)
                Button("复制配对链接"){copyText(invitation.link)}.buttonStyle(.borderedProminent)
            }.padding(30).navigationTitle("设备配对").toolbar{ToolbarItem(placement:.cancellationAction){Button("完成"){dismiss()}}}
        }
        #if os(macOS)
        .frame(width:480,height:570)
        #endif
    }
}

struct PairingView:View {
    @EnvironmentObject var store:AppStore
    @State private var link=""
    @State private var scanning=false
    var body:some View {
        NavigationStack{
            ScrollView{
                VStack(alignment:.leading,spacing:28){
                    BrandMark(size:64)
                    VStack(alignment:.leading,spacing:6) {
                        Text("连接你的 Mac").font(.title.weight(.semibold))
                        Text("Jurvis 的模型和数据都在 Mac 上运行。配对后，iPhone 和 iPad 就能使用助理、创作和短剧。").font(.callout).foregroundStyle(.secondary)
                    }
                    VStack(alignment:.leading,spacing:16){Label("在 Mac 上打开 Jurvis",systemImage:"1.circle");Label("进入设置，选择设备配对",systemImage:"2.circle");Label("扫描二维码，或粘贴配对链接",systemImage:"3.circle")}.font(.subheadline)
                    #if os(iOS)
                    Button{scanning=true}label:{Label("扫描配对二维码",systemImage:"qrcode.viewfinder").frame(maxWidth:.infinity).padding(8)}.buttonStyle(.borderedProminent)
                    #endif
                    TextField("jurvis://pair?…",text:$link,axis:.vertical).textFieldStyle(.roundedBorder).lineLimit(2...4).autocorrectionDisabled()
                    Button(store.connecting ? "正在连接…" : "使用配对链接连接"){Task{await store.pair(link)}}.buttonStyle(.bordered).disabled(link.isEmpty || store.connecting)
                    Label("加密连接 · 证书指纹校验 · 凭据保存在钥匙串",systemImage:"lock.shield").font(.caption).foregroundStyle(.secondary)
                }.padding(32).frame(maxWidth:560).frame(maxWidth:.infinity)
            }.background(Color.canvas)
            #if os(iOS)
            .sheet(isPresented:$scanning){ScannerView{value in scanning=false;link=value;Task{await store.pair(value)}}}
            #endif
        }
    }
}
