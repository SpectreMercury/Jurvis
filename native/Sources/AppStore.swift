import SwiftUI
import Foundation
import OSLog
import AVFoundation
import ImageIO

@MainActor final class AppStore: ObservableObject {
    @Published var snapshot: Snapshot?
    @Published var messages: [Message] = []
    @Published var section: Section? = .assistant
    /// The stretch of the one conversation the user is in; nil once it has gone quiet and the next message begins a new one.
    @Published var selectedSession: String?
    /// The stream is loaded from this time on; scrolling back moves it earlier.
    private var streamFrom: Double?
    /// There is more of the conversation before what is loaded.
    @Published var streamEarlier = false
    /// What is loaded stops short of the present: an old part of the conversation is open.
    @Published var streamPast = false
    /// A message to bring into view once, instead of the newest one.
    @Published var scrollTarget: String?
    /// The next message begins a new subject, whatever was being talked about.
    @Published var fresh = false
    @Published var selectedProject: String?
    /// Code project picked in the composer for what is said next; `projectPicked` tells a pick of "none" from no pick at all.
    @Published var codeProjectID: String?
    @Published var projectPicked = false
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
    /// The message being sent, shown at once while the Mac has not stored it yet.
    @Published var outgoing: Message?
    private var outgoingBase = 0
    /// A conversation was picked and its messages are on their way.
    @Published var loadingSession = false
    /// Jobs whose cancel request has been pressed but not answered yet.
    @Published var cancelling: Set<String> = []
    /// The picture or clip open in the full-window viewer.
    @Published var viewing: MediaViewing?
    private var thumbnails: [String: Image] = [:]
    private var polling: Task<Void,Never>?
    /// The character that stands for the assistant, and what it is saying to itself (JournalViews.swift).
    let companion = Companion()
    private var seenEvent = -1
    #if os(macOS)
    private var engineProcess: Process?
    #endif
    var activeJobs: [Job] { snapshot?.jobs.filter(\.isActive) ?? [] }
    /// Pictures the user made or added. Illustrations drawn for cards (kind "card") are not theirs to browse.
    var images: [Asset] { snapshot?.assets.filter { !$0.isVideo && $0.kind != "card" } ?? [] }
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
        // For UI checks in the simulator, where nobody can scan a code: `SIMCTL_CHILD_JURVIS_PAIR_LINK=… xcrun simctl launch …`.
        else if let link = Self.checkLink { await pair(link) }
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
        _ = await api?.reconnect()
        await refresh()
        await learnAddresses()
        await loadCodeProjects()
        polling?.cancel()
        polling = Task { [weak self] in
            var tick = 0
            while !Task.isCancelled {
                // Stream replies quickly; keep the full snapshot at a calmer pace.
                let streaming = await self?.chatStreaming ?? false
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
            if let outreach = state.outreach, !outreach.isEmpty { await Notifier.shared.deliver(outreach, store: self) }
            if let bookings = state.calendar, !bookings.isEmpty { Task { await CalendarSync.shared.add(bookings, store: self) } }
            if !connected { connected = true }
            if connectionStatus != "Mac 已连接" { connectionStatus = "Mac 已连接" }
            await refreshMessages()
            // Something happened on the Mac since the last look: it may have said something to itself, or its drawing moved on.
            if state.last_event != seenEvent || companion.drawing { seenEvent = state.last_event; await companion.sync(api, working: chatStreaming) }
        } catch {
            // The address in use stopped answering, as when the phone leaves the home network: try the Mac's other addresses.
            if api.candidates.count > 1, await api.reconnect(), let state: Snapshot = try? await api.request("/api/state") {
                if state != snapshot { snapshot = state }
                connected = true; connectionStatus = "Mac 已连接"
                return
            }
            if connected { connected = false }
            if connectionStatus != "连接已断开，正在重试" { connectionStatus = "连接已断开，正在重试" }
        }
    }
    /// Asks the Mac where it can be reached and remembers the answer, so Tailscale switched on later works without pairing again.
    func learnAddresses() async {
        #if os(iOS)
        guard let api, let reach: Reach = try? await api.request("/api/reach"), let updated = api.learn(reach.urls) else { return }
        try? Keychain.save(updated)
        #endif
    }
    /// How many messages are kept loaded while following the conversation; past that the view starts over from the newest.
    private static let streamLimit = 400
    private static func stamp(_ time: Double) -> String { String(format: "%.6f", time) }

    /// Brings the loaded part of the conversation up to date. While an old part is open it is left as it is.
    func refreshMessages() async {
        guard let api, !streamPast else { return }
        guard let from = streamFrom else { await loadNewest(); return }
        guard let page: StreamPage = try? await api.request("/api/stream?after=\(Self.stamp(from))&limit=\(Self.streamLimit)") else { return }
        if page.later { await loadNewest(); return }  // grown too long to keep whole: start over from the newest
        apply(page)
        // Pick up job/state changes (new images, finished jobs) when the reply completes.
        if !page.messages.contains(where: \.isLive) && chatStreaming { if let state: Snapshot = try? await api.request("/api/state"), state != snapshot { snapshot = state } }
    }
    private func apply(_ page: StreamPage) {
        if page.messages != messages { messages = page.messages }
        if page.earlier != streamEarlier { streamEarlier = page.earlier }
        if page.current != selectedSession { selectedSession = page.current }
    }
    /// The newest page: where the conversation is now.
    func loadNewest() async {
        guard let api, let page: StreamPage = try? await api.request("/api/stream") else { return }
        streamPast = false
        streamFrom = (page.messages.first?.created_at ?? Date().timeIntervalSince1970) - 0.00001
        apply(page)
    }
    /// Scrolling back: the page before what is loaded, with the view kept on what was at the top.
    func loadEarlier() async {
        guard let api, let first = messages.first, streamEarlier,
              let page: StreamPage = try? await api.request("/api/stream?before=\(Self.stamp(first.created_at))") else { return }
        streamFrom = (page.messages.first?.created_at ?? first.created_at) - 0.00001
        scrollTarget = first.id
        messages = page.messages + messages
        streamEarlier = page.earlier
    }
    /// Opens the conversation at an earlier stretch, or at one message of it (a search result).
    func open(stretch: String? = nil, message: String? = nil) async {
        guard let api else { return }
        section = .assistant; loadingSession = true; defer { loadingSession = false }
        let query = message.map { "at=\($0)" } ?? "start=\(stretch ?? "")"
        guard let page: StreamPage = try? await api.request("/api/stream?\(query)&limit=\(Self.streamLimit)") else { return }
        streamPast = page.later
        streamFrom = (page.messages.first?.created_at ?? Date().timeIntervalSince1970) - 0.00001
        scrollTarget = message ?? page.messages.first { $0.session_id == stretch }?.id ?? page.messages.first?.id
        apply(page)
    }
    func searchStream(_ text: String) async -> [StreamHit] {
        guard let api, let query = text.addingPercentEncoding(withAllowedCharacters: .alphanumerics),
              let found: StreamHits = try? await api.request("/api/stream/search?q=\(query)") else { return [] }
        return found.results
    }
    /// Show an error to the user, except cancellations caused by leaving a screen.
    func report(_ failure: Error) {
        if failure is CancellationError || (failure as? URLError)?.code == .cancelled { return }
        error = failure.localizedDescription
    }
    /// Returns whether the action succeeded, for buttons that show the outcome.
    @discardableResult func perform(_ action: () async throws -> Void) async -> Bool {
        do { try await action(); await refresh(); return true } catch { self.report(error); return false }
    }
    /// Stored messages plus the one still on its way; the outgoing one drops out once a poll has brought it back.
    var visibleMessages: [Message] {
        if let outgoing, messages.count == outgoingBase { return messages + [outgoing] }
        return messages
    }
    /// The next message begins a new subject; with a project, work in that code project.
    func newChat(project: String? = nil) async {
        fresh = true; section = .assistant
        if let project { codeProjectID = project; projectPicked = true }
        if streamPast { await loadNewest() }
    }
    var currentSession: Session? { snapshot?.sessions.first { $0.id == selectedSession } }
    /// The code project being worked in: the one just picked in the composer, else the one of the stretch the user is in.
    var activeProject: CodeProject? {
        let id = projectPicked ? codeProjectID : fresh ? nil : currentSession?.code_project_id
        return codeProjects.first { $0.id == id }
    }
    func selectSession(_ id: String) async { await open(stretch: id); await refresh() }
    /// `voice` marks a message from the voice conversation: the reply will be read aloud.
    /// `answerTo` is the waiting question this message answers: it goes back to where the question was asked.
    @discardableResult func send(_ text: String, assets: [String], mode: String? = nil, voice: Bool = false, answerTo: String? = nil) async throws -> Job {
        guard let api else { throw APIError.message("请先连接 Mac") }
        if streamPast { await loadNewest() }  // what is said now belongs at the end, not in the old part that was open
        outgoingBase = messages.count
        outgoing = Message(id: "outgoing", session_id: selectedSession ?? "", role: "user", content: text, status: "complete",
                           created_at: Date().timeIntervalSince1970, metadata: .object(["asset_ids": .array(assets.map { .string($0) })]))
        defer { outgoing = nil }
        var body: [String: Any] = ["content": text, "asset_ids": assets, "request_id": UUID().uuidString]
        if let mode { body["mode"] = mode }
        if voice { body["voice"] = true }
        if let answerTo { body["answer_to"] = answerTo } else if fresh { body["fresh"] = true }
        if projectPicked { body["code_project_id"] = codeProjectID ?? "" }
        // The engine decides which stretch of the conversation this continues, or begins a new one.
        let job: Job = try await api.request("/api/stream/messages",method:"POST",body:body)
        fresh = false; projectPicked = false
        selectedSession = job.payload["session_id"].string
        await refresh()
        return job
    }
    func submit(_ path: String, body: [String: Any]) async throws -> Job {
        guard let api else { throw APIError.message("请先连接 Mac") }
        let job: Job = try await api.request(path,method:"POST",body:body)
        await refresh(); return job
    }
    func uploadImage(_ url: URL) async throws -> Asset {
        guard let api else { throw APIError.message("请先连接 Mac") }
        let granted = url.startAccessingSecurityScopedResource(); defer { if granted { url.stopAccessingSecurityScopedResource() } }
        return try await uploadImage(Data(contentsOf:url), name:url.lastPathComponent)
    }
    /// Uploads image bytes that have no file behind them, such as a pasted screenshot.
    func uploadImage(_ data: Data, name: String) async throws -> Asset {
        guard let api else { throw APIError.message("请先连接 Mac") }
        guard data.count <= 32*1024*1024 else { throw APIError.message("图片不能超过32MB") }
        let asset: Asset = try await api.upload("/api/assets",data:data,name:name,mime:"application/octet-stream")
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
        // The Mac shrinks it, so a list of pictures does not download every file whole; that took seconds apiece away from home.
        if let data = try? await api?.thumbnailData(asset.id), let source = CGImageSourceCreateWithData(data as CFData, nil),
           let small = CGImageSourceCreateImageAtIndex(source, 0, nil) {
            let image = Image(decorative: small, scale: 1)
            thumbnails[asset.id] = image
            return image
        }
        // An engine from before it made previews: fetch the file and shrink it here.
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
    /// The whole picture for the viewer, which needs more than a thumbnail's pixels.
    func fullImage(_ asset: Asset) async -> CGImage? {
        guard let url = try? await mediaURL(asset), let source = CGImageSourceCreateWithURL(url as CFURL, nil) else { return nil }
        return CGImageSourceCreateThumbnailAtIndex(source, 0, [kCGImageSourceCreateThumbnailFromImageAlways: true, kCGImageSourceThumbnailMaxPixelSize: 16384, kCGImageSourceCreateThumbnailWithTransform: true] as CFDictionary)
    }
    func view(_ asset: Asset, among assets: [Asset] = []) {
        let set = assets.contains(asset) ? assets : [asset]
        viewing = MediaViewing(assets: set, index: set.firstIndex(of: asset) ?? 0)
    }
    func stepViewing(_ by: Int) {
        guard let current = viewing, current.assets.indices.contains(current.index + by) else { return }
        viewing?.index += by
    }
    /// The artwork that stands for a drama: its opening frame, or failing that its lead.
    func cover(of project: Project) -> Asset? {
        let shots = project.document.shots.flatMap { [$0.first_frame, $0.video_asset] }
        return (shots + project.document.characters.map(\.reference_asset)).lazy.compactMap { self.asset($0) }.first
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
            let others = (components.queryItems ?? []).filter { $0.name == "alt" }.compactMap(\.value).filter { URL(string:$0)?.scheme == "https" }
            let candidate = API(Connection(url:endpoint,token:"",fingerprint:fingerprint,others:others))
            _ = await candidate.reconnect()
            #if os(macOS)
            let name = Host.current().localizedName ?? "Mac"
            #else
            let name = UIDevice.current.name
            #endif
            let result: PairResult = try await candidate.request("/pair",method:"POST",body:["code":code,"name":name])
            let connection = Connection(url:endpoint,token:result.token,fingerprint:fingerprint,others:others.isEmpty ? nil : others)
            candidate.close()
            try Keychain.save(connection); api?.close(); api = API(connection); await beginPolling()
        } catch { self.report(error); Logger(subsystem:"app.jurvis",category:"connection").error("Pairing failed: \(error.localizedDescription,privacy:.public)") }
    }
    func disconnect() {
        polling?.cancel(); api?.close(); api = nil; Keychain.clear(); connected = false
        snapshot = nil; messages = []; streamFrom = nil; streamPast = false; selectedSession = nil; mediaCache = [:]; connectionStatus = "请与 Mac 配对"
        companion.reset(); seenEvent = -1
    }
    func job(_ id: String?) -> Job? { guard let id else { return nil }; return snapshot?.jobs.first { $0.id == id } }
    func asset(_ id: String?) -> Asset? { guard let id else { return nil }; return snapshot?.assets.first { $0.id == id } }
    func outputs(of job: Job) -> [Asset] { job.result["asset_ids"].array.compactMap { asset($0.string) } }
    /// The reply the conversation is waiting for. Background work does not count: you can keep talking while it runs.
    var chatJob: Job? { activeJobs.first { $0.kind == "chat" && !$0.isBackground } }
    /// Any reply being written, background work included: it is what keeps the messages refreshing.
    var chatStreaming: Bool { activeJobs.contains { $0.kind == "chat" } }
    func cancel(_ job: Job) async {
        cancelling.insert(job.id); defer { cancelling.remove(job.id) }
        await perform { let _: Job = try await self.requireAPI().request("/api/jobs/\(job.id)/cancel", method: "POST") }
    }
    /// Pressed, or accepted by the Mac and still winding down: a running job can take a while to stop.
    func isCancelling(_ job: Job) -> Bool { job.isActive && (cancelling.contains(job.id) || job.phase == "正在停止") }
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
    /// The user's answer to a command the assistant wants to run: "run", "deny" or "always" (allow the whole conversation).
    @discardableResult func decideCommand(message: String, call: String, _ decision: String) async -> Bool {
        await perform {
            let _: JSONValue = try await self.requireAPI().request("/api/messages/\(message)/commands/\(call)", method: "POST", body: ["decision": decision])
            await self.refreshMessages()
        }
    }
    func setAllowCommands(_ session: String, _ allow: Bool) async {
        await perform { let _: Session = try await self.requireAPI().request("/api/sessions/\(session)", method: "PATCH", body: ["allow_commands": allow]) }
    }
    /// What is waiting to be decided, except the question the conversation ends on: its options are right there under it.
    var waitingQuestions: [WaitingQuestion] { (snapshot?.questions ?? []).filter { $0.message_id != messages.last?.id } }
    /// The user does not want to decide this one: it stops waiting.
    func dropQuestion(_ id: String) async {
        await perform { let _: JSONValue = try await self.requireAPI().request("/api/questions/\(id)", method: "DELETE") }
    }
    func deleteSession(_ id: String) async {
        await perform {
            let _: JSONValue = try await self.requireAPI().request("/api/sessions/\(id)", method: "DELETE")
            if self.selectedSession == id { self.selectedSession = nil }
            await self.loadNewest()
        }
    }
    func saveProject(_ project: Project) async throws {
        guard let api else { throw APIError.message("请先连接 Mac") }
        let doc = try JSONSerialization.jsonObject(with:JSONEncoder().encode(project.document))
        let _: Project = try await api.request("/api/projects/\(project.id)",method:"PUT",body:["document":doc,"expected_updated_at":project.updated_at])
        await refresh()
    }
}

extension AppStore {
    /// A pairing link handed in at launch. Debug builds only.
    static var checkLink: String? {
        #if DEBUG
        return ProcessInfo.processInfo.environment["JURVIS_PAIR_LINK"]
        #else
        return nil
        #endif
    }
    /// The screen to open at launch for a UI check. Debug builds only.
    static var checkStart: String? {
        #if DEBUG
        return ProcessInfo.processInfo.environment["JURVIS_UI_START"]
        #else
        return nil
        #endif
    }
}
