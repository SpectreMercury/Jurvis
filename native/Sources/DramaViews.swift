import SwiftUI

struct DramaView: View {
    @EnvironmentObject var store: AppStore
    @State private var creating = false
    @State private var title = ""
    @State private var brief = ""
    @State private var busy = false
    var body: some View {
        Page(title: "短剧", subtitle: "从故事梗概到角色、分镜、首尾帧和带声音的镜头，最后导出成片。") {
            Button { creating = true } label: { Label("新建短剧", systemImage: "plus") }.buttonStyle(.borderedProminent)
        } content: {
            if store.snapshot?.projects.isEmpty != false {
                EmptyState(icon: "film.stack", title: "还没有短剧", detail: "写下故事梗概，Jurvis 会准备角色与分镜。你可以审阅、修改，再逐镜生成。")
            }
            ForEach(store.snapshot?.projects ?? []) { project in
                NavigationLink { ProjectDetailView(projectID: project.id) } label: {
                    let shots = project.document.shots
                    HStack(alignment: .center, spacing: 14) {
                        Image(systemName: "film").font(.title3).foregroundStyle(Color.jurvisAccent)
                            .frame(width: 44, height: 44).background(Color.jurvisAccent.opacity(0.1), in: RoundedRectangle(cornerRadius: 10))
                        VStack(alignment: .leading, spacing: 4) {
                            Text(project.title).font(.headline).foregroundStyle(.primary)
                            Text(project.document.logline.isEmpty ? project.brief : project.document.logline).font(.callout).foregroundStyle(.secondary).lineLimit(2)
                            Text("\(project.document.characters.count) 个角色 · \(shots.filter { $0.video_asset != nil }.count)/\(shots.count) 镜头完成 · 约 \(Int(shots.reduce(0) { $0 + $1.duration })) 秒 · \(project.updated_at.relativeDate)")
                                .font(.caption).foregroundStyle(.tertiary).lineLimit(1).minimumScaleFactor(0.85)
                            if !shots.isEmpty {
                                ProgressView(value: Double(shots.filter { $0.video_asset != nil }.count), total: Double(shots.count)).frame(maxWidth: 220)
                            }
                        }
                        Spacer(minLength: 4)
                        Image(systemName: "chevron.right").foregroundStyle(.tertiary)
                    }
                    .padding(14).background(Color.card, in: RoundedRectangle(cornerRadius: Metrics.radius))
                    .overlay(RoundedRectangle(cornerRadius: Metrics.radius).strokeBorder(Color.hairline))
                }.buttonStyle(.plain)
            }
        }
        .sheet(isPresented:$creating) {
            NavigationStack {
                Form {
                    TextField("短剧名称",text:$title)
                    SwiftUI.Section("故事梗概") { TextEditor(text:$brief).frame(minHeight:180); Text("可以包含总时长、人物、风格、关键对白和结局。默认生成竖屏短剧计划。").font(.caption).foregroundStyle(.secondary) }
                }.formStyle(.grouped).navigationTitle("新建短剧")
                .toolbar {
                    ToolbarItem(placement:.cancellationAction) { Button("取消") { creating = false } }
                    ToolbarItem(placement:.confirmationAction) { Button(busy ? "正在提交" : "生成分镜") { Task { await create() } }.disabled(title.isEmpty || brief.isEmpty || busy) }
                }
            }
            #if os(macOS)
            .frame(width:560,height:440)
            #endif
        }
    }
    func create() async {
        guard let api = store.api else { return }
        busy = true; defer { busy = false }
        await store.perform { let _:Project = try await api.request("/api/projects",method:"POST",body:["title":title,"brief":brief]); creating = false; title = ""; brief = "" }
    }
}

struct ProjectDetailView: View {
    @EnvironmentObject var store: AppStore
    let projectID: String
    @State private var draft: Project?
    @State private var editingShot: Shot?
    @State private var editingCharacter: Character?
    @State private var selected: Asset?
    @State private var busy = false
    @State private var dirty = false
    @State private var confirmReplan = false
    @State private var generation = GenerationSettings()
    var current: Project? { store.snapshot?.projects.first{$0.id==projectID} }
    var relatedJobs: [Job] { store.activeJobs.filter{$0.payload["project_id"].string==projectID} }
    var body: some View {
        ScrollView {
            if let project = draft {
                VStack(alignment:.leading,spacing:20) {
                    VStack(alignment:.leading,spacing:6) {
                        Text(project.title).font(.title2.weight(.semibold))
                        Text(project.document.logline.isEmpty ? project.brief : project.document.logline).font(.callout).foregroundStyle(.secondary).textSelection(.enabled)
                        HStack(spacing:8) { Badge(text:"9:16 竖屏"); if !project.document.style.isEmpty { Badge(text:project.document.style,tone:.success) } }
                    }
                    if dirty {
                        HStack { Label("有未保存的修改",systemImage:"pencil.circle").font(.callout); Spacer(); Button("放弃") { draft = current; dirty = false }.buttonStyle(.bordered); Button("保存") { Task { await save() } }.buttonStyle(.borderedProminent) }
                            .padding(12).background(Color.jurvisAccent.opacity(0.08),in:RoundedRectangle(cornerRadius:Metrics.smallRadius))
                    }
                    ForEach(relatedJobs) { JobCard(job:$0) }
                    if project.document.shots.isEmpty {
                        EmptyState(icon:"text.book.closed",title:relatedJobs.isEmpty ? "还没有分镜" : "正在编写角色与分镜",detail:"计划完成后会显示在这里，你可以先去做别的事。")
                        if relatedJobs.isEmpty { Button("生成分镜计划") { Task { await store.perform { _ = try await store.submit("/api/projects/\(projectID)/plan",body:[:]) } } }.buttonStyle(.borderedProminent) }
                    } else {
                        if project.document.characters.isEmpty { Hint(text:"这部短剧没有角色设定，镜头将只按画面描述生成。") } else { characterSection(project) }
                        Card { GenerationOptions(settings:$generation) }
                        SectionTitle(title:"分镜",detail:"\(project.document.shots.count) 个镜头 · 约 \(Int(project.document.shots.reduce(0){$0+$1.duration})) 秒")
                        ForEach(Array(project.document.shots.enumerated()),id:\.element.id) { index,shot in shotCard(shot,index:index,project:project) }
                        Card {
                            HStack {
                                Button("生成所有未完成镜头") { Task { await renderAll() } }.buttonStyle(.bordered).disabled(busy || !relatedJobs.isEmpty || dirty)
                                Spacer()
                                Button { Task { await store.perform { _ = try await store.submit("/api/projects/\(projectID)/export",body:[:]) } } } label: { Label("导出成片",systemImage:"square.and.arrow.up") }
                                    .buttonStyle(.borderedProminent).disabled(project.document.shots.contains{$0.video_asset==nil} || dirty)
                            }
                            Hint(text:"角色参考图用于生成一致的首帧，首尾帧约束视频画面。角色一致性和对白效果需要逐镜审片。")
                            let films = (store.snapshot?.assets ?? []).filter{$0.kind=="film" && $0.metadata["project_id"].string==projectID}
                            ForEach(films) { asset in Button { selected = asset } label: { Label("\(asset.name) · \(asset.created_at.relativeDate)",systemImage:"play.rectangle.fill") }.buttonStyle(.borderless) }
                        }
                    }
                }.padding(Metrics.pagePadding).frame(maxWidth:Metrics.wide,alignment:.leading).frame(maxWidth:.infinity)
            } else { ProgressView().padding(40) }
        }.background(Color.canvas).navigationTitle(draft?.title ?? "短剧")
        .task { draft = current }
        .onChange(of:current?.updated_at) { _,_ in if !dirty && editingShot==nil && editingCharacter==nil { draft = current } }
        .sheet(item:$editingShot) { shot in ShotEditor(shot:shot) { updated in if let index = draft?.document.shots.firstIndex(where:{$0.id==updated.id}) { draft?.document.shots[index] = updated; dirty = true } }.environmentObject(store) }
        .sheet(item:$editingCharacter) { character in CharacterEditor(character:character) { updated in if let index = draft?.document.characters.firstIndex(where:{$0.id==updated.id}) { draft?.document.characters[index] = updated; dirty = true } }.environmentObject(store) }
        .sheet(item:$selected) { MediaDetail(asset:$0).environmentObject(store) }
    }
    func characterSection(_ project:Project)->some View {
        VStack(alignment:.leading,spacing:14) {
            SectionTitle(title:"角色",detail:"\(project.document.characters.count) 个")
            LazyVGrid(columns:[GridItem(.adaptive(minimum:220),spacing:12)],spacing:12) {
                ForEach(project.document.characters) { character in
                    VStack(alignment:.leading,spacing:12) {
                        if let asset = store.snapshot?.assets.first(where:{$0.id==character.reference_asset}) { MediaThumb(asset:asset).frame(height:150) }
                        HStack { Text(character.name).font(.headline); Spacer(); Button { editingCharacter = character } label: { Image(systemName:"pencil") }.buttonStyle(.borderless) }
                        Text(character.description).font(.caption).foregroundStyle(.secondary).lineLimit(5)
                        Button(character.reference_asset==nil ? "生成角色参考图" : "重新生成参考图") { Task { await characterImage(character) } }.buttonStyle(.bordered).disabled(busy || dirty)
                    }.padding(14).background(Color.card,in:RoundedRectangle(cornerRadius:Metrics.radius)).overlay(RoundedRectangle(cornerRadius:Metrics.radius).strokeBorder(Color.hairline))
                }
            }
        }
    }
    func shotCard(_ shot:Shot,index:Int,project:Project)->some View {
        VStack(alignment:.leading,spacing:16) {
            HStack {
                Text(String(format:"%02d",index+1)).font(.system(size:20,weight:.medium,design:.monospaced)).foregroundStyle(Color.jurvisAccent)
                VStack(alignment:.leading,spacing:3) { Text(shot.title).font(.headline); Text("\(Int(shot.duration)) 秒 · \(shot.versions.count) 个版本").font(.caption).foregroundStyle(.secondary) }
                Spacer()
                if let job = relatedJobs.first(where:{$0.payload["shot_id"].string==shot.id}) { Badge(text:job.phase.isEmpty ? job.statusLabel : job.phase,tone:.active) } else if shot.video_asset != nil { Badge(text:"已生成",tone:.success) }
                Button { editingShot = shot } label: { Image(systemName:"pencil") }.buttonStyle(.borderless)
            }
            if let first = store.snapshot?.assets.first(where:{$0.id==shot.first_frame}) {
                HStack(alignment:.top,spacing:14) { MediaThumb(asset:first).frame(width:110,height:145); Text(shot.description).font(.subheadline).foregroundStyle(.secondary).frame(maxWidth:.infinity,alignment:.leading) }
            } else { Text(shot.description).font(.subheadline).foregroundStyle(.secondary) }
            if !shot.dialogue.isEmpty { Text(shot.dialogue).font(.callout).padding(12).frame(maxWidth:.infinity,alignment:.leading).background(Color.jurvisAccent.opacity(0.05),in:RoundedRectangle(cornerRadius:10)) }
            ViewThatFits(in:.horizontal) { HStack { shotActions(shot,index:index) }; VStack(alignment:.leading,spacing:12) { shotActions(shot,index:index) } }
        }.padding(16).background(Color.card,in:RoundedRectangle(cornerRadius:Metrics.radius)).overlay(RoundedRectangle(cornerRadius:Metrics.radius).strokeBorder(Color.hairline))
    }
    @ViewBuilder func shotActions(_ shot:Shot,index:Int)->some View {
        HStack(spacing:10) {
            Button("生成首帧") { Task { await shotImage(shot) } }.buttonStyle(.bordered).disabled(busy || dirty)
            Button(shot.video_asset==nil ? "生成镜头" : "重新生成") { Task { await render(shot) } }.buttonStyle(.borderedProminent).disabled(busy || dirty)
            if let asset = store.snapshot?.assets.first(where:{$0.id==shot.video_asset}) { Button { selected = asset } label: { Label("审片",systemImage:"play.fill") }.buttonStyle(.bordered) }
        }
        Spacer(minLength:4)
        HStack {
            Button { if index>0 { draft?.document.shots.swapAt(index,index-1); dirty = true } } label: { Image(systemName:"arrow.up") }.disabled(index==0)
            Button { if let count=draft?.document.shots.count,index<count-1 { draft?.document.shots.swapAt(index,index+1); dirty = true } } label: { Image(systemName:"arrow.down") }.disabled(index==(draft?.document.shots.count ?? 1)-1)
        }.buttonStyle(.borderless)
    }
    func save() async { guard let draft else{return}; await store.perform { try await store.saveProject(draft); self.draft = current; dirty = false } }
    func characterImage(_ character:Character) async {
        await store.perform { _ = try await store.submit("/api/images",body:["prompt":character.image_prompt.isEmpty ? character.description : character.image_prompt,"width":768,"height":1024,"steps":25,"seed":42,"project_id":projectID,"character_id":character.id]) }
    }
    func shotImage(_ shot:Shot) async {
        let refs = (draft?.document.characters ?? []).filter{shot.character_ids.contains($0.id)}.compactMap(\.reference_asset)
        let prompt = "\(draft?.document.style ?? ""). \(shot.image_prompt.isEmpty ? shot.description : shot.image_prompt). Preserve character identity, costume and facial features from reference images."
        await store.perform { _ = try await store.submit("/api/images",body:["prompt":prompt,"width":768,"height":1024,"steps":25,"seed":42,"project_id":projectID,"shot_id":shot.id,"reference_assets":Array(refs.prefix(4))]) }
    }
    func render(_ shot:Shot) async {
        var p:[String:Any] = ["prompt":"\(draft?.document.style ?? ""). \(shot.video_prompt.isEmpty ? shot.description : shot.video_prompt)\nExact dialogue: \(shot.dialogue)","duration":shot.duration,"width":352,"height":608,"seed":42,"enhance":true,"project_id":projectID,"shot_id":shot.id]
        if let first=shot.first_frame { p["first_frame"] = first }; if let last=shot.last_frame { p["last_frame"] = last }
        p.merge(generation.payload){_,new in new}
        await store.perform { _ = try await store.submit("/api/videos",body:p) }
    }
    func renderAll() async { busy = true; defer {busy=false}; for shot in draft?.document.shots.filter({$0.video_asset==nil}) ?? [] { await render(shot) } }
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
            }.formStyle(.grouped).navigationTitle("编辑镜头")
            .toolbar { ToolbarItem(placement:.cancellationAction){Button("取消"){dismiss()}}; ToolbarItem(placement:.confirmationAction){Button("应用"){onSave(shot);dismiss()}.disabled(shot.title.isEmpty)} }
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
                SwiftUI.Section("角色参考图提示词") { TextEditor(text:$character.image_prompt).frame(minHeight:150) }
                Picker("参考图片",selection:Binding(get:{character.reference_asset ?? ""},set:{character.reference_asset=$0.isEmpty ? nil:$0})) { Text("未选择").tag("");ForEach(store.images){Text($0.name).tag($0.id)} }
            }.formStyle(.grouped).navigationTitle("角色设定")
            .toolbar { ToolbarItem(placement:.cancellationAction){Button("取消"){dismiss()}};ToolbarItem(placement:.confirmationAction){Button("应用"){onSave(character);dismiss()}.disabled(character.name.isEmpty)} }
        }
        #if os(macOS)
        .frame(width:600,height:600)
        #endif
    }
}
