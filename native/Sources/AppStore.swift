import SwiftUI
import Foundation
import OSLog
import AVFoundation
import ImageIO

@MainActor final class AppStore: ObservableObject {
    @Published var snapshot: Snapshot?
    @Published var messages: [Message] = []
    @Published var section: Section? = .assistant
    @Published var selectedSession: String?
    @Published var selectedProject: String?
    /// Code project the next new conversation is bound to (picked in the composer).
    @Published var codeProjectID: String?
    @Published var codeProjects: [CodeProject] = []
    @Published var error: String?
    @Published var connected = false
    @Published var connecting = false
    @Published var connectionStatus = "正在连接"
    @Published var api: API?
    @Published var modelState: ModelState?
    @Published var components: [MediaComponent] = []
    @Published var devices: [Device] = []
    @Published var files: [WorkspaceFile] = []
    @Published var invitation: Invitation?
    @Published var mediaCache: [String: URL] = [:]
    private var thumbnails: [String: Image] = [:]
    private var polling: Task<Void,Never>?
    #if os(macOS)
    private var engineProcess: Process?
    #endif
    var activeJobs: [Job] { snapshot?.jobs.filter(\.isActive) ?? [] }
    var images: [Asset] { snapshot?.assets.filter { !$0.isVideo } ?? [] }
    var chatBusy: Bool { chatJob != nil }
    func requireAPI() throws -> API {
        guard let api else {throw APIError.message("请先连接 Mac")}
        return api
    }

    func start() async {
        Logger(subsystem:"app.jurvis",category:"connection").info("Starting connection")
        guard api == nil else { return }
        connecting = true
        #if os(macOS)
        do {
            let root = Self.workspaceRoot
            let config = root.appendingPathComponent("data/security/local.json")
            if let data = try? Data(contentsOf: config), let connection = try? JSONDecoder().decode(Connection.self,from:data) {
                let candidate = API(connection)
                if let _: JSONValue = try? await candidate.request("/health") { api = candidate }
            }
            if api == nil {
                let python = root.appendingPathComponent(".venv/bin/python")
                guard FileManager.default.isExecutableFile(atPath: python.path) else { throw APIError.message("请先在项目目录运行 scripts/bootstrap.sh 安装本地引擎") }
                let process = Process(); process.executableURL = python; process.arguments = ["-m","engine"]
                process.currentDirectoryURL = root
                process.standardOutput = FileHandle.nullDevice; process.standardError = FileHandle.nullDevice
                try process.run(); engineProcess = process
                for _ in 0..<40 {
                    try await Task.sleep(for:.milliseconds(500))
                    if let data = try? Data(contentsOf:config), let connection = try? JSONDecoder().decode(Connection.self,from:data) {
                        let candidate = API(connection)
                        if let _: JSONValue = try? await candidate.request("/health") { api = candidate; break }
                    }
                    if !process.isRunning { break }
                }
            }
            guard api != nil else { throw APIError.message("本地引擎启动失败，请查看 data/logs/engine.log") }
            await beginPolling()
        } catch { self.report(error); connectionStatus = "引擎未连接" }
        #else
        if let connection = Keychain.load() { api = API(connection); await beginPolling() }
        else { connectionStatus = "请与 Mac 配对" }
        #endif
        connecting = false
    }
    #if os(macOS)
    static var workspaceRoot: URL {
        if let env = ProcessInfo.processInfo.environment["JURVIS_ROOT"] { return URL(fileURLWithPath:env) }
        if let value = Bundle.main.object(forInfoDictionaryKey:"JurvisWorkspaceRoot") as? String, !value.isEmpty { return URL(fileURLWithPath:value) }
        return URL(fileURLWithPath:FileManager.default.currentDirectoryPath)
    }
    #endif
    func beginPolling() async {
        await refresh()
        await loadCodeProjects()
        polling?.cancel()
        polling = Task { [weak self] in
            var tick = 0
            while !Task.isCancelled {
                // Stream replies quickly; keep the full snapshot at a calmer pace.
                let streaming = await self?.chatBusy ?? false
                try? await Task.sleep(for: .milliseconds(streaming ? 350 : 2000))
                if Task.isCancelled { break }
                tick += 1
                if streaming && tick % 6 != 0 { await self?.refreshMessages() } else { await self?.refresh() }
            }
        }
    }
    func loadCodeProjects() async {
        guard let api, let list: [CodeProject] = try? await api.request("/api/code/projects") else { return }
        if list != codeProjects { codeProjects = list }
    }
    func refresh() async {
        guard let api else { return }
        do {
            let state: Snapshot = try await api.request("/api/state")
            // Publishing re-renders every screen; skip it when nothing changed so typing and IME input are never disturbed.
            if state != snapshot { snapshot = state }
            if !connected { connected = true }
            if connectionStatus != "Mac 已连接" { connectionStatus = "Mac 已连接" }
            await refreshMessages()
        } catch {
            if connected { connected = false }
            if connectionStatus != "连接已断开，正在重试" { connectionStatus = "连接已断开，正在重试" }
        }
    }
    func refreshMessages() async {
        guard let api, let id = selectedSession else { return }
        if let list: [Message] = try? await api.request("/api/sessions/\(id)/messages"), selectedSession == id {
            if list != messages { messages = list }
            // Pick up job/state changes (new images, finished jobs) when the reply completes.
            if !(list.last?.isLive ?? false) && chatBusy { if let state: Snapshot = try? await api.request("/api/state"), state != snapshot { snapshot = state } }
        }
    }
    /// Show an error to the user, except cancellations caused by leaving a screen.
    func report(_ failure: Error) {
        if failure is CancellationError || (failure as? URLError)?.code == .cancelled { return }
        error = failure.localizedDescription
    }
    func perform(_ action: () async throws -> Void) async {
        do { try await action(); await refresh() } catch { self.report(error) }
    }
    /// A new conversation is created on the engine when its first message is sent.
    func newChat(project: String? = nil) async { selectedSession = nil; messages = []; codeProjectID = project; section = .assistant }
    var currentSession: Session? { snapshot?.sessions.first { $0.id == selectedSession } }
    /// The code project of the open conversation, or the one picked for the next new conversation.
    var activeProject: CodeProject? {
        let id = selectedSession == nil ? codeProjectID : currentSession?.code_project_id
        return codeProjects.first { $0.id == id }
    }
    func selectSession(_ id: String) async { selectedSession = id; messages = []; section = .assistant; await refresh() }
    func send(_ text: String, assets: [String], mode: String? = nil) async throws {
        guard let api else { throw APIError.message("请先连接 Mac") }
        if selectedSession == nil {
            var body: [String: Any] = ["title": "新对话"]
            if let project = codeProjectID { body["code_project_id"] = project }
            let session: Session = try await api.request("/api/sessions",method:"POST",body:body)
            selectedSession = session.id
        }
        var body: [String: Any] = ["content": text, "asset_ids": assets, "request_id": UUID().uuidString]
        if let mode { body["mode"] = mode }
        let _: Job = try await api.request("/api/sessions/\(selectedSession!)/messages",method:"POST",body:body)
        await refresh()
    }
    func submit(_ path: String, body: [String: Any]) async throws -> Job {
        guard let api else { throw APIError.message("请先连接 Mac") }
        let job: Job = try await api.request(path,method:"POST",body:body)
        await refresh(); return job
    }
    func uploadImage(_ url: URL) async throws -> Asset {
        guard let api else { throw APIError.message("请先连接 Mac") }
        let granted = url.startAccessingSecurityScopedResource(); defer { if granted { url.stopAccessingSecurityScopedResource() } }
        let data = try Data(contentsOf:url)
        guard data.count <= 32*1024*1024 else { throw APIError.message("图片不能超过32MB") }
        let asset: Asset = try await api.upload("/api/assets",data:data,name:url.lastPathComponent,mime:"application/octet-stream")
        await refresh(); return asset
    }
    func mediaURL(_ asset: Asset) async throws -> URL {
        if let url = mediaCache[asset.id] { return url }
        guard let api else { throw APIError.message("请先连接 Mac") }
        let data = try await api.assetData(asset.id)
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent("JurvisMedia",isDirectory:true)
        try FileManager.default.createDirectory(at:directory,withIntermediateDirectories:true)
        let url = directory.appendingPathComponent(asset.id + "." + (asset.isVideo ? "mp4" : "png"))
        try data.write(to:url,options:.atomic); mediaCache[asset.id] = url; return url
    }
    /// Decoded preview for an image, or the first frame of a video; cached per asset.
    func thumbnail(_ asset: Asset) async -> Image? {
        if let cached = thumbnails[asset.id] { return cached }
        guard let url = try? await mediaURL(asset) else { return nil }
        var cg: CGImage?
        if asset.isVideo {
            let generator = AVAssetImageGenerator(asset: AVURLAsset(url: url))
            generator.appliesPreferredTrackTransform = true
            generator.maximumSize = CGSize(width: 640, height: 640)
            cg = try? await generator.image(at: .zero).image
        } else if let source = CGImageSourceCreateWithURL(url as CFURL, nil) {
            cg = CGImageSourceCreateThumbnailAtIndex(source, 0, [kCGImageSourceCreateThumbnailFromImageAlways: true, kCGImageSourceThumbnailMaxPixelSize: 900, kCGImageSourceCreateThumbnailWithTransform: true] as CFDictionary)
        }
        guard let cg else { return nil }
        let image = Image(decorative: cg, scale: 1)
        thumbnails[asset.id] = image
        return image
    }
    func reloadSettings() async {
        guard let api else { return }
        await perform {
            self.modelState = try await api.request("/api/models")
            self.components = try await api.request("/api/components")
            self.devices = try await api.request("/api/devices")
            let listing: FileList = try await api.request("/api/files"); self.files = listing.files
        }
    }
    func pair(_ link: String) async {
        Logger(subsystem:"app.jurvis",category:"connection").info("Pairing request received")
        connecting = true; defer { connecting = false }
        do {
            guard let components = URLComponents(string:link.trimmingCharacters(in:.whitespacesAndNewlines)), components.scheme == "jurvis", components.host == "pair" else { throw APIError.message("请粘贴 Mac 上生成的完整配对链接") }
            func field(_ key: String) -> String? { components.queryItems?.first { $0.name == key }?.value }
            guard let endpoint = field("url"), URL(string:endpoint)?.scheme == "https", let code = field("code"), let fingerprint = field("fingerprint"), fingerprint.count == 64 else { throw APIError.message("配对链接不完整") }
            let candidate = API(Connection(url:endpoint,token:"",fingerprint:fingerprint))
            #if os(macOS)
            let name = Host.current().localizedName ?? "Mac"
            #else
            let name = UIDevice.current.name
            #endif
            let result: PairResult = try await candidate.request("/pair",method:"POST",body:["code":code,"name":name])
            let connection = Connection(url:endpoint,token:result.token,fingerprint:fingerprint)
            try Keychain.save(connection); api?.close(); api = API(connection); await beginPolling()
        } catch { self.report(error); Logger(subsystem:"app.jurvis",category:"connection").error("Pairing failed: \(error.localizedDescription,privacy:.public)") }
    }
    func disconnect() {
        polling?.cancel(); api?.close(); api = nil; Keychain.clear(); connected = false
        snapshot = nil; messages = []; mediaCache = [:]; connectionStatus = "请与 Mac 配对"
    }
    func job(_ id: String?) -> Job? { guard let id else { return nil }; return snapshot?.jobs.first { $0.id == id } }
    func asset(_ id: String?) -> Asset? { guard let id else { return nil }; return snapshot?.assets.first { $0.id == id } }
    func outputs(of job: Job) -> [Asset] { job.result["asset_ids"].array.compactMap { asset($0.string) } }
    var chatJob: Job? { activeJobs.first { $0.kind == "chat" && $0.payload["session_id"].string == selectedSession } }
    func cancel(_ job: Job) async {
        await perform { let _: Job = try await self.requireAPI().request("/api/jobs/\(job.id)/cancel", method: "POST") }
    }
    func retry(_ job: Job) async {
        await perform { let _: Job = try await self.requireAPI().request("/api/jobs/\(job.id)/retry", method: "POST") }
    }
    /// Retry the chat job that produced a given assistant message.
    func retryMessage(_ message: Message) async {
        guard let job = snapshot?.jobs.first(where: { $0.kind == "chat" && $0.payload["message_id"].string == message.id }) else {
            error = "找不到这条回复对应的任务"; return
        }
        await retry(job)
    }
    func deleteSession(_ id: String) async {
        await perform {
            let _: JSONValue = try await self.requireAPI().request("/api/sessions/\(id)", method: "DELETE")
            if self.selectedSession == id { self.selectedSession = nil; self.messages = [] }
        }
    }
    func saveProject(_ project: Project) async throws {
        guard let api else { throw APIError.message("请先连接 Mac") }
        let doc = try JSONSerialization.jsonObject(with:JSONEncoder().encode(project.document))
        let _: Project = try await api.request("/api/projects/\(project.id)",method:"PUT",body:["document":doc,"expected_updated_at":project.updated_at])
        await refresh()
    }
}
