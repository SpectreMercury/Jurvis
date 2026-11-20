import Foundation

struct Connection: Codable {
    var url: String
    var token: String
    var fingerprint: String
    /// Further addresses of the same Mac, such as its Tailscale address, tried when `url` does not answer.
    var others: [String]? = nil
}

enum JSONValue: Codable, Hashable {
    case string(String), number(Double), bool(Bool), object([String: JSONValue]), array([JSONValue]), null
    init(from decoder: Decoder) throws {
        let c = try decoder.singleValueContainer()
        if c.decodeNil() { self = .null }
        else if let v = try? c.decode(Bool.self) { self = .bool(v) }
        else if let v = try? c.decode(Double.self) { self = .number(v) }
        else if let v = try? c.decode(String.self) { self = .string(v) }
        else if let v = try? c.decode([String: JSONValue].self) { self = .object(v) }
        else { self = .array(try c.decode([JSONValue].self)) }
    }
    func encode(to encoder: Encoder) throws {
        var c = encoder.singleValueContainer()
        switch self {
        case .string(let v): try c.encode(v)
        case .number(let v): try c.encode(v)
        case .bool(let v): try c.encode(v)
        case .object(let v): try c.encode(v)
        case .array(let v): try c.encode(v)
        case .null: try c.encodeNil()
        }
    }
    subscript(key: String) -> JSONValue { if case .object(let o) = self { return o[key] ?? .null }; return .null }
    var string: String { if case .string(let s) = self { return s }; return "" }
    var array: [JSONValue] { if case .array(let a) = self { return a }; return [] }
    var number: Double? { if case .number(let n) = self { return n }; return nil }
    var isNull: Bool { if case .null = self { return true }; return false }
    /// Compact single-line rendering used for argument previews.
    var preview: String {
        switch self {
        case .string(let v): return v
        case .number(let v): return v.rounded() == v ? String(Int(v)) : String(v)
        case .bool(let v): return v ? "true" : "false"
        case .null: return "null"
        case .array(let a): return "[" + a.map(\.preview).joined(separator: ", ") + "]"
        case .object(let o): return o.keys.sorted().map { "\($0): \(o[$0]!.preview)" }.joined(separator: " · ")
        }
    }
}

struct Session: Codable, Identifiable, Hashable { var id: String; var title: String; var created_at: Double; var updated_at: Double; var code_project_id: String?; var allow_commands: Int? }
struct Message: Codable, Identifiable, Equatable {
    var id: String; var session_id: String; var role: String; var content: String; var status: String
    var created_at: Double; var metadata: JSONValue
    var isUser: Bool { role == "user" }
    var isLive: Bool { ["queued", "streaming"].contains(status) }
    var attachmentIDs: [String] { metadata["asset_ids"].array.map(\.string) }
    var errorText: String { metadata["error"].string }
    /// Ordered harness trace: text the model wrote, then the tools it called.
    var steps: [TraceStep] {
        guard case .array = metadata["steps"], let data = try? JSONEncoder().encode(metadata["steps"]) else { return [] }
        return (try? JSONDecoder().decode([TraceStep].self, from: data)) ?? []
    }
}
/// One line of a goal's plan, as the engine keeps it in the message metadata.
struct PlanStep: Hashable { var step: String; var done: Bool }
extension Message {
    var plan: [PlanStep] {
        metadata["plan"].array.compactMap { item in
            item["step"].string.isEmpty ? nil : PlanStep(step: item["step"].string, done: item["status"].string == "done")
        }
    }
    /// The answers offered with the question this reply ended on.
    var options: [String] { metadata["ask"]["options"].array.map(\.string).filter { !$0.isEmpty } }
    /// A tool call that is waiting for the user's yes or no.
    var awaitingCall: ToolCall? { isLive ? steps.flatMap(\.calls).first(where: \.awaiting) : nil }
}
struct TraceStep: Codable, Hashable {
    var text: String
    var calls: [ToolCall]
}
struct ToolCall: Codable, Hashable, Identifiable {
    var id: String; var name: String; var label: String; var status: String
    var summary: String?; var output: String?; var arguments: JSONValue?
    var job_id: String?; var project_id: String?; var duration: Double?
    /// What the call made or found, shown as the thing itself instead of as a tool call (engine/cards.py describes the fields).
    var card: JSONValue?
    var made: JSONValue? {
        guard let card, !card["title"].string.isEmpty else { return nil }
        return card
    }
    // run_command
    var command: String?; var reason: String?; var cwd: String?; var console: String?; var exit_code: Int?
    var isRunning: Bool { status == "running" }
    var awaiting: Bool { status == "awaiting" }
    var failed: Bool { status == "error" }
}
struct Job: Codable, Identifiable, Equatable {
    var id: String; var kind: String; var status: String; var phase: String; var progress: Double
    var payload: JSONValue; var result: JSONValue; var error: String?; var created_at: Double; var updated_at: Double
    var isActive: Bool { ["queued", "running"].contains(status) }
    /// Work the conversation does not wait for: a task started in the background, or a long reply that moved there.
    var isBackground: Bool { payload["background"] == .bool(true) }
    var title: String { ["chat":"对话", "image":"生成图片", "video":"生成视频", "rewrite":"提示词编排", "plan":"短剧分镜", "export":"成片导出", "frames":"提取首尾帧", "install":"安装创作扩展", "code":"代码建议", "code_test":"构建与测试", "memory":"整理记忆"][kind] ?? kind }
    var statusLabel: String { ["queued":"排队中", "running":"进行中", "succeeded":"已完成", "failed":"未完成", "cancelled":"已取消", "interrupted":"已中断"][status] ?? status }
}
struct Asset: Codable, Identifiable, Hashable {
    var id: String; var name: String; var path: String; var mime: String; var kind: String; var size: Int
    var created_at: Double; var job_id: String?; var metadata: JSONValue
    var isVideo: Bool { mime.hasPrefix("video/") }
}
struct MediaViewing: Equatable {
    var assets: [Asset]; var index: Int
    var current: Asset { assets[index] }
}
struct Character: Codable, Identifiable, Hashable {
    var id: String; var name: String; var description: String; var appearance: String?; var image_prompt: String
    var reference_asset: String?
}
struct Shot: Codable, Identifiable, Hashable {
    var id: String; var title: String; var description: String; var dialogue: String; var duration: Double
    var image_prompt: String; var video_prompt: String; var character_ids: [String]
    var first_frame: String?; var frame_references: [String]?; var last_frame: String?; var video_asset: String?; var versions: [String]
}
struct DramaDocument: Codable, Hashable {
    var logline: String = ""; var style: String = ""; var characters: [Character] = []; var shots: [Shot] = []
    enum CodingKeys: CodingKey { case logline, style, characters, shots }
    init() {}
    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        logline = try c.decodeIfPresent(String.self, forKey: .logline) ?? ""
        style = try c.decodeIfPresent(String.self, forKey: .style) ?? ""
        characters = try c.decodeIfPresent([Character].self, forKey: .characters) ?? []
        shots = try c.decodeIfPresent([Shot].self, forKey: .shots) ?? []
    }
}
struct Project: Codable, Identifiable, Equatable {
    var id: String; var title: String; var brief: String; var document: DramaDocument
    var created_at: Double; var updated_at: Double
}
struct Memory: Codable, Identifiable, Equatable {
    var id: String; var title: String; var content: String; var enabled: Int; var created_at: Double; var updated_at: Double
}
struct RuntimeInfo: Codable, Equatable {
    var active: String?; var memory_used_gib: Double; var memory_total_gib: Double
    /// Memory readings jitter on every poll; only a visible change counts.
    static func == (a: Self, b: Self) -> Bool {
        a.active == b.active && abs(a.memory_used_gib - b.memory_used_gib) < 1 && a.memory_total_gib == b.memory_total_gib
    }
}
struct Snapshot: Codable, Equatable {
    var version: String; var sessions: [Session]; var jobs: [Job]; var assets: [Asset]; var projects: [Project]; var memories: [Memory]
    var runtime: RuntimeInfo; var last_event: Int
}
struct ModelInfo: Codable, Identifiable { var id: String; var title: String; var installed: Bool; var verified: Bool; var active: Bool; var kind: String }
struct ModelState: Codable { var models: [ModelInfo]; var media_runtime_installed: Bool; var addresses: [String] }
struct MediaComponent: Codable, Identifiable {
    var id: String; var title: String; var kind: String; var filename: String; var directory: String
    var bytes: Int; var sha256: String; var source: String; var installed: Bool
}
struct Device: Codable, Identifiable { var id: String; var name: String; var created_at: Double; var last_seen: Double; var revoked: Int }
struct Invitation: Codable { var link: String; var url: String; var urls: [String]? = nil; var code: String; var fingerprint: String; var expires_at: Double }
struct Reach: Codable { var urls: [String] }
struct PairResult: Codable { var token: String; var device_id: String; var fingerprint: String }
struct FileList: Codable { var files: [WorkspaceFile] }
struct WorkspaceFile: Codable, Identifiable { var name: String; var bytes: Int; var id: String { name } }
struct WorkspaceContent: Codable, Identifiable { var name: String; var content: String; var id: String { name } }

enum Section: String, CaseIterable, Identifiable {
    case assistant = "对话", drama = "短剧", library = "素材", tasks = "任务", home = "家居", goals = "目标", health = "健康", ledger = "账本", memory = "记忆", files = "文件", settings = "设置"
    var id: String { rawValue }
    /// The name shown in navigation. Conversations are "任务" there, so the job queue is "后台任务".
    var title: String {
        switch self {
        case .assistant: return "新任务"
        case .tasks: return "后台任务"
        default: return rawValue
        }
    }
    var icon: String {
        switch self {
        case .assistant: return "square.and.pencil"
        case .drama: return "film.stack"
        case .library: return "photo.on.rectangle"
        case .tasks: return "list.bullet.rectangle"
        case .home: return "house"
        case .memory: return "brain"
        case .goals: return "scope"
        case .ledger: return "list.bullet.rectangle.portrait"
        case .health: return "heart.text.square"
        case .files: return "doc.text"
        case .settings: return "gearshape"
        }
    }
    /// The places of the app; the other sections are tabs inside them.
    var hub: Hub {
        switch self {
        case .assistant: return .tasks
        case .library, .drama, .files: return .library
        case .home: return .home
        case .goals, .health, .ledger: return .agenda
        case .settings, .memory, .tasks: return .settings
        }
    }
}

enum Hub: String, CaseIterable, Identifiable {
    case tasks = "任务", library = "作品库", agenda = "目标", home = "家居", settings = "设置"
    var id: String { rawValue }
    var icon: String {
        switch self {
        case .tasks: return "checklist"
        case .library: return "square.grid.2x2"
        case .agenda: return "scope"
        case .home: return "house"
        case .settings: return "gearshape"
        }
    }
    /// The section a hub opens on.
    var first: Section { sections[0] }
    var sections: [Section] {
        switch self {
        case .tasks: return [.assistant]
        case .library: return [.library, .drama, .files]
        case .home: return [.home]
        case .agenda: return [.goals, .health, .ledger]
        case .settings: return [.settings, .memory, .tasks]
        }
    }
}
