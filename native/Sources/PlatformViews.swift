import SwiftUI

// MARK: - Models and web search

struct ProviderInfo: Codable, Identifiable, Equatable {
    var id: String; var title: String; var base_url: String; var configured: Bool; var has_key: Bool; var web_search: Bool; var builtin: Bool
}
struct AssistantSettings: Codable, Equatable { var chat_model: String; var tool_model: String; var embed_model: String; var web_search: Bool }
struct LocalModel: Codable, Equatable { var id: String; var title: String; var installed: Bool }
struct SettingsState: Codable, Equatable { var settings: AssistantSettings; var providers: [ProviderInfo]; var local: LocalModel }
struct CloudModel: Codable, Identifiable, Hashable { var id: String; var name: String }
struct CloudModelList: Codable { var models: [CloudModel] }

extension JSONValue {
    var bool: Bool? { if case .bool(let v) = self { return v }; return nil }
}

/// Which model answers: the one on this Mac, or a cloud model through Miavo (which also brings web search).
struct AssistantModelSettings: View {
    @EnvironmentObject var store: AppStore
    @State private var state: SettingsState?
    @State private var models: [CloudModel] = []
    @State private var modelsError: String?
    @State private var key = ""

    var miavo: ProviderInfo? { state?.providers.first { $0.id == "miavo" } }

    var body: some View {
        RowGroup(title: "对话模型", footnote: footnote) {
            if let state {
                // The model's name can be long, so it gets its own line instead of squeezing the label.
                ViewThatFits(in: .horizontal) {
                    HStack(spacing: 12) { modelLabel.fixedSize(); Spacer(minLength: 8); modelPicker(state) }
                    VStack(alignment: .leading, spacing: 8) { modelLabel; modelPicker(state).padding(.leading, -10) }
                }
                .padding(.horizontal, 16).padding(.vertical, 11).frame(maxWidth: .infinity, minHeight: 52, alignment: .leading)
                VStack(alignment: .leading, spacing: 8) {
                    Text("Miavo API Key").font(.jBody)
                    Text(miavo?.has_key == true ? "已配置。重新填写可以替换。" : "填写后可以使用云端模型和联网搜索。").font(.jSmall).foregroundStyle(Color.inkSoft)
                    HStack(spacing: 8) {
                        SecureField("sk-maas-…", text: $key).textFieldStyle(.roundedBorder).autocorrectionDisabled()
                        #if os(iOS)
                            .textInputAutocapitalization(.never)
                        #endif
                        ActionButton(title: "保存", busyTitle: "正在保存", doneTitle: "已保存", icon: "key", scale: .small) { try await saveKey() }
                            .disabled(key.trimmingCharacters(in: .whitespaces).isEmpty)
                    }
                }
                .padding(.horizontal, 16).padding(.vertical, 11)
                InfoRow(title: "联网搜索", detail: "云端模型回答时查询实时信息，由 Miavo 随模型提供") {
                    Toggle("联网搜索", isOn: Binding(get: { state.settings.web_search }, set: { setSearch($0) })).labelsHidden().tint(Color.ink)
                }
            } else {
                LoadingState(text: "正在读取模型设置")
            }
        }
        .task { await load() }
    }

    var modelLabel: some View {
        VStack(alignment: .leading, spacing: 3) {
            Text("默认模型").font(.jBody)
            Text("每段对话也可以单独选择").font(.jSmall).foregroundStyle(Color.inkFaint)
        }
    }

    func modelPicker(_ state: SettingsState) -> some View {
        Picker("默认模型", selection: Binding(get: { state.settings.chat_model }, set: { choose($0) })) {
            Text(state.local.title).tag("local")
            ForEach(options(including: state.settings.chat_model)) { Text($0.name).tag($0.id) }
        }
        .labelsHidden().lineLimit(1).fixedSize()
    }

    var footnote: String {
        if let modelsError { return "读取 Miavo 模型列表没有成功：\(modelsError)" }
        return "本机模型完全离线运行。云端模型把对话内容发送给 Miavo；使用云端模型时，生图和视频渲染期间也能继续对话。"
    }

    func options(including current: String) -> [CloudModel] {
        if current == "local" || models.contains(where: { $0.id == current }) { return models }
        return [CloudModel(id: current, name: current.split(separator: ":", maxSplits: 1).last.map(String.init) ?? current)] + models
    }

    func load() async {
        guard let api = store.api else { return }
        state = try? await api.request("/api/settings")
        await loadModels()
    }

    func loadModels() async {
        guard let api = store.api, miavo?.has_key == true else { models = []; return }
        do {
            let list: CloudModelList = try await api.request("/api/providers/miavo/models")
            models = list.models; modelsError = nil
        } catch { modelsError = error.localizedDescription }
    }

    func saveKey() async throws {
        state = try await store.requireAPI().request("/api/providers/miavo", method: "PUT", body: ["api_key": key.trimmingCharacters(in: .whitespaces)])
        key = ""
        await loadModels()
    }

    func choose(_ model: String) {
        Task { await store.perform { state = try await store.requireAPI().request("/api/settings", method: "PATCH", body: ["chat_model": model]) } }
    }

    func setSearch(_ on: Bool) {
        Task { await store.perform { state = try await store.requireAPI().request("/api/settings", method: "PATCH", body: ["web_search": on]) } }
    }
}

/// Web pages a cloud model read for its answer.
struct SourceList: View {
    var sources: [JSONValue]
    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            Eyebrow(text: "来源")
            ForEach(Array(sources.enumerated()), id: \.offset) { _, source in
                if let url = URL(string: source["url"].string), ["http", "https"].contains(url.scheme ?? "") {
                    Link(destination: url) {
                        Label(source["title"].string.isEmpty ? (url.host ?? url.absoluteString) : source["title"].string, systemImage: "globe")
                            .font(.jSmall).lineLimit(1).truncationMode(.tail).foregroundStyle(Color.ink)
                    }
                }
            }
        }
    }
}

// MARK: - Smart home

struct HomeDevice: Codable, Identifiable, Equatable {
    var id: String; var connector: String; var name: String; var room: String; var type: String; var online: Bool; var state: JSONValue; var actions: [String]
    var isOn: Bool { state["on"].bool ?? false }
    var guarded: Bool { ["lock", "cover", "camera"].contains(type) }
    var summary: String {
        var parts: [String] = []
        if !online { parts.append("离线") }
        if let level = state["brightness"].number, isOn { parts.append("亮度 \(Int(level))%") }
        if let now = state["temperature"].number { parts.append("当前 \(Self.degrees(now))") }
        if let target = state["target_temperature"].number { parts.append("设定 \(Self.degrees(target))") }
        if !state["mode"].string.isEmpty { parts.append(state["mode"].string) }
        if let locked = state["locked"].bool { parts.append(locked ? "已上锁" : "未上锁") }
        if let open = state["open"].bool { parts.append(open ? "已打开" : "已关闭") }
        if !state["value"].isNull { parts.append(state["value"].preview + (state["unit"].string.isEmpty ? "" : " " + state["unit"].string)) }
        return parts.joined(separator: " · ")
    }
    static func degrees(_ value: Double) -> String { (value.rounded() == value ? String(Int(value)) : String(format: "%.1f", value)) + "°" }
}
struct HomeDeviceList: Codable { var devices: [HomeDevice]; var errors: [String: String] }

/// Devices from every connected smart-home platform, by room, with the controls each one supports.
struct HomeView: View {
    @EnvironmentObject var store: AppStore
    @State private var devices: [HomeDevice] = []
    @State private var errors: [String: String] = [:]
    @State private var loaded = false
    @State private var confirming: (device: HomeDevice, action: String)?

    var rooms: [(name: String, devices: [HomeDevice])] {
        Dictionary(grouping: devices) { $0.room.isEmpty ? "未分配房间" : $0.room }
            .map { (name: $0.key, devices: $0.value.sorted { $0.name < $1.name }) }
            .sorted { $0.name < $1.name }
    }

    var body: some View {
        Page(title: "家居", width: 760, actions: {
            ActionButton(title: "刷新", busyTitle: "正在刷新", icon: "arrow.clockwise", scale: .small) { try await load(refresh: true) }
        }) {
            VStack(alignment: .leading, spacing: 26) {
                ForEach(errors.keys.sorted(), id: \.self) { key in
                    Label("\(key)：\(errors[key] ?? "")", systemImage: "exclamationmark.triangle").font(.jSmall).foregroundStyle(Color.danger)
                }
                if !loaded { LoadingState(text: "正在读取设备") }
                else if devices.isEmpty && errors.isEmpty {
                    EmptyState(icon: "house", title: "还没有连接智能家居", detail: "在 Mac 上打开 Jurvis 网页版，进入 家居 → 连接品牌，添加家里在用的品牌或平台。连接后，也可以直接在对话里说“把客厅灯关了”。")
                }
                ForEach(rooms, id: \.name) { room in
                    RowGroup(title: room.name) {
                        ForEach(room.devices) { device in row(device) }
                    }
                }
            }
        }
        .task { try? await load(refresh: false) }
        .confirmationDialog(confirmTitle, isPresented: Binding(get: { confirming != nil }, set: { if !$0 { confirming = nil } }), titleVisibility: .visible) {
            if let pending = confirming {
                Button(Self.label(pending.action), role: pending.action == "lock" || pending.action == "close" ? nil : .destructive) {
                    Task { await send(pending.device, pending.action) }
                }
            }
            Button("取消", role: .cancel) { confirming = nil }
        }
    }

    var confirmTitle: String {
        guard let pending = confirming else { return "" }
        return "\(Self.label(pending.action))「\(pending.device.name)」？"
    }

    static func label(_ action: String) -> String {
        ["lock": "上锁", "unlock": "解锁", "open": "打开", "close": "关闭", "activate": "执行", "turn_on": "打开", "turn_off": "关闭"][action] ?? action
    }

    @ViewBuilder func row(_ device: HomeDevice) -> some View {
        VStack(alignment: .leading, spacing: 0) {
            InfoRow(title: device.name, detail: device.summary.isEmpty ? nil : device.summary) {
                HStack(spacing: 6) {
                    ForEach(device.actions.filter { ["lock", "unlock", "open", "close", "activate"].contains($0) }, id: \.self) { action in
                        Button(Self.label(action)) {
                            if device.guarded { confirming = (device, action) } else { Task { await send(device, action) } }
                        }
                        .buttonStyle(.jurvis(.secondary, .small))
                    }
                    if device.actions.contains("turn_on") && device.actions.contains("turn_off") {
                        Toggle(device.name, isOn: Binding(get: { device.isOn }, set: { on in Task { await send(device, on ? "turn_on" : "turn_off") } })).labelsHidden().tint(Color.ink)
                    }
                }
                .disabled(!device.online)
            }
            if device.actions.contains("set_brightness"), device.isOn {
                BrightnessSlider(value: device.state["brightness"].number ?? 100) { level in await send(device, "set_brightness", value: level) }
                    .padding(.horizontal, 16).padding(.bottom, 10)
            }
        }
    }

    func load(refresh: Bool) async throws {
        defer { loaded = true }
        let list: HomeDeviceList = try await store.requireAPI().request("/api/home/devices?refresh=\(refresh)")
        if list.devices != devices { devices = list.devices }
        errors = list.errors
    }

    func send(_ device: HomeDevice, _ action: String, value: Double? = nil) async {
        confirming = nil
        var body: [String: Any] = ["device_id": device.id, "action": action]
        if let value { body["value"] = value }
        let sent = await store.perform { let _: JSONValue = try await store.requireAPI().request("/api/home/devices/control", method: "POST", body: body) }
        guard sent else { return }
        // Devices report their new state a moment after the command.
        try? await Task.sleep(for: .milliseconds(1200))
        try? await load(refresh: true)
    }
}

/// Sends the level once the finger lifts, not on every step of the drag.
struct BrightnessSlider: View {
    @State var value: Double
    var commit: (Double) async -> Void
    var body: some View {
        HStack(spacing: 10) {
            Image(systemName: "sun.min").font(.jSmall).foregroundStyle(Color.inkSoft)
            Slider(value: $value, in: 1...100, step: 1) { editing in
                if !editing { Task { await commit(value) } }
            }
            .accessibilityLabel("亮度")
            Text("\(Int(value))%").font(.jSmall.monospacedDigit()).foregroundStyle(Color.inkSoft).frame(width: 40, alignment: .trailing)
        }
    }
}
