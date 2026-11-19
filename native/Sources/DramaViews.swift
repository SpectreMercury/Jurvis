import SwiftUI

struct DramaView: View {
    @EnvironmentObject var store: AppStore
    @State private var creating = false
    @State private var title = ""
    @State private var brief = ""
    var body: some View {
        Page(title: "短剧") {
            if store.snapshot?.projects.isEmpty == false {
                Button { creating = true } label: { ButtonFace(title: "新建", icon: "plus") }
                    .buttonStyle(.jurvis(.secondary, .small)).accessibilityLabel("新建短剧")
            }
        } content: {
            if store.snapshot == nil {
                LoadingState(text: "正在载入短剧")
            } else if store.snapshot?.projects.isEmpty == true {
                WelcomeCard(icon: "film.stack", title: "还没有短剧", detail: "写下故事梗概，Jurvis 准备角色和分镜，你审阅修改后再逐镜生成。",
                            hint: "定妆 → 首帧 → 视频 → 导出") {
                    Button { creating = true } label: { ButtonFace(title: "新建短剧", icon: "plus") }.buttonStyle(.jurvis(.primary))
                }
            }
            LazyVGrid(columns: PosterTile<EmptyView>.columns, alignment: .leading, spacing: 24) {
                ForEach(store.snapshot?.projects ?? []) { project in
                    NavigationLink { ProjectDetailView(projectID: project.id) } label: { poster(project) }.buttonStyle(PressStyle())
                }
            }
        }
        .sheet(isPresented: $creating) {
            NavigationStack {
                Form {
                    TextField("短剧名称", text: $title)
                    SwiftUI.Section("故事梗概") {
                        TextEditor(text: $brief).frame(minHeight: 180)
                        Text("可以包含总时长、人物、风格、关键对白和结局。默认生成竖屏短剧计划。").font(.jSmall).foregroundStyle(Color.inkSoft)
                    }
                }
                .sheetForm().navigationTitle("新建短剧")
                .safeAreaInset(edge: .bottom, spacing: 0) {
                    SheetFooter {
                        Button("取消") { creating = false }.buttonStyle(.jurvis(.ghost))
                        ActionButton(title: "生成分镜", busyTitle: "正在提交", kind: .primary) { try await create() }.disabled(title.isEmpty || brief.isEmpty)
                    }
                }
            }
            .environmentObject(store)
            #if os(macOS)
            .frame(width: 560, height: 460)
            #endif
        }
    }

    func poster(_ project: Project) -> some View {
        let shots = project.document.shots
        let finished = shots.filter { $0.video_asset != nil }.count
        return VStack(alignment: .leading, spacing: 5) {
            Group {
                if let cover = store.cover(of: project) {
                    MediaThumb(asset: cover)
                } else {
                    Color.stage.overlay {
                        Text(project.title).font(.jSection).foregroundStyle(.white).multilineTextAlignment(.center).padding(16)
                    }
                }
            }
            .aspectRatio(PosterTile<EmptyView>.ratio, contentMode: .fit)
            .clipShape(RoundedRectangle(cornerRadius: 10, style: .continuous))
            .overlay(alignment: .bottomLeading) {
                // Where the production stands, on the poster itself.
                if !shots.isEmpty {
                    Text(finished == shots.count ? "已完成" : project.stages[project.currentStage].title)
                        .font(.jMicro).foregroundStyle(.white).lineLimit(1)
                        .padding(.horizontal, 8).frame(height: 22).background(.black.opacity(0.6), in: Capsule()).padding(8)
                }
            }
            Text(project.title).font(.jBody).foregroundStyle(Color.ink).lineLimit(1).padding(.top, 5)
            Text(project.document.logline.isEmpty ? project.brief : project.document.logline).font(.jSmall).foregroundStyle(Color.inkSoft).lineLimit(2, reservesSpace: true)
            Text(shots.isEmpty ? project.updated_at.relativeDate : "\(finished)/\(shots.count) 镜头 · \(project.updated_at.relativeDate)")
                .font(.jMicro).foregroundStyle(Color.inkFaint).lineLimit(1)
        }
        .multilineTextAlignment(.leading)
    }

    func create() async throws {
        let _: Project = try await store.requireAPI().request("/api/projects", method: "POST", body: ["title": title, "brief": brief])
        creating = false; title = ""; brief = ""
    }
}

/// A vertical picture on the drama pages. It shows `showing` and opens `asset` in the viewer.
struct PosterTile<Overlay: View>: View {
    static var ratio: CGFloat { 9 / 16 }
    #if os(macOS)
    static var columns: [GridItem] { [GridItem(.adaptive(minimum: 176), spacing: 16, alignment: .top)] }
    #else
    static var columns: [GridItem] { [GridItem(.adaptive(minimum: 140), spacing: 12, alignment: .top)] }
    #endif

    @EnvironmentObject var store: AppStore
    var asset: Asset
    var showing: Asset? = nil
    var among: [Asset] = []
    @ViewBuilder var overlay: () -> Overlay
    @State private var hovering = false

    var body: some View {
        Button { store.view(asset, among: among) } label: {
            MediaThumb(asset: showing ?? asset)
                .aspectRatio(Self.ratio, contentMode: .fit)
                .overlay { overlay().clipShape(RoundedRectangle(cornerRadius: 10, style: .continuous)).allowsHitTesting(false) }
                .overlay(alignment: .topTrailing) {
                    if hovering {
                        Image(systemName: "arrow.up.left.and.arrow.down.right").font(.system(size: 10, weight: .medium)).foregroundStyle(.white)
                            .frame(width: 24, height: 24).background(.black.opacity(0.55), in: Circle()).padding(6)
                    }
                }
        }
        .buttonStyle(PressStyle())
        .onHover { hovering = $0 }
        .accessibilityLabel("查看 \(asset.name)")
        #if os(macOS)
        .pointerStyle(.zoomIn)
        #endif
    }
}

extension PosterTile where Overlay == EmptyView {
    init(asset: Asset, among: [Asset] = []) {
        self.init(asset: asset, among: among) { EmptyView() }
    }
}

extension Project {
    /// The fixed production order, so it is always clear which stage comes next.
    var stages: [(title: String, done: Bool)] {
        let shots = document.shots
        let finished = shots.filter { $0.video_asset != nil }.count
        return [("定妆", document.characters.allSatisfy { $0.reference_asset != nil }),
                ("首帧", shots.allSatisfy { $0.first_frame != nil || $0.video_asset != nil }),
                ("视频 \(finished)/\(shots.count)", finished == shots.count),
                ("导出", false)]
    }
    var currentStage: Int { stages.firstIndex { !$0.done } ?? stages.count - 1 }
}

struct ProductionSteps: View {
    var project: Project
    var active: Bool

    var body: some View {
        let stages = project.stages
        let current = project.currentStage
        HStack(spacing: 10) {
            ForEach(Array(stages.enumerated()), id: \.offset) { index, stage in
                if index > 0 { Rectangle().fill(Color.stroke).frame(width: 22, height: 1) }
                HStack(spacing: 7) {
                    ZStack {
                        Circle().fill(stage.done ? Color.signal : index == current && active ? .signalSoft : .clear)
                        Circle().strokeBorder(stage.done ? .clear : index == current && active ? Color.signal : Color.ring, lineWidth: 1.5)
                        if stage.done { Image(systemName: "checkmark").font(.system(size: 9, weight: .medium)).foregroundStyle(Color.inkReversed) }
                        else { Text("\(index + 1)").font(.system(size: 11)).foregroundStyle(index == current && active ? Color.signal : .inkSoft) }
                    }
                    .frame(width: 20, height: 20)
                    Text(stage.title).font(index == current ? .jSmall.weight(.medium) : .jSmall).foregroundStyle(stage.done || index == current ? Color.ink : .inkSoft).lineLimit(1).fixedSize()
                }
            }
        }
        .accessibilityElement(children: .combine)
    }
}

struct ProjectDetailView: View {
    @EnvironmentObject var store: AppStore
    @Environment(\.colorScheme) private var colorScheme
    let projectID: String
    @State private var draft: Project?
    @State private var editingShot: Shot?
    @State private var editingCharacter: Character?
    @State private var dirty = false
    @State private var generation = GenerationSettings()
    @State private var configuring = false
    @State private var heroWidth: CGFloat = 900
    /// Shots whose re-generation was picked from a menu and is on its way to the Mac.
    @State private var starting: Set<String> = []
    var current: Project? { store.snapshot?.projects.first { $0.id == projectID } }
    var relatedJobs: [Job] { store.activeJobs.filter { $0.payload["project_id"].string == projectID } }
    var films: [Asset] { (store.snapshot?.assets ?? []).filter { $0.kind == "film" && $0.metadata["project_id"].string == projectID } }
    func jobs(kind: String) -> [Job] { relatedJobs.filter { $0.kind == kind } }
    var body: some View {
        ScrollView {
            if let project = draft {
                VStack(alignment: .leading, spacing: 28) {
                    hero(project)
                    if dirty {
                        HStack(spacing: 8) {
                            Label("有未保存的修改", systemImage: "pencil.circle").font(.jCallout)
                            Spacer()
                            Button("放弃") { draft = current; dirty = false }.buttonStyle(.jurvis(.ghost, .small))
                            ActionButton(title: "保存", busyTitle: "正在保存", kind: .primary, scale: .small) { try await save() }
                        }
                        .padding(12).background(Color.bubble, in: RoundedRectangle(cornerRadius: Metrics.smallRadius, style: .continuous))
                    }
                    ForEach(relatedJobs.filter { $0.payload["shot_id"].isNull }) { JobCard(job: $0) }
                    if project.document.shots.isEmpty {
                        EmptyState(icon: "text.book.closed", title: relatedJobs.isEmpty ? "还没有分镜" : "正在编写角色与分镜", detail: "计划完成后会显示在这里，你可以先去做别的事。")
                        if relatedJobs.isEmpty {
                            ActionButton(title: "生成分镜计划", busyTitle: "正在提交", kind: .primary) { _ = try await store.submit("/api/projects/\(projectID)/plan", body: [:]) }
                        }
                    } else {
                        filmSection(project)
                        if project.document.characters.isEmpty { Hint(text: "这部短剧没有角色设定，镜头将只按画面描述生成。") } else { characterSection(project) }
                        shotSection(project)
                    }
                }
                .padding(Metrics.pagePadding).frame(maxWidth: Metrics.wide, alignment: .leading).frame(maxWidth: .infinity)
            } else { LoadingState(text: "正在载入短剧") }
        }
        .background(Color.canvas).navigationTitle(draft?.title ?? "短剧")
        .task { draft = current }
        .onChange(of: current?.updated_at) { _, _ in if !dirty && editingShot == nil && editingCharacter == nil { draft = current } }
        .sheet(item: $editingShot) { shot in ShotEditor(shot: shot) { updated in if let index = draft?.document.shots.firstIndex(where: { $0.id == updated.id }) { draft?.document.shots[index] = updated; dirty = true } }.environmentObject(store) }
        .sheet(item: $editingCharacter) { character in CharacterEditor(character: character) { updated in if let index = draft?.document.characters.firstIndex(where: { $0.id == updated.id }) { draft?.document.characters[index] = updated; dirty = true } }.environmentObject(store) }
    }

    /// Everything worth reviewing in this drama, in viewing order, so the viewer can step through it.
    func reviewSet(_ project: Project) -> [Asset] {
        let cast = project.document.characters.compactMap { store.asset($0.reference_asset) }
        return cast + project.document.shots.compactMap { store.asset($0.video_asset ?? $0.first_frame) } + films
    }

    /// The title card: the poster, the logline and where production stands.
    func hero(_ project: Project) -> some View {
        let cover = store.cover(of: project)
        let shots = project.document.shots
        return HStack(alignment: .top, spacing: 24) {
            if heroWidth > 600, let cover {
                PosterTile(asset: cover, among: reviewSet(project)).frame(width: 148)
            }
            VStack(alignment: .leading, spacing: 10) {
                Text(project.title).font(.jTitle)
                Text(project.document.logline.isEmpty ? project.brief : project.document.logline).font(.jBody).foregroundStyle(Color.inkSoft).lineSpacing(3).textSelection(.enabled).fixedSize(horizontal: false, vertical: true)
                HStack(spacing: 6) {
                    Badge(text: "9:16 竖屏")
                    if !project.document.style.isEmpty { Badge(text: project.document.style) }
                    if !shots.isEmpty { Badge(text: "\(shots.count) 镜 · 约 \(Int(shots.reduce(0) { $0 + $1.duration })) 秒") }
                }
                .lineLimit(1)
                if !shots.isEmpty {
                    Spacer(minLength: 12)
                    ProductionSteps(project: project, active: !relatedJobs.isEmpty)
                    Text(stageNote(project)).font(.jSmall).foregroundStyle(Color.inkSoft).fixedSize(horizontal: false, vertical: true)
                    HStack(spacing: 8) { stageAction(project); configureButton }.padding(.top, 4)
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
        }
        .padding(20)
        .surface()
        .onGeometryChange(for: CGFloat.self) { $0.size.width } action: { heroWidth = $0 }
    }

    func stageNote(_ project: Project) -> String {
        switch project.currentStage {
        case 0, 1: return "每个首帧都带角色定妆图。先审定妆和首帧，满意后再生成镜头。"
        case 2: return "视频从首帧开始。更换定妆图后，相关首帧和镜头会重新生成。"
        default: return "所有镜头都已完成，可以导出成片。"
        }
    }

    /// Only the next stage is offered: the Mac would do the earlier ones first anyway.
    @ViewBuilder func stageAction(_ project: Project) -> some View {
        let locked = !relatedJobs.isEmpty || dirty
        switch project.currentStage {
        case 0, 1:
            ActionButton(title: "定妆并生成首帧", busyTitle: "正在提交", kind: .primary, scale: .small) { try await produce("frames") }.disabled(locked)
        case 2:
            ActionButton(title: "生成所有未完成镜头", busyTitle: "正在提交", kind: .primary, scale: .small) { try await produce("video") }.disabled(locked)
        default:
            ActionButton(title: "导出成片", busyTitle: "导出中", icon: "square.and.arrow.up", kind: .primary, scale: .small, isBusy: !jobs(kind: "export").isEmpty) {
                _ = try await store.submit("/api/projects/\(projectID)/export", body: [:])
            }
            .disabled(dirty)
        }
    }

    var configureButton: some View {
        Button { configuring = true } label: { Label("生成配置", systemImage: "slider.horizontal.3") }
            .buttonStyle(.jurvis(.ghost, .small))
            .popover(isPresented: $configuring) {
                GenerationOptions(settings: $generation).padding(16).frame(width: 420)
                    .environment(\.colorScheme, colorScheme).environmentObject(store)
            }
    }

    @ViewBuilder func filmSection(_ project: Project) -> some View {
        if !films.isEmpty {
            VStack(alignment: .leading, spacing: 14) {
                SectionTitle(title: "成片", detail: "\(films.count) 个版本")
                LazyVGrid(columns: PosterTile<EmptyView>.columns, alignment: .leading, spacing: 16) {
                    ForEach(films) { asset in
                        VStack(alignment: .leading, spacing: 6) {
                            PosterTile(asset: asset, among: reviewSet(project))
                            Text(asset.created_at.relativeDate).font(.jSmall).foregroundStyle(Color.inkSoft)
                        }
                    }
                }
            }
        }
    }

    func characterSection(_ project: Project) -> some View {
        VStack(alignment: .leading, spacing: 14) {
            SectionTitle(title: "角色", detail: "\(project.document.characters.count) 个")
            LazyVGrid(columns: PosterTile<EmptyView>.columns, alignment: .leading, spacing: 20) {
                ForEach(project.document.characters) { character in
                    let generating = relatedJobs.contains { $0.payload["character_id"].string == character.id }
                    VStack(alignment: .leading, spacing: 8) {
                        if let asset = store.asset(character.reference_asset) {
                            PosterTile(asset: asset, among: reviewSet(project))
                        } else {
                            Slate(icon: "person.crop.rectangle", text: "待定妆")
                        }
                        Text(character.name).font(.jHeading).lineLimit(1)
                        Text(character.description).font(.jSmall).foregroundStyle(Color.inkSoft).lineLimit(3, reservesSpace: true)
                        HStack(spacing: 6) {
                            ActionButton(title: character.reference_asset == nil ? "生成定妆图" : "重新定妆", busyTitle: "生成中", kind: character.reference_asset == nil ? .primary : .secondary, scale: .small, fill: true, isBusy: generating) { try await characterImage(character) }.disabled(dirty)
                            Button { editingCharacter = character } label: { Image(systemName: "pencil") }.buttonStyle(.jurvisIcon()).help("编辑角色").accessibilityLabel("编辑角色")
                        }
                    }
                }
            }
        }
    }

    func shotSection(_ project: Project) -> some View {
        VStack(alignment: .leading, spacing: 14) {
            SectionTitle(title: "分镜", detail: "\(project.document.shots.count) 个镜头")
            LazyVGrid(columns: PosterTile<EmptyView>.columns, alignment: .leading, spacing: 24) {
                ForEach(Array(project.document.shots.enumerated()), id: \.element.id) { index, shot in shotCard(shot, index: index, project: project) }
            }
        }
    }

    func shotCard(_ shot: Shot, index: Int, project: Project) -> some View {
        let job = relatedJobs.first { $0.payload["shot_id"].string == shot.id }
        return VStack(alignment: .leading, spacing: 8) {
            shotFrame(shot, index: index, job: job, project: project)
            HStack(spacing: 6) {
                Text(shot.title).font(.jHeading).lineLimit(1)
                Spacer(minLength: 2)
                if job == nil, shot.video_asset != nil { Image(systemName: "checkmark.circle.fill").font(.jSmall).foregroundStyle(Color.success).accessibilityLabel("已生成") }
            }
            Text(shot.description).font(.jSmall).foregroundStyle(Color.inkSoft).lineSpacing(2).lineLimit(3, reservesSpace: true)
            HStack(spacing: 6) { shotActions(shot, index: index, job: job, project: project) }
        }
    }

    /// The frame reads like the finished shot: its dialogue sits where the subtitle will be.
    @ViewBuilder func shotFrame(_ shot: Shot, index: Int, job: Job?, project: Project) -> some View {
        let frame = store.asset(shot.first_frame)
        let video = store.asset(shot.video_asset)
        let captions = ShotCaptions(number: index + 1, duration: shot.duration, dialogue: shot.dialogue, playable: video != nil, job: job, starting: starting.contains(shot.id))
        if let open = video ?? frame {
            PosterTile(asset: open, showing: frame, among: reviewSet(project)) { captions }
        } else {
            Slate(icon: "photo", text: "待生成首帧").overlay { captions.clipShape(RoundedRectangle(cornerRadius: 10, style: .continuous)) }
        }
    }

    @ViewBuilder func shotActions(_ shot: Shot, index: Int, job: Job?, project: Project) -> some View {
        let video = store.asset(shot.video_asset)
        let needsFrame = !shot.character_ids.isEmpty && shot.first_frame == nil
        if let job, job.isActive {
            ActionButton(title: "取消", busyTitle: "正在停止", kind: .secondary, scale: .small, fill: true, isBusy: store.isCancelling(job)) { await store.cancel(job) }
        } else if let video {
            Button { store.view(video, among: reviewSet(project)) } label: { Label("审片", systemImage: "play.fill").frame(maxWidth: .infinity) }.buttonStyle(.jurvis(.primary, .small, fill: true))
        } else if needsFrame {
            ActionButton(title: "生成首帧", busyTitle: "生成中", kind: .primary, scale: .small, fill: true) { try await shotImage(shot) }.disabled(dirty || !castReady(shot))
        } else {
            ActionButton(title: "生成镜头", busyTitle: "生成中", kind: .primary, scale: .small, fill: true) { try await render(shot) }.disabled(dirty || !castReady(shot))
        }
        Button { editingShot = shot } label: { Image(systemName: "pencil") }.buttonStyle(.jurvisIcon()).help("编辑镜头").accessibilityLabel("编辑镜头")
        Menu {
            if video != nil { Button("重新生成镜头") { start(shot) { try await render(shot) } }.disabled(dirty || job != nil || !castReady(shot) || needsFrame) }
            if !needsFrame { Button(shot.first_frame == nil ? "生成首帧" : "重新生成首帧") { start(shot) { try await shotImage(shot) } }.disabled(dirty || job != nil || !castReady(shot)) }
            Divider()
            Button("上移") { draft?.document.shots.swapAt(index, index - 1); dirty = true }.disabled(index == 0)
            Button("下移") { draft?.document.shots.swapAt(index, index + 1); dirty = true }.disabled(index == project.document.shots.count - 1)
        } label: { Image(systemName: "ellipsis") }
            .menuStyle(.button).menuIndicator(.hidden).buttonStyle(.jurvisIcon()).fixedSize().help("更多操作").accessibilityLabel("更多操作")
    }

    func start(_ shot: Shot, action: @escaping () async throws -> Void) {
        starting.insert(shot.id)
        Task { await store.perform(action); starting.remove(shot.id) }
    }
    func save() async throws { guard let draft else { return }; try await store.saveProject(draft); self.draft = current; dirty = false }
    func characterImage(_ character: Character) async throws {
        _ = try await store.submit("/api/images", body: ["prompt": character.description, "width": 704, "height": 1216, "steps": 25, "project_id": projectID, "character_id": character.id])
    }
    func shotImage(_ shot: Shot) async throws {
        let prompt = "\(draft?.document.style ?? ""). \(shot.image_prompt.isEmpty ? shot.description : shot.image_prompt)"
        _ = try await store.submit("/api/images", body: ["prompt": prompt, "width": 704, "height": 1216, "steps": 25, "project_id": projectID, "shot_id": shot.id])
    }
    func render(_ shot: Shot) async throws {
        var p: [String: Any] = ["prompt": "\(draft?.document.style ?? ""). \(shot.video_prompt.isEmpty ? shot.description : shot.video_prompt)\nExact dialogue: \(shot.dialogue)", "duration": shot.duration, "width": 352, "height": 608, "seed": 42, "enhance": true, "project_id": projectID, "shot_id": shot.id]
        if let first = shot.first_frame { p["first_frame"] = first }; if let last = shot.last_frame { p["last_frame"] = last }
        p.merge(generation.payload) { _, new in new }
        _ = try await store.submit("/api/videos", body: p)
    }
    func castReady(_ shot: Shot) -> Bool { (draft?.document.characters ?? []).filter { shot.character_ids.contains($0.id) }.allSatisfy { $0.reference_asset != nil } }
    func produce(_ stage: String) async throws {
        var p: [String: Any] = ["stage": stage]
        p.merge(generation.payload) { _, new in new }
        _ = try await store.submit("/api/projects/\(projectID)/produce", body: p)
    }
}

/// The empty frame a picture will fill once it has been generated.
struct Slate: View {
    var icon: String
    var text: String
    var body: some View {
        Color.bubble.aspectRatio(PosterTile<EmptyView>.ratio, contentMode: .fit)
            .overlay {
                VStack(spacing: 8) {
                    Image(systemName: icon).font(.system(size: 22, weight: .light))
                    Text(text).font(.jSmall)
                }
                .foregroundStyle(Color.inkFaint)
            }
            .clipShape(RoundedRectangle(cornerRadius: 10, style: .continuous))
    }
}

/// What sits on top of a shot's frame: its number and length, the subtitle, and live progress.
struct ShotCaptions: View {
    var number: Int
    var duration: Double
    var dialogue: String
    var playable: Bool
    var job: Job?
    var starting: Bool

    var body: some View {
        ZStack {
            if !dialogue.isEmpty {
                VStack(spacing: 0) {
                    Spacer()
                    Text(dialogue).font(.jSmall.weight(.medium)).foregroundStyle(.white).multilineTextAlignment(.center).lineLimit(3)
                        .shadow(color: .black.opacity(0.6), radius: 2, y: 1)
                        .padding(.horizontal, 10).padding(.top, 36).padding(.bottom, 12)
                        .frame(maxWidth: .infinity)
                        .background(Color.black.opacity(0.55))
                }
            }
            if playable, job == nil {
                Image(systemName: "play.fill").font(.system(size: 16)).foregroundStyle(.white)
                    .frame(width: 44, height: 44).background(.black.opacity(0.5), in: Circle())
            }
            VStack {
                HStack {
                    chip(String(format: "%02d", number))
                    Spacer()
                    chip("\(Int(duration)) 秒").padding(.trailing, 30)
                }
                Spacer()
            }
            .padding(6)
            if job != nil || starting {
                Color.black.opacity(0.6)
                VStack(spacing: 8) {
                    BusyRing(size: 20)
                    Text(job.map { $0.phase.isEmpty ? $0.statusLabel : $0.phase } ?? "正在提交").font(.jSmall).foregroundStyle(.white).multilineTextAlignment(.center).lineLimit(2)
                    if let job, job.progress > 0, job.progress < 1 { ProgressBar(value: job.progress).frame(maxWidth: 110) }
                }
                .padding(10)
            }
        }
    }

    func chip(_ text: String) -> some View {
        Text(text).font(.jMicro.monospacedDigit()).foregroundStyle(.white)
            .padding(.horizontal, 7).frame(height: 20).background(.black.opacity(0.5), in: Capsule())
    }
}

struct ShotEditor: View {
    @EnvironmentObject var store:AppStore
    @Environment(\.dismiss) var dismiss
    @State var shot: Shot
    var onSave:(Shot)->Void
    var body:some View {
        NavigationStack {
            Form {
                TextField("镜头名称",text:$shot.title)
                Stepper("时长 \(Int(shot.duration)) 秒",value:$shot.duration,in:1...15,step:1)
                SwiftUI.Section("画面与动作") { TextEditor(text:$shot.description).frame(minHeight:100) }
                SwiftUI.Section("对白") { TextEditor(text:$shot.dialogue).frame(minHeight:70) }
                SwiftUI.Section("首帧提示词") { TextEditor(text:$shot.image_prompt).frame(minHeight:100) }
                SwiftUI.Section("视频提示词") { TextEditor(text:$shot.video_prompt).frame(minHeight:150) }
                SwiftUI.Section("参考素材") {
                    Picker("首帧",selection:Binding(get:{shot.first_frame ?? ""},set:{shot.first_frame=$0.isEmpty ? nil:$0})) { Text("不使用").tag(""); ForEach(store.images){Text($0.name).tag($0.id)} }
                    Picker("尾帧",selection:Binding(get:{shot.last_frame ?? ""},set:{shot.last_frame=$0.isEmpty ? nil:$0})) { Text("不使用").tag(""); ForEach(store.images){Text($0.name).tag($0.id)} }
                }
                if !shot.versions.isEmpty {
                    Picker("采用的视频版本",selection:Binding(get:{shot.video_asset ?? ""},set:{shot.video_asset=$0.isEmpty ? nil:$0})) { Text("不采用").tag(""); ForEach(Array(shot.versions.enumerated()),id:\.element) { n,id in Text("版本 \(n+1)").tag(id) } }
                }
            }.sheetForm().navigationTitle("编辑镜头")
            .safeAreaInset(edge:.bottom,spacing:0) { SheetFooter { Button("取消"){dismiss()}.buttonStyle(.jurvis(.ghost)); Button("应用"){onSave(shot);dismiss()}.buttonStyle(.jurvis(.primary)).disabled(shot.title.isEmpty) } }
        }
        #if os(macOS)
        .frame(width:660,height:700)
        #endif
    }
}

struct CharacterEditor: View {
    @EnvironmentObject var store:AppStore
    @Environment(\.dismiss) var dismiss
    @State var character:Character
    var onSave:(Character)->Void
    var body:some View {
        NavigationStack {
            Form {
                TextField("角色名",text:$character.name)
                SwiftUI.Section("外貌、服装、声音与性格") { TextEditor(text:$character.description).frame(minHeight:180) }
                SwiftUI.Section("固定外貌（每个镜头都会带上）") { TextEditor(text:Binding(get:{character.appearance ?? ""},set:{character.appearance=$0})).frame(minHeight:110) }
                SwiftUI.Section("定妆图提示词（留空时使用固定外貌）") { TextEditor(text:$character.image_prompt).frame(minHeight:110) }
                Picker("参考图片",selection:Binding(get:{character.reference_asset ?? ""},set:{character.reference_asset=$0.isEmpty ? nil:$0})) { Text("未选择").tag("");ForEach(store.images){Text($0.name).tag($0.id)} }
            }.sheetForm().navigationTitle("角色设定")
            .safeAreaInset(edge:.bottom,spacing:0) { SheetFooter { Button("取消"){dismiss()}.buttonStyle(.jurvis(.ghost)); Button("应用"){onSave(character);dismiss()}.buttonStyle(.jurvis(.primary)).disabled(character.name.isEmpty) } }
        }
        #if os(macOS)
        .frame(width:600,height:600)
        #endif
    }
}
