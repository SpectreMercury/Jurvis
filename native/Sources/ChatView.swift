import SwiftUI
import UniformTypeIdentifiers

/// What the next message should make; picked with the chips by the composer.
enum ComposerMode: String, CaseIterable, Identifiable {
    case goal, image, video, drama
    var id: String { rawValue }
    var title: String { ["goal": "目标", "image": "图片", "video": "视频", "drama": "短剧"][rawValue]! }
    var icon: String { ["goal": "scope", "image": "photo", "video": "film", "drama": "theatermasks"][rawValue]! }
    var placeholder: String {
        ["goal": "给一个要做到完成的目标，例如：装好 xx 插件并确认能用", "image": "描述你想要的画面", "video": "描述镜头、动作和声音", "drama": "说说故事、时长和风格"][rawValue]!
    }
    var hint: String {
        ["goal": "目标：先列计划，自己做完再汇报。需要运行命令时会先问你。", "image": "图片：描述画面，在这台 Mac 上生成。",
         "video": "视频：描述镜头、动作和声音，在这台 Mac 上生成。", "drama": "短剧：从梗概到角色、分镜和镜头，一步步做成片。"][rawValue]!
    }
}

/// A task: an empty one is a headline and a composer; a running one is the conversation with its steps.
struct ChatView: View {
    @EnvironmentObject var store: AppStore
    @State private var input = ""
    @State private var sending = false
    @State private var importing = false
    @State private var connecting = false
    /// Attachments picked, pasted or dropped that are still uploading.
    @State private var uploading = 0
    @State private var linkingProject = false
    @State private var attachments: [Asset] = []
    @State private var mode: ComposerMode?
    @State private var width: CGFloat = 800
    /// The room the container gives this view. The view's own measured width cannot tell a phone from a tablet once it is laid out wide:
    /// the wide composer does not shrink, so the view overflows a screen that got narrower (a phone that was folded) and keeps reading as wide.
    var available: CGFloat?
    /// The voice conversation is open.
    @State private var talking = false
    @ObservedObject private var voice = Voice.shared
    @FocusState private var focused: Bool
    #if os(macOS)
    @State private var pasteMonitor: Any?
    #endif

    /// Nothing has been said yet: the conversation has not begun.
    var isHome: Bool { store.visibleMessages.isEmpty && !store.streamEarlier && !store.loadingSession }
    /// Phone width: one column, a one-line composer, finger-sized controls.
    var narrow: Bool {
        #if os(iOS)
        return (available ?? width) < Metrics.phoneWidth
        #else
        return false
        #endif
    }
    #if os(macOS)
    private let control: CGFloat = 32
    #else
    private let control: CGFloat = 40
    #endif

    var body: some View {
        Group {
            if isHome {
                home
            } else {
                conversation
            }
        }
        .background(Color.canvas)
        .onGeometryChange(for: CGFloat.self) { $0.size.width } action: { width = $0 }
        .fileImporter(isPresented: $importing, allowedContentTypes: [.image], allowsMultipleSelection: true) { result in
            guard let urls = try? result.get() else { return }
            let picked = urls.prefix(max(0, 4 - attachments.count - uploading))
            uploading += picked.count
            Task {
                for url in picked {
                    await store.perform { attachments.append(try await store.uploadImage(url)) }
                    uploading -= 1
                }
            }
        }
        .fileImporter(isPresented: $connecting, allowedContentTypes: [.folder]) { result in
            linkingProject = true
            Task { defer { linkingProject = false }; await store.perform {
                let url = try result.get()
                let granted = url.startAccessingSecurityScopedResource(); defer { if granted { url.stopAccessingSecurityScopedResource() } }
                let project: CodeProject = try await store.requireAPI().request("/api/code/projects", method: "POST", body: ["root": url.path])
                await store.loadCodeProjects()
                await store.newChat(project: project.id)
            } }
        }
        // What the Mac can do with speech decides which voice buttons the composer shows.
        .task(id: store.connected) { await voice.refresh(store.api) }
        #if os(iOS)
        .fullScreenCover(isPresented: $talking) { VoiceConversationView().environmentObject(store) }
        #else
        .sheet(isPresented: $talking) { VoiceConversationView().environmentObject(store) }
        #endif
        #if os(macOS)
        .onAppear { focused = true }  // on iPhone this would pop the keyboard and swallow the next tap
        #endif
        #if os(macOS)
        // ⌘V with an image on the clipboard attaches it; plain text still pastes into the field.
        .onAppear { pasteMonitor = NSEvent.addLocalMonitorForEvents(matching: .keyDown) { event in
            guard event.modifierFlags.intersection(.deviceIndependentFlagsMask) == .command, event.charactersIgnoringModifiers == "v",
                  focused, let images = ClipboardImages.read(), !images.isEmpty else { return event }
            attach(images); return nil
        } }
        .onDisappear { if let pasteMonitor { NSEvent.removeMonitor(pasteMonitor) }; pasteMonitor = nil }
        #else
        .navigationBarTitleDisplayMode(.inline)
        .toolbar {
            ToolbarItem(placement: .principal) {
                VStack(spacing: 1) {
                    Text(store.currentSession?.title ?? "Jurvis").font(.jHeading).lineLimit(1)
                    if let status = store.runStatus { RunStatusLine(text: status) }
                }
            }
        }
        #endif
    }

    /// Uploads pasted or dropped images as attachments, up to four per message.
    func attach(_ images: [(Data, String)]) {
        let picked = images.prefix(max(0, 4 - attachments.count - uploading))
        uploading += picked.count
        Task {
            for (data, name) in picked {
                await store.perform { attachments.append(try await store.uploadImage(data, name: name)) }
                uploading -= 1
            }
        }
    }

    // MARK: New task

    var home: some View {
        VStack(spacing: 0) {
            Spacer(minLength: 20)
            Text("今天交给 Jurvis 什么？").font(.jDisplay).multilineTextAlignment(.center).lineLimit(2).minimumScaleFactor(0.8)
                .padding(.bottom, narrow ? 20 : 28)
            composer.frame(maxWidth: 680)
            ViewThatFits(in: .horizontal) {
                modeRow
                ScrollView(.horizontal, showsIndicators: false) { modeRow }
            }
            .padding(.top, 16)
            Text(hint).font(.jCallout).foregroundStyle(Color.inkSoft).multilineTextAlignment(.center)
                .fixedSize(horizontal: false, vertical: true).frame(maxWidth: 520, minHeight: 40, alignment: .top).padding(.top, 14)
            Spacer(minLength: 20)
            waiting.frame(maxWidth: 680)
            Spacer(minLength: narrow ? 12 : 28)
        }
        .padding(.horizontal, narrow ? 16 : 32)
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }

    var hint: String {
        if let mode { return mode.hint }
        if store.activeProject != nil { return "Jurvis 会先阅读代码，修改以差异形式出现在对话里，你点「应用修改」才会写入文件。" }
        return "对话：直接说想要的结果，Jurvis 会按需要读文件、搜索和动手。"
    }

    /// Task types, as a row of pills under the composer.
    var modeRow: some View {
        HStack(spacing: 8) {
            Chip(title: "对话", icon: "text.bubble", on: mode == nil, scale: .large) { mode = nil }
            ForEach(ComposerMode.allCases) { item in
                Chip(title: item.title, icon: item.icon, on: mode == item, scale: .large) { mode = mode == item ? nil : item }
            }
            projectChip(compact: false, scale: .large)
        }
        .fixedSize()
    }

    /// Tasks that stopped to ask something, so a new task never hides them.
    @ViewBuilder var waiting: some View {
        let items = store.waitingSessions
        if !items.isEmpty {
            VStack(alignment: .leading, spacing: 8) {
                Eyebrow(text: "等你处理").padding(.horizontal, 4)
                ForEach(items.prefix(2), id: \.session.id) { item in
                    Button { Task { await store.selectSession(item.session.id) } } label: {
                        HStack(spacing: 12) {
                            IconWell(icon: "hand.raised", tint: .signal, fill: .signalSoft)
                            VStack(alignment: .leading, spacing: 1) {
                                Text(item.session.title).font(.jBody).foregroundStyle(Color.ink).lineLimit(1)
                                Text(item.job.phase).font(.jSmall).foregroundStyle(Color.inkFaint).lineLimit(1)
                            }
                            Spacer(minLength: 8)
                            HStack(spacing: 4) { Text("去确认"); Image(systemName: "chevron.right").font(.system(size: 10)) }
                                .font(.jCallout).foregroundStyle(Color.signal).fixedSize()
                        }
                        .padding(.horizontal, 14).frame(minHeight: 56)
                        .overlay(RoundedRectangle(cornerRadius: 14, style: .continuous).strokeBorder(Color.hairline))
                        .contentShape(Rectangle())
                    }
                    .buttonStyle(PressStyle())
                }
            }
        }
    }

    // MARK: Running task

    var conversation: some View {
        VStack(spacing: 0) {
            #if os(macOS)
            header
            #endif
            ScrollViewReader { proxy in
                ScrollView {
                    LazyVStack(alignment: .leading, spacing: 22) {
                        if store.loadingSession && store.messages.isEmpty { LoadingState(text: "正在打开对话") }
                        if store.streamEarlier {
                            ActionButton(title: "更早的对话", busyTitle: "正在载入", kind: .ghost, scale: .small) { await store.loadEarlier() }
                                .frame(maxWidth: .infinity)
                        }
                        let shown = store.visibleMessages
                        ForEach(Array(shown.enumerated()), id: \.element.id) { index, message in
                            // One conversation, in stretches: where a new one begins, the time says how long ago that was.
                            if message.id != "outgoing", index == 0 || shown[index - 1].session_id != message.session_id {
                                StretchMark(time: message.created_at)
                            }
                            Group {
                                if message.isUser { UserMessage(message: message, open: { store.view($0) }) }
                                else { AssistantMessage(message: message, open: { store.view($0) }) }
                            }
                            .id(message.id)
                        }
                        if store.visibleMessages.last?.id == "outgoing" {
                            HStack(spacing: 8) { SignalDot(); Text("正在发送").font(.jSmall).foregroundStyle(Color.inkSoft) }
                        }
                        Color.clear.frame(height: 1).id("bottom")
                    }
                    .padding(.horizontal, narrow ? 16 : 24).padding(.top, 16).padding(.bottom, 12)
                    .frame(maxWidth: Metrics.readable).frame(maxWidth: .infinity)
                }
                .scrollDismissesKeyboard(.interactively)
                #if os(iOS)
                .simultaneousGesture(TapGesture().onEnded { focused = false })  // a tap on the conversation puts the keyboard away
                #endif
                .onChange(of: store.messages.last?.content) { _, _ in settle(proxy) }
                .onChange(of: store.messages.last?.steps.flatMap(\.calls).count) { _, _ in settle(proxy) }
                .onChange(of: store.visibleMessages.count) { _, _ in settle(proxy, animated: true) }
                .onChange(of: store.scrollTarget) { _, target in if target != nil { settle(proxy) } }  // clearing it is not a reason to move
                .onAppear { settle(proxy) }
            }
            WaitingQuestions()
                .frame(maxWidth: Metrics.readable)
                .padding(.horizontal, narrow ? 12 : 24).padding(.top, 6)
            if store.streamPast {
                HStack(spacing: 6) {
                    Image(systemName: "clock.arrow.circlepath")
                    Text("这是较早的对话").lineLimit(1)
                    ActionButton(title: "回到现在", busyTitle: "正在载入", kind: .ghost, scale: .small) { await store.loadNewest() }
                }
                .font(.jSmall).foregroundStyle(Color.inkSoft).padding(.top, 6)
            } else if store.fresh {
                HStack(spacing: 6) {
                    Image(systemName: "square.and.pencil")
                    Text("下一条消息会开始一个新话题").lineLimit(1)
                    Button("取消") { store.fresh = false; store.projectPicked = false }.buttonStyle(.plain).foregroundStyle(Color.ink)
                }
                .font(.jSmall).foregroundStyle(Color.inkSoft).padding(.top, 6)
            }
            if store.currentSession?.allow_commands == 1, let id = store.selectedSession, !store.streamPast {
                HStack(spacing: 6) {
                    Image(systemName: "terminal")
                    Text("这段对话里的命令会直接运行").lineLimit(1)
                    ActionButton(title: "改为每次确认", busyTitle: "正在修改", kind: .ghost, scale: .small) { await store.setAllowCommands(id, false) }
                }
                .font(.jSmall).foregroundStyle(Color.inkSoft).padding(.top, 6)
            }
            composer
                .frame(maxWidth: Metrics.readable)
            .padding(.horizontal, narrow ? 12 : 24).padding(.bottom, narrow ? 8 : 18).padding(.top, 8)
        }
    }

    /// Keeps the newest message in view, or brings the one that was asked for to the top (an old stretch, a search result).
    func settle(_ proxy: ScrollViewProxy, animated: Bool = false) {
        if let target = store.scrollTarget {
            proxy.scrollTo(target, anchor: .top)
            Task { @MainActor in
                try? await Task.sleep(for: .milliseconds(400))  // the list is still laying itself out: hold the position meanwhile
                proxy.scrollTo(target, anchor: .top)
                if store.scrollTarget == target { store.scrollTarget = nil }
            }
        } else if !store.streamPast {
            if animated { withAnimation(.easeOut(duration: 0.2)) { proxy.scrollTo("bottom", anchor: .bottom) } }
            else { proxy.scrollTo("bottom", anchor: .bottom) }
        }
    }

    #if os(macOS)
    /// What is being talked about and where it stands.
    var header: some View {
        HStack(spacing: 10) {
            Text(store.streamPast ? "较早的对话" : store.currentSession?.title ?? "Jurvis").font(.jSection).lineLimit(1).truncationMode(.tail)
            if let status = store.runStatus { Badge(text: status, tone: .active) }
            Spacer(minLength: 8)
            if let job = store.chatJob, !store.isCancelling(job) {
                Button { Task { await store.cancel(job) } } label: { Image(systemName: "stop.circle") }
                    .buttonStyle(.jurvisIcon(.ghost, .regular)).help("停止").accessibilityLabel("停止任务")
            }
        }
        .padding(.leading, 24).padding(.trailing, 12).frame(height: 52)
    }
    #endif

    // MARK: Composer

    var canSend: Bool {
        !input.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty && store.connected && !sending && !store.chatBusy && uploading == 0
    }

    var placeholder: String {
        if let mode { return mode.placeholder }
        if let project = store.activeProject { return "问问 \(project.name) 的代码，或说说要改什么" }
        if isHome { return narrow ? "说清楚想要的结果" : "说清楚想要的结果，比如：写一个 10 集短剧剧本，保存成文件" }
        return narrow ? "补充要求或调整方向" : "补充要求或调整方向，Jurvis 会接着做"
    }

    var addLabel: some View {
        Image(systemName: "plus").font(.system(size: 14, weight: .regular)).foregroundStyle(Color.ink).frame(width: control, height: control)
            .overlay(Circle().strokeBorder(narrow && !isHome ? Color.clear : Color.hairline))
            .frame(minWidth: narrow ? 44 : nil, minHeight: narrow ? 44 : nil)
            .contentShape(Rectangle())
    }

    @ViewBuilder var addButton: some View {
        #if os(iOS)
        // iOS text fields can't take an image paste, so pasting lives here. On a phone the task types do too.
        Menu {
            Button { importing = true } label: { Label("选择图片", systemImage: "photo.on.rectangle") }
            Button { if let images = ClipboardImages.read(), !images.isEmpty { attach(images) } else { store.error = "剪贴板里没有图片" } } label: {
                Label("粘贴图片", systemImage: "doc.on.clipboard")
            }
            if narrow && !isHome {
                SwiftUI.Section("任务类型") {
                    ForEach(ComposerMode.allCases) { item in
                        Button { mode = mode == item ? nil : item } label: { Label(item.title, systemImage: mode == item ? "checkmark" : item.icon) }
                    }
                }
            }
        } label: { addLabel }
        .menuStyle(.button).buttonStyle(.plain).menuIndicator(.hidden)
        .accessibilityLabel("添加图片")
        #else
        Button { importing = true } label: { addLabel }
            .buttonStyle(.plain).help("添加图片（最多 4 张），也可以直接粘贴或拖进来").accessibilityLabel("添加图片")
        #endif
    }

    @ViewBuilder var attachmentStrip: some View {
        if !attachments.isEmpty || uploading > 0 {
            ScrollView(.horizontal, showsIndicators: false) {
                HStack(spacing: 8) {
                    ForEach(attachments) { asset in
                        Button { store.view(asset, among: attachments) } label: { MediaThumb(asset: asset).frame(width: 56, height: 56) }.buttonStyle(.plain)
                            .overlay(alignment: .topTrailing) {
                                Button { attachments.removeAll { $0.id == asset.id } } label: {
                                    Image(systemName: "xmark.circle.fill").symbolRenderingMode(.palette).foregroundStyle(.white, .black.opacity(0.6))
                                }.buttonStyle(.plain).padding(3).accessibilityLabel("移除 \(asset.name)")
                            }
                    }
                    ForEach(0..<uploading, id: \.self) { _ in
                        RoundedRectangle(cornerRadius: 10, style: .continuous).fill(Color.bubble).frame(width: 56, height: 56)
                            .overlay { BusyRing(size: 16) }.accessibilityLabel("正在上传图片")
                    }
                }
            }
        }
    }

    var hideKeyboardButton: some View {
        Button { focused = false } label: {
            Image(systemName: "keyboard.chevron.compact.down").font(.system(size: 15, weight: .regular)).foregroundStyle(Color.inkSoft)
                .frame(width: 44, height: 44).contentShape(Rectangle())
        }
        .buttonStyle(.plain).accessibilityLabel("收起键盘")
    }

    var field: some View {
        TextField(placeholder, text: $input, axis: .vertical)
            .textFieldStyle(.plain).font(.system(size: 15)).lineLimit(1...10)
            .focused($focused)
            .onSubmit { if canSend { Task { await send() } } }
            .accessibilityLabel("消息输入框")
    }

    /// A rounded card with a hairline and a very soft shadow. On a phone, inside a task, it folds to one line.
    var composer: some View {
        let pill = narrow && !isHome
        let radius: CGFloat = pill ? 24 : Metrics.composerRadius
        return VStack(alignment: .leading, spacing: 10) {
            attachmentStrip
            if pill {
                if let mode {
                    Chip(title: mode.title, icon: mode.icon, on: true) { self.mode = nil }.padding(.leading, 8)
                }
                HStack(alignment: .bottom, spacing: 2) {
                    addButton.disabled(attachments.count + uploading >= 4)
                    field.padding(.vertical, 11)
                    // While typing, the keyboard has its own dictation key: the room goes to a way to put the keyboard away.
                    if focused { hideKeyboardButton } else { voiceButtons }
                    sendButton
                }
            } else {
                // Room for a couple of lines before it starts growing.
                field.padding(.leading, 2).frame(minHeight: isHome ? 46 : 26, alignment: .topLeading)
                HStack(spacing: 6) {
                    addButton.disabled(attachments.count + uploading >= 4)
                    if !isHome {
                        // Labels when they fit, icons only when they do not.
                        ViewThatFits(in: .horizontal) {
                            chips(compact: false)
                            chips(compact: true)
                        }
                    }
                    Spacer(minLength: 4)
                    #if os(iOS)
                    if focused { hideKeyboardButton }
                    #endif
                    voiceButtons
                    sendButton
                }
            }
        }
        .padding(.leading, pill ? 2 : 16).padding(.trailing, pill ? 2 : 10).padding(.top, pill ? 2 : 14).padding(.bottom, pill ? 2 : 10)
        .background {
            RoundedRectangle(cornerRadius: radius, style: .continuous).fill(Color.card)
                .shadow(color: Color.shadow, radius: 1, y: 1)
                .shadow(color: Color.shadow, radius: 12, y: 8)
        }
        .overlay { RoundedRectangle(cornerRadius: radius, style: .continuous).strokeBorder(focused ? Color.ring : Color.stroke) }
        .animation(.easeOut(duration: 0.2), value: focused)
        .onDrop(of: [.image], isTargeted: nil) { providers in
            // Images dragged in from Finder, Photos or a browser.
            Task {
                var images: [(Data, String)] = []
                for provider in providers {
                    if let data = try? await provider.loadImageData() { images.append((data, provider.suggestedName.map { $0 + ".png" } ?? "拖入的图片.png")) }
                }
                if !images.isEmpty { attach(images) }
            }
            return true
        }
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

    /// The code project being worked in. Picking another, or none, applies from the next message on.
    @ViewBuilder func projectChip(compact: Bool, scale: Chip.Scale = .regular) -> some View {
        Menu {
            ForEach(store.codeProjects) { project in
                Button { store.codeProjectID = project.id; store.projectPicked = true } label: {
                    if project.id == store.activeProject?.id { Label(project.name, systemImage: "checkmark") } else { Text(project.name) }
                }
            }
            if store.activeProject != nil { Button("不关联项目") { store.codeProjectID = nil; store.projectPicked = true } }
            #if os(macOS)
            Divider()
            Button { connecting = true } label: { Label("连接项目文件夹…", systemImage: "folder.badge.plus") }
            #endif
        } label: {
            Chip(title: linkingProject ? "正在连接" : store.activeProject?.name ?? "代码", icon: "chevron.left.forwardslash.chevron.right", on: store.activeProject != nil, compact: compact && store.activeProject == nil && !linkingProject, busy: linkingProject, scale: scale) {}
                .allowsHitTesting(false)
        }
        .menuStyle(.button).buttonStyle(.plain).menuIndicator(.hidden).fixedSize()
        .help("接下来在一个代码项目里工作")
    }

    /// Dictation into the field, and the hands-free conversation. Each shows only when the Mac has the model it needs.
    @ViewBuilder var voiceButtons: some View {
        if voice.canListen {
            DictationButton(size: control, interrupted: talking) { text in
                input += (input.isEmpty || input.last?.isWhitespace == true ? "" : " ") + text
                #if os(macOS)
                focused = true   // on iPhone this would pop the keyboard over what was just said
                #endif
            }
            if voice.canSpeak {
                Button { talking = true } label: { Image(systemName: "waveform").foregroundStyle(Color.inkSoft).composerFace(control) }
                    .buttonStyle(PressStyle()).help("语音对话：你说，Jurvis 读出回答").accessibilityLabel("语音对话")
            }
        }
    }

    @ViewBuilder var sendButton: some View {
        if let job = store.chatJob {
            let stopping = store.isCancelling(job)
            Button { Task { await store.cancel(job) } } label: {
                Group {
                    if stopping { BusyRing(size: 15) }
                    else { RoundedRectangle(cornerRadius: 2.5).fill(Color.inkReversed).frame(width: 10, height: 10) }
                }
                .sendFace(Color.ink, size: control)
            }
            .buttonStyle(PressStyle()).disabled(stopping).help(stopping ? "正在停止" : "停止").accessibilityLabel(stopping ? "正在停止" : "停止回复")
        } else {
            Button { Task { await send() } } label: {
                Group { if sending { BusyRing(size: 14) } else { Image(systemName: "arrow.up").font(.system(size: 14, weight: .medium)) } }
                    .foregroundStyle(canSend || sending ? Color.inkReversed : Color.inkFaint)
                    .sendFace(canSend || sending ? Color.ink : Color.bubble, size: control)
            }
            .buttonStyle(PressStyle()).disabled(!canSend).keyboardShortcut(.return, modifiers: .command)
            .help("发送（Return）").accessibilityLabel(sending ? "正在发送" : "发送消息")
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

private extension View {
    func sendFace(_ fill: Color, size: CGFloat) -> some View {
        frame(width: size, height: size).background(fill, in: Circle())
            #if os(iOS)
            .frame(minWidth: 44, minHeight: 44)
            #endif
    }
}

/// For hand-drawn buttons: the press answers immediately, whatever the button goes on to do.
struct PressStyle: ButtonStyle {
    func makeBody(configuration: Configuration) -> some View {
        configuration.label.contentShape(Rectangle())
            .scaleEffect(configuration.isPressed ? 0.96 : 1)
            .animation(.easeOut(duration: 0.08), value: configuration.isPressed)
    }
}

/// A pill that is either off (white, hairline) or on (black).
struct Chip: View {
    enum Scale { case regular, large }
    var title: String
    var icon: String
    var on: Bool
    var compact = false
    var busy = false
    var scale: Scale = .regular
    var action: () -> Void
    var height: CGFloat {
        #if os(macOS)
        return scale == .large ? 36 : 32
        #else
        return scale == .large ? 40 : 36
        #endif
    }
    var body: some View {
        let shape = Capsule()
        Button(action: action) {
            Group {
                if busy { HStack(spacing: 6) { BusyRing(); Text(title) } }
                else if compact { Image(systemName: icon).frame(width: 18) }
                else { HStack(spacing: scale == .large ? 8 : 6) { Image(systemName: icon).font(.system(size: 12)); Text(title) } }
            }
            .font(.jCallout).lineLimit(1)
            .padding(.horizontal, compact ? 7 : scale == .large ? 14 : 12).frame(height: height)
            .foregroundStyle(on ? Color.inkReversed : .ink)
            .background(on ? Color.ink : Color.card, in: shape)
            .overlay(shape.strokeBorder(on ? .clear : Color.hairline))
            #if os(iOS)
            .frame(minWidth: 44, minHeight: 44)
            #endif
            .contentShape(Rectangle())
        }
        .buttonStyle(PressStyle())
        .help(title).accessibilityLabel(title)
        .accessibilityAddTraits(on ? .isSelected : [])
    }
}

/// "进行中 · 第 5 / 12 步", in the one green.
struct RunStatusLine: View {
    var text: String
    var body: some View {
        HStack(spacing: 5) {
            Circle().fill(Color.signal).frame(width: 6, height: 6)
            Text(text).lineLimit(1)
        }
        .font(.jMicro).foregroundStyle(Color.signal)
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
                .font(.jBody).lineSpacing(4)
                .padding(.horizontal, 16).padding(.vertical, 10)
                .background(Color.bubble, in: RoundedRectangle(cornerRadius: 18, style: .continuous))
                .contextMenu { Button("复制") { copyText(message.content) } }
        }
        .frame(maxWidth: .infinity, alignment: .trailing)
        .padding(.leading, 48)
    }
}

struct AssistantMessage: View {
    @EnvironmentObject var store: AppStore
    var message: Message
    var open: (Asset) -> Void

    var body: some View {
        let steps = message.steps
        let plan = message.plan
        VStack(alignment: .leading, spacing: 12) {
            if case .object = message.metadata["goal"] { GoalStatus(goal: message.metadata["goal"], live: message.isLive, task: message.metadata["task"]["title"].string) }
            if !plan.isEmpty { PlanList(plan: plan, live: message.isLive) }
            if steps.isEmpty {
                if !message.content.isEmpty { MarkdownText(text: message.content) }
            } else {
                ForEach(Array(steps.enumerated()), id: \.offset) { _, step in
                    // What the assistant says comes as a few short messages, the way a person writes; tool traces sit between them.
                    if !step.text.isEmpty {
                        VStack(alignment: .leading, spacing: 6) {
                            ForEach(Array(Bubbles.split(step.text).enumerated()), id: \.offset) { _, part in said(part) }
                        }
                    }
                    // The question a reply ends on is shown as its options, not as a tool call.
                    let calls = step.calls.filter { $0.name != "ask_user" }
                    if !calls.isEmpty { ToolTrace(messageID: message.id, calls: calls, open: open) }
                }
            }
            if !message.isLive, !message.options.isEmpty {
                OptionList(options: message.options, isLatest: store.messages.last?.id == message.id, answer: answer)
            }
            if !message.isLive, !message.metadata["sources"].array.isEmpty { SourceList(sources: message.metadata["sources"].array) }
            footer(hasContent: !message.content.isEmpty || !steps.isEmpty, words: steps.isEmpty ? message.content : steps.map(\.text).filter { !$0.isEmpty }.joined(separator: "\n\n"))
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    func said(_ text: String) -> some View {
        MarkdownText(text: text)
            .padding(.horizontal, 14).padding(.vertical, 10)
            .background(Color.card, in: RoundedRectangle(cornerRadius: 18, style: .continuous))
            .overlay(RoundedRectangle(cornerRadius: 18, style: .continuous).strokeBorder(Color.hairline))
    }

    /// What the user answered, when this reply ended on a question: the message picked for it later, or the one that follows it.
    var answer: String? {
        if let picked = store.messages.first(where: { $0.isUser && $0.metadata["answer"]["message_id"].string == message.id }) { return picked.content }
        guard let index = store.messages.firstIndex(where: { $0.id == message.id }), index + 1 < store.messages.count,
              store.messages[index + 1].isUser, store.messages[index + 1].session_id == message.session_id else { return nil }
        return store.messages[index + 1].content
    }

    /// `words` is the text the model wrote, which is what the read button reads aloud.
    @ViewBuilder func footer(hasContent: Bool, words: String) -> some View {
        if message.isLive {
            HStack(spacing: 8) {
                SignalDot()
                Text(liveStatus).font(.jSmall).foregroundStyle(Color.inkSoft).lineLimit(2)
                // The composer's stop button belongs to the reply being waited for; background work is stopped here.
                if let job = ownJob, job.isBackground {
                    ActionButton(title: "停止", busyTitle: "正在停止", kind: .ghost, scale: .small, isBusy: store.isCancelling(job)) { await store.cancel(job) }
                }
            }
        } else {
            if !message.errorText.isEmpty {
                Label(message.errorText, systemImage: "exclamationmark.triangle").font(.jSmall).foregroundStyle(Color.danger).textSelection(.enabled)
            }
            if message.status == "cancelled" { Text("已停止").font(.jSmall).foregroundStyle(Color.inkSoft) }
            if message.status == "interrupted" { Text("引擎重启中断了这条回复").font(.jSmall).foregroundStyle(Color.inkSoft) }
            HStack(spacing: 4) {
                if hasContent { CopyButton(text: message.content) }
                if message.status == "complete", !words.isEmpty { ReadAloudButton(id: message.id, text: words) }
                if ["failed", "cancelled", "interrupted"].contains(message.status), store.messages.last?.id == message.id {
                    ActionButton(title: "重试", busyTitle: "正在重试", icon: "arrow.clockwise", kind: .ghost, scale: .small) { await store.retryMessage(message) }
                }
                if message.metadata["truncated"] == .bool(true) {
                    Text("回复达到长度上限").font(.jSmall).foregroundStyle(Color.inkSoft)
                }
            }
            .padding(.leading, -12)
        }
    }

    var ownJob: Job? { store.snapshot?.jobs.first { $0.kind == "chat" && $0.payload["message_id"].string == message.id } }

    var liveStatus: String {
        if message.status == "queued" { return "排队中，前面的任务完成后开始" }
        let moved = message.metadata["background"] == .bool(true) ? " · 已转到后台，做完发在这里，你可以接着聊" : ""
        if let job = ownJob, !job.phase.isEmpty { return job.phase + moved }
        return "正在思考" + moved
    }
}

/// The plan of a goal as rows with round marks: done, the one being worked on, and what is left.
struct PlanList: View {
    var plan: [PlanStep]
    var live: Bool
    var limit: Int? = nil
    #if os(macOS)
    private let rowHeight: CGFloat = 32
    #else
    private let rowHeight: CGFloat = 36
    #endif

    var body: some View {
        let current = live ? plan.firstIndex { !$0.done } : nil
        let rows = Array(plan.enumerated())
        VStack(alignment: .leading, spacing: 0) {
            ForEach(limit.map { Array(rows.prefix($0)) } ?? rows, id: \.offset) { index, item in
                HStack(alignment: .firstTextBaseline, spacing: 10) {
                    StatusMark(state: item.done ? .done : index == current ? .running : .pending)
                        .alignmentGuide(.firstTextBaseline) { $0[.bottom] - 4 }
                    Text(item.step).font(index == current ? .jBody.weight(.medium) : .jBody)
                        .foregroundStyle(item.done || index == current ? Color.ink : .inkSoft)
                        .fixedSize(horizontal: false, vertical: true)
                    Spacer(minLength: 0)
                }
                .padding(.vertical, 6).frame(minHeight: rowHeight)
                .accessibilityElement(children: .combine)
                .accessibilityValue(item.done ? "已完成" : index == current ? "进行中" : "未开始")
            }
        }
    }
}

/// The answers offered with a question. On the latest reply a tap sends that answer; on older ones the list records what was chosen.
struct OptionList: View {
    @EnvironmentObject var store: AppStore
    var options: [String]
    var isLatest: Bool
    var answer: String?
    @State private var sending: String?

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            ForEach(options, id: \.self) { option in
                let chosen = answer == option
                Button {
                    guard sending == nil else { return }
                    sending = option
                    Task {
                        do { try await store.send(option, assets: []) } catch { store.report(error) }
                        sending = nil
                    }
                } label: {
                    HStack(spacing: 12) {
                        Text(option).font(.jBody).multilineTextAlignment(.leading).frame(maxWidth: .infinity, alignment: .leading)
                        if sending == option { ProgressView().controlSize(.small) }
                        else if chosen { Image(systemName: "checkmark").font(.system(size: 12, weight: .semibold)) }
                    }
                    .foregroundStyle(isLatest || chosen ? Color.ink : Color.inkFaint)
                    .padding(.horizontal, 14).padding(.vertical, 10).frame(minHeight: 44)
                    .overlay(RoundedRectangle(cornerRadius: 12, style: .continuous)
                        .strokeBorder(chosen ? Color.stroke : Color.stroke.opacity(isLatest ? 1 : 0.6), style: StrokeStyle(lineWidth: 1, dash: chosen ? [] : [4, 3])))
                    .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
                .disabled(!isLatest || sending != nil)
                .accessibilityAddTraits(chosen ? .isSelected : [])
            }
        }
        .frame(maxWidth: 520, alignment: .leading)
    }
}

// MARK: - Tool trace

/// Each tool call is a small pill; tap one for its arguments and result. A long run folds to its latest calls.
/// What a tool produced for the user (images, videos, code changes) always shows in full below it.
struct ToolTrace: View {
    @EnvironmentObject var store: AppStore
    var messageID: String
    var calls: [ToolCall]
    var open: (Asset) -> Void
    @State private var expanded = false

    /// Commands and anything waiting for a yes are shown as their own cards.
    static func isCard(_ call: ToolCall) -> Bool { call.name == "run_command" || call.awaiting }
    var quiet: [ToolCall] { calls.filter { !Self.isCard($0) && $0.made == nil } }

    var body: some View {
        let quiet = quiet
        let folded = !expanded && quiet.count > 4
        VStack(alignment: .leading, spacing: 10) {
            if !quiet.isEmpty {
                VStack(alignment: .leading, spacing: 6) {
                    if quiet.count > 4 {
                        Button { withAnimation(.easeOut(duration: 0.15)) { expanded.toggle() } } label: {
                            HStack(spacing: 6) {
                                Image(systemName: "chevron.right").font(.system(size: 10)).rotationEffect(.degrees(expanded ? 90 : 0))
                                Text(expanded ? "收起" : "更早的 \(quiet.count - 3) 步").lineLimit(1)
                            }
                            .font(.jSmall).foregroundStyle(Color.inkFaint).frame(minHeight: 28).contentShape(Rectangle())
                        }
                        .buttonStyle(.plain)
                        .accessibilityLabel("工具：\(quiet.count) 步")
                    }
                    ForEach(folded ? Array(quiet.suffix(3)) : quiet) { call in ToolCallRow(call: call, open: open) }
                }
            }
            ForEach(calls) { call in
                if Self.isCard(call) { CommandCard(messageID: messageID, call: call) }
                else if let made = call.made { CardView(card: made) }
                else if call.name == "edit_code", let id = call.job_id { ChangeSetCard(jobID: id) }
                else if let id = call.job_id, let job = store.job(id) { JobInline(job: job, open: open) }
            }
        }
    }
}

/// What a tool call made or found, shown as the thing itself instead of as a tool call.
/// A made thing (goal, schedule, memory, document, task) is one row; a lookup (weather, price, rate, clock, countdown) shows its figures.
struct CardView: View {
    var card: JSONValue
    @EnvironmentObject var store: AppStore
    static let made = ["goal": "scope", "schedule": "calendar.badge.clock", "memory": "bookmark", "file": "doc.text", "task": "checklist", "session": "leaf"]
    /// Where the thing a card stands for lives; a tap goes there.
    static let places: [String: Section] = ["goal": .goals, "schedule": .goals, "memory": .memory, "file": .files, "ledger": .ledger, "expense": .ledger, "health": .health, "cycle": .health, "mood": .health]
    static let glyphs = ["sun": "sun.max.fill", "moon": "moon.stars.fill", "partly": "cloud.sun.fill", "cloud": "cloud.fill", "fog": "cloud.fog.fill",
                         "drizzle": "cloud.drizzle.fill", "rain": "cloud.rain.fill", "snow": "snowflake", "storm": "cloud.bolt.rain.fill",
                         "coin": "bitcoinsign.circle.fill", "exchange": "arrow.left.arrow.right.circle.fill", "calendar": "calendar", "wallet": "list.bullet.rectangle.portrait.fill", "heart": "heart.fill", "drop": "drop.fill"]
    var kind: String { card["kind"].string }
    var trendColor: Color { card["trend"].string == "up" ? .signal : card["trend"].string == "down" ? .danger : .inkFaint }

    var body: some View {
        let content = Group {
            if let symbol = Self.made[kind] { row(symbol) } else { figures }
        }
        .surface(radius: 16)
        .accessibilityElement(children: .combine)
        if kind == "session", !card["session_id"].string.isEmpty {
            // A conversation the assistant opened for what the user is going through: a tap goes there.
            Button { Task { await store.open(stretch: card["session_id"].string) } } label: { content.contentShape(Rectangle()) }.buttonStyle(.plain).accessibilityHint("转到这段对话")
        } else if let place = Self.places[kind] {
            Button { store.section = place } label: { content.contentShape(Rectangle()) }.buttonStyle(.plain).accessibilityHint("打开" + place.rawValue)
        } else {
            content
        }
    }

    func row(_ symbol: String) -> some View {
        HStack(alignment: .top, spacing: 12) {
            Image(systemName: symbol).font(.system(size: 15)).foregroundStyle(Color.inkSoft)
                .frame(width: 32, height: 32)
                .background(Color.bubble, in: RoundedRectangle(cornerRadius: 8, style: .continuous))
            VStack(alignment: .leading, spacing: 2) {
                Text(card["title"].string).font(.jHeading).lineLimit(1)
                if !card["subtitle"].string.isEmpty { Text(card["subtitle"].string).font(.jSmall).foregroundStyle(Color.inkSoft).lineLimit(1) }
                if !card["note"].string.isEmpty { Text(card["note"].string).font(.jCallout).foregroundStyle(Color.inkSoft).lineLimit(2).padding(.top, 2) }
            }
            Spacer(minLength: 0)
            if Self.places[kind] != nil || kind == "session" { Image(systemName: "chevron.right").font(.system(size: 11, weight: .medium)).foregroundStyle(Color.inkFaint).padding(.top, 10) }
        }
        .padding(.horizontal, 14).padding(.vertical, 10)
        .frame(maxWidth: 360, alignment: .leading)
    }

    var figures: some View {
        let shares = card["bars"].array.map { (label: $0["label"].string, value: $0["value"].string, share: $0["share"].number ?? 0) }
        let rows = card["rows"].array
        let days = card["days"].array
        let series = card["series"].array.compactMap(\.number)
        return VStack(alignment: .leading, spacing: 10) {
            HStack(alignment: .top) {
                VStack(alignment: .leading, spacing: 2) {
                    Text(card["title"].string).font(.jHeading).lineLimit(1)
                    if !card["subtitle"].string.isEmpty { Text(card["subtitle"].string).font(.jSmall).foregroundStyle(Color.inkSoft).lineLimit(1) }
                }
                Spacer(minLength: 8)
                if let glyph = Self.glyphs[card["icon"].string] {
                    Image(systemName: glyph).symbolRenderingMode(.multicolor).font(.system(size: 24)).foregroundStyle(Color.inkSoft)
                }
            }
            // The figure and what to wear sit beside the temperature: the picture is the advice at a glance.
            HStack(alignment: .top, spacing: 12) {
              VStack(alignment: .leading, spacing: 10) {
            if !card["value"].string.isEmpty {
                HStack(alignment: .firstTextBaseline, spacing: 6) {
                    Text(card["value"].string).font(.system(size: ["price", "currency", "ledger", "expense", "health", "cycle"].contains(kind) ? 28 : 38, weight: .semibold)).monospacedDigit()
                        .lineLimit(1).minimumScaleFactor(0.6)
                    if kind == "countdown", card["value"].string != "今天" { Text("天").font(.jBody).foregroundStyle(Color.inkSoft) }
                }
            }
            if !card["outfit"]["wear"].array.isEmpty {
                VStack(alignment: .leading, spacing: 6) {
                    FlowChips(items: card["outfit"]["wear"].array.map(\.string))
                    if !card["outfit"]["tip"].string.isEmpty { Text(card["outfit"]["tip"].string).font(.jCallout).fixedSize(horizontal: false, vertical: true) }
                }
            }
              }
              .frame(maxWidth: .infinity, alignment: .leading)
              if kind == "weather" { art }
            }
            if series.count > 2 { Sparkline(points: series).stroke(trendColor, style: StrokeStyle(lineWidth: 1.5, lineJoin: .round)).frame(height: 34) }
            if !shares.isEmpty { ShareBars(items: shares) }
            if !rows.isEmpty {
                LazyVGrid(columns: [GridItem(.flexible(), spacing: 16), GridItem(.flexible())], alignment: .leading, spacing: 6) {
                    ForEach(Array(rows.enumerated()), id: \.offset) { _, item in
                        HStack(spacing: 8) {
                            if !item["label"].string.isEmpty { Text(item["label"].string).foregroundStyle(Color.inkFaint) }
                            Spacer(minLength: 0)
                            Text(item["value"].string).monospacedDigit().lineLimit(1).minimumScaleFactor(0.7)
                        }
                        .font(.jCallout)
                    }
                }
            }
            if !days.isEmpty {
                Divider().overlay(Color.hairline)
                HStack(spacing: 4) {
                    ForEach(Array(days.enumerated()), id: \.offset) { _, day in
                        VStack(spacing: 3) {
                            Text(day["label"].string).font(.jSmall).foregroundStyle(Color.inkSoft)
                            Image(systemName: Self.glyphs[day["icon"].string] ?? "cloud.fill").symbolRenderingMode(.multicolor).font(.system(size: 15)).foregroundStyle(Color.inkSoft)
                            (Text("\(Int(day["high"].number ?? 0))° ") + Text("\(Int(day["low"].number ?? 0))°").foregroundColor(.inkFaint)).font(.jSmall).monospacedDigit()
                        }
                        .frame(maxWidth: .infinity)
                    }
                }
            }
            if !card["note"].string.isEmpty { Text(card["note"].string).font(.jSmall).foregroundStyle(Color.inkFaint).lineLimit(1) }
        }
        .padding(.horizontal, 16).padding(.vertical, 14)
        .frame(maxWidth: 360, alignment: .leading)
    }
}

extension CardView {
    /// The outfit illustration of a weather card: the picture once it is drawn, a quiet note while it is being drawn.
    @ViewBuilder var art: some View {
        let job = store.job(card["image_job"].string.isEmpty ? nil : card["image_job"].string)
        let drawn = card["image"].string.isEmpty ? job.flatMap { store.outputs(of: $0).first } : store.asset(card["image"].string)
        if let drawn {
            // The drawing is a square with the figure in its middle: the frame keeps the figure and trims the empty sides.
            MediaThumb(asset: drawn, fill: true).frame(width: 116, height: 164)
        } else if let job, job.isActive {
            VStack(spacing: 8) {
                BusyRing(size: 14)
                Text("正在画穿搭").font(.jSmall).foregroundStyle(Color.inkSoft)
            }
            .frame(width: 116, height: 164).background(Color.bubble, in: RoundedRectangle(cornerRadius: 10, style: .continuous))
        }
    }
}

/// Short labels that wrap onto as many lines as they need.
struct FlowChips: View {
    var items: [String]
    var body: some View {
        FlowLayout(spacing: 6) {
            ForEach(items, id: \.self) { item in
                Text(item).font(.jSmall).padding(.horizontal, 10).frame(height: 24).background(Color.bubble, in: Capsule())
            }
        }
    }
}

struct FlowLayout: Layout {
    var spacing: CGFloat = 6
    func sizeThatFits(proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) -> CGSize {
        arrange(width: proposal.width ?? .infinity, subviews: subviews).size
    }
    func placeSubviews(in bounds: CGRect, proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) {
        for (subview, point) in zip(subviews, arrange(width: bounds.width, subviews: subviews).points) {
            subview.place(at: CGPoint(x: bounds.minX + point.x, y: bounds.minY + point.y), proposal: .unspecified)
        }
    }
    func arrange(width: CGFloat, subviews: Subviews) -> (size: CGSize, points: [CGPoint]) {
        var points: [CGPoint] = [], x: CGFloat = 0, y: CGFloat = 0, row: CGFloat = 0, widest: CGFloat = 0
        for subview in subviews {
            let size = subview.sizeThatFits(.unspecified)
            if x > 0, x + size.width > width { x = 0; y += row + spacing; row = 0 }
            points.append(CGPoint(x: x, y: y))
            x += size.width + spacing
            row = max(row, size.height)
            widest = max(widest, x - spacing)
        }
        return (CGSize(width: widest, height: y + row), points)
    }
}

/// A week of prices as one line, scaled to its own low and high.
struct Sparkline: Shape {
    var points: [Double]
    func path(in rect: CGRect) -> Path {
        var path = Path()
        guard points.count > 1, let low = points.min(), let high = points.max() else { return path }
        let span = high - low == 0 ? 1 : high - low
        for (index, value) in points.enumerated() {
            let point = CGPoint(x: rect.minX + rect.width * CGFloat(index) / CGFloat(points.count - 1),
                                y: rect.maxY - 1 - (rect.height - 2) * CGFloat((value - low) / span))
            if index == 0 { path.move(to: point) } else { path.addLine(to: point) }
        }
        return path
    }
}

/// Something the assistant wants to do that needs a yes: the user sees exactly what and why, approves it, then watches the output.
struct CommandCard: View {
    @EnvironmentObject var store: AppStore
    var messageID: String
    var call: ToolCall
    @State private var showOutput = false
    /// What the user pressed; stays until the call leaves `awaiting`, which can be after the request returns.
    @State private var decision: String?

    var lines: [Substring] { (call.console ?? "").split(separator: "\n", omittingEmptySubsequences: false) }
    var isCommand: Bool { call.name == "run_command" }
    var reason: String { call.reason?.isEmpty == false ? call.reason! : isCommand ? "运行命令" : call.label }

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack(spacing: 10) {
                IconWell(icon: isCommand ? "terminal" : ToolCallRow.icon(call.name))
                Text(call.awaiting ? (isCommand ? "Jurvis 想运行一条命令" : "Jurvis 想\(call.label)") : reason).font(.jHeading).lineLimit(2)
                Spacer(minLength: 6)
                status
            }
            Text(call.command ?? "").font(.jMono).textSelection(.enabled)
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(.horizontal, 12).padding(.vertical, 10)
                .background(Color.subtleFill, in: RoundedRectangle(cornerRadius: 10, style: .continuous))
                .overlay(RoundedRectangle(cornerRadius: 10, style: .continuous).strokeBorder(Color.hairline))
            if call.awaiting, isCommand { Text(reason).font(.jCallout).foregroundStyle(Color.inkSoft).fixedSize(horizontal: false, vertical: true) }
            if let cwd = call.cwd, !cwd.isEmpty { Text("在 " + cwd).font(.jMicro).foregroundStyle(Color.inkFaint).lineLimit(1).truncationMode(.middle) }
            if call.awaiting { approval }
            if call.isRunning, !(call.console ?? "").isEmpty { console(lines.suffix(8).joined(separator: "\n")) }
            if !call.isRunning, !call.awaiting, !(call.console ?? "").isEmpty {
                Button { withAnimation(.easeOut(duration: 0.15)) { showOutput.toggle() } } label: {
                    Label(showOutput ? "收起输出" : "查看输出（\(lines.count) 行）", systemImage: showOutput ? "chevron.down" : "chevron.right")
                        .labelStyle(.titleAndIcon)
                }
                .buttonStyle(.jurvis(.ghost, .small)).padding(.leading, -12)
                if showOutput { console(call.console ?? "") }
            }
        }
        .padding(16)
        .surface(radius: 18, edge: .stroke, lifted: call.awaiting)
        .accessibilityElement(children: .contain)
        .accessibilityLabel(call.awaiting ? "需要你确认" : reason)
    }

    @ViewBuilder var status: some View {
        if call.awaiting { Text("等你确认").font(.jSmall).foregroundStyle(Color.signal).fixedSize() }
        else if call.isRunning { HStack(spacing: 6) { SignalDot(); Text("运行中").font(.jSmall).foregroundStyle(Color.signal) }.fixedSize() }
        else if let summary = call.summary {
            let problem = call.failed || call.status == "skipped"
            HStack(spacing: 6) {
                if !problem { StatusMark(state: .done, size: 14) }
                Text(summary).font(.jSmall).foregroundStyle(problem ? Color.danger : Color.inkSoft).lineLimit(1)
            }
        }
    }

    var approval: some View {
        ViewThatFits(in: .horizontal) {
            HStack(spacing: 8) {
                decisionButton("run", title: "允许", busy: "运行中", icon: "checkmark", kind: .primary)
                decisionButton("always", title: "本任务内都允许", busy: "运行中", icon: nil, kind: .secondary)
                    .help("之后这个任务里的命令直接运行；删除、sudo、git push 等仍会询问")
                decisionButton("deny", title: "拒绝", busy: "正在拒绝", icon: "xmark", kind: .ghost)
                Spacer(minLength: 0)
            }
            VStack(spacing: 8) {
                HStack(spacing: 8) {
                    decisionButton("run", title: "允许", busy: "运行中", icon: "checkmark", kind: .primary, fill: true)
                    decisionButton("deny", title: "拒绝", busy: "正在拒绝", icon: "xmark", kind: .secondary, fill: true)
                }
                decisionButton("always", title: "本任务内都允许", busy: "运行中", icon: nil, kind: .ghost, fill: true)
            }
        }
    }

    func decisionButton(_ choice: String, title: String, busy: String, icon: String?, kind: ButtonKind, fill: Bool = false) -> some View {
        ActionButton(title: title, busyTitle: busy, icon: icon, kind: kind, scale: .regular, fill: fill, isBusy: decision == choice) {
            decision = choice
            if await !store.decideCommand(message: messageID, call: call.id, choice) { decision = nil }
        }
        .disabled(decision != nil && decision != choice)
    }

    func console(_ text: String) -> some View {
        ScrollView([.horizontal, .vertical]) {
            Text(text).font(.jMono).textSelection(.enabled)
                .frame(maxWidth: .infinity, alignment: .leading).padding(10)
        }
        .frame(maxHeight: 260)
        .background(Color.subtleFill, in: RoundedRectangle(cornerRadius: 10, style: .continuous))
        .overlay(RoundedRectangle(cornerRadius: 10, style: .continuous).strokeBorder(Color.hairline))
    }
}

/// Where a goal stands: running, done (checked by a separate review), waiting for the user, or stopped.
struct GoalStatus: View {
    var goal: JSONValue
    var live: Bool
    /// The title of the background task this message carries; empty for a goal the user set.
    var task = ""
    var name: String { task.isEmpty ? "目标" : "后台任务「\(task)」" }

    var body: some View {
        let status = goal["status"].string
        if live {
            Badge(text: name + " · " + label(status), tone: .active)
        } else {
            HStack(alignment: .firstTextBaseline, spacing: 7) {
                Group {
                    if status == "done" { StatusMark(state: .done, size: 16) }
                    else if status == "blocked" { StatusMark(state: .waiting, size: 16) }
                    else { Image(systemName: icon(status)).font(.system(size: 13)).foregroundStyle(Color.inkSoft) }
                }
                .alignmentGuide(.firstTextBaseline) { $0[.bottom] - 3 }
                Text(name).font(.jSmall.weight(.medium))
                Text(label(status)).font(.jSmall).foregroundStyle(status == "blocked" ? Color.signal : Color.inkSoft).lineLimit(2)
            }
        }
    }

    func icon(_ status: String) -> String {
        ["unverified": "questionmark.circle", "incomplete": "pause.circle"][status] ?? "scope"
    }

    func label(_ status: String) -> String {
        let steps = Int(goal["steps"].number ?? 0)
        let missing = goal["missing"].string
        switch status {
        case "done": return "已完成 · 已复核"
        case "blocked": return "需要你回复"
        case "unverified": return "已结束 · 未能确认完成" + (missing.isEmpty ? "" : "：" + missing)
        case "incomplete": return "未完成 · 用了 \(steps) 步"
        default:
            let base = live ? "进行中 · 第 \(steps) 步" : "已中断"
            return missing.isEmpty ? base : base + " · 复核：" + missing
        }
    }
}

/// One tool call as a pill: icon, what it did, the file or query in monospace, and how it went.
struct ToolCallRow: View {
    @EnvironmentObject var store: AppStore
    var call: ToolCall
    var open: (Asset) -> Void
    @State private var expanded = false

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Button { withAnimation(.easeOut(duration: 0.15)) { expanded.toggle() } } label: {
                HStack(spacing: 8) {
                    if call.isRunning { StatusMark(state: .running, size: 14) }
                    else { Image(systemName: ToolCallRow.icon(call.name)).font(.system(size: 12)).frame(width: 14) }
                    Text(call.label).fixedSize()
                    if !argumentPreview.isEmpty {
                        Text(argumentPreview).font(.jMono).foregroundStyle(Color.ink).lineLimit(1).truncationMode(.middle).layoutPriority(1)
                    }
                    if let summary = call.summary, !call.isRunning {
                        Text(summary).font(.jSmall).foregroundStyle(call.failed ? Color.danger : Color.inkFaint).lineLimit(1)
                    }
                }
                .font(.jCallout).foregroundStyle(Color.inkSoft)
                .padding(.leading, 10).padding(.trailing, 12).frame(minHeight: 30)
                .background(Color.subtleFill, in: Capsule())
                .overlay(Capsule().strokeBorder(Color.hairline))
                #if os(iOS)
                .frame(minHeight: 44)
                #endif
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .accessibilityLabel("\(call.label)，\(call.summary ?? call.status)")

            if expanded {
                details.padding(.leading, 14)
                    .overlay(alignment: .leading) { Rectangle().fill(Color.hairline).frame(width: 1) }
                    .padding(.leading, 8)
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
            if let duration = call.duration { Text(String(format: "耗时 %.2f 秒", duration)).font(.jMicro).foregroundStyle(Color.inkFaint) }
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
        case "save_file": return "doc.badge.plus"
        case "append_file": return "text.append"
        case "edit_file": return "pencil.line"
        case "update_plan": return "checklist"
        case "recall": return "magnifyingglass"
        case "remember": return "brain"
        case "create_image": return "photo"
        case "create_video": return "film"
        case "create_drama": return "film.stack"
        case "job_status": return "checklist"
        case "code_search": return "text.magnifyingglass"
        case "code_propose", "edit_code": return "wand.and.stars"
        case "home_control": return "house"
        case "web_search": return "globe"
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
                if job.isActive { Text(job.phase).font(.jSmall).foregroundStyle(Color.inkSoft).lineLimit(1) }
                if let error = job.error { Text(error).font(.jSmall).foregroundStyle(Color.danger).lineLimit(2) }
                Spacer()
                if job.isActive {
                    ActionButton(title: "取消", busyTitle: "正在停止", kind: .ghost, scale: .small, isBusy: store.isCancelling(job)) { await store.cancel(job) }
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
        case "plan": Button { store.section = .drama } label: { Label("打开短剧", systemImage: "film.stack") }.buttonStyle(.jurvis(.ghost, .small))
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
                case .paragraph(let s): inline(s).font(.jBody).lineSpacing(6)
                case .heading(let level, let s): inline(s, font: level == 1 ? .jSection : .jHeading).font(level == 1 ? .jSection : .jHeading).padding(.top, 4)
                case .bullet(let marker, let s):
                    HStack(alignment: .firstTextBaseline, spacing: 8) {
                        Text(marker).font(.jBody).foregroundStyle(Color.inkSoft).monospacedDigit()
                        inline(s).font(.jBody).lineSpacing(4)
                    }
                case .quote(let s):
                    inline(s).font(.jBody).foregroundStyle(Color.inkSoft).padding(.leading, 10)
                        .overlay(alignment: .leading) { Rectangle().fill(Color.hairline).frame(width: 3) }
                case .code(let lang, let s): CodeBlock(title: lang.isEmpty ? nil : lang, text: s)
                case .rule: Divider()
                }
            }
        }
        .textSelection(.enabled)
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    /// Strong text is set in medium: the app has no bold.
    func inline(_ s: String, font: Font = .jBody) -> Text {
        let options = AttributedString.MarkdownParsingOptions(interpretedSyntax: .inlineOnlyPreservingWhitespace)
        var text = (try? AttributedString(markdown: s, options: options)) ?? AttributedString(s)
        let strong = text.runs.filter { $0.inlinePresentationIntent?.contains(.stronglyEmphasized) ?? false }.map(\.range)
        for range in strong {
            text[range].inlinePresentationIntent = text[range].inlinePresentationIntent?.subtracting(.stronglyEmphasized)
            text[range].font = font.weight(.medium)
        }
        return Text(text)
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
                Text(title ?? "").font(.jMicro).foregroundStyle(Color.inkSoft)
                Spacer()
                CopyButton(text: text, iconOnly: true)
            }
            .padding(.leading, 10).padding(.trailing, 2).padding(.vertical, 1)
            Divider()
            ScrollView(.horizontal) {
                Text(text).font(.jMono).textSelection(.enabled).padding(10)
            }
            .frame(maxHeight: 320)
        }
        .background(Color.subtleFill, in: RoundedRectangle(cornerRadius: 10, style: .continuous))
        .overlay(RoundedRectangle(cornerRadius: 10, style: .continuous).strokeBorder(Color.hairline))
    }
}

/// Images on the clipboard as upload-ready bytes: copied image files first, then raw image data.
/// Returns nil when the clipboard holds text, so a normal text paste is left alone.
enum ClipboardImages {
    static func read() -> [(Data, String)]? {
        #if os(macOS)
        let board = NSPasteboard.general
        if let urls = board.readObjects(forClasses: [NSURL.self], options: [.urlReadingFileURLsOnly: true]) as? [URL] {
            let images = urls.filter { (UTType(filenameExtension: $0.pathExtension)?.conforms(to: .image)) ?? false }
            if !images.isEmpty { return images.compactMap { url in (try? Data(contentsOf: url)).map { ($0, url.lastPathComponent) } } }
        }
        if board.string(forType: .string) != nil { return nil }
        if let png = board.data(forType: .png) { return [(png, "粘贴的图片.png")] }
        guard let image = NSImage(pasteboard: board), let tiff = image.tiffRepresentation,
              let png = NSBitmapImageRep(data: tiff)?.representation(using: .png, properties: [:]) else { return nil }
        return [(png, "粘贴的图片.png")]
        #else
        let board = UIPasteboard.general
        guard board.hasImages else { return nil }
        return (board.images ?? []).compactMap { $0.pngData() }.map { ($0, "粘贴的图片.png") }
        #endif
    }
}

extension NSItemProvider {
    func loadImageData() async throws -> Data {
        try await withCheckedThrowingContinuation { done in
            _ = loadDataRepresentation(for: .image) { data, error in
                if let data { done.resume(returning: data) } else { done.resume(throwing: error ?? CocoaError(.fileReadUnknown)) }
            }
        }
    }
}

/// Where a stretch of the conversation begins: the time, in the middle, like a pause between two talks.
struct StretchMark: View {
    var time: Double
    var body: some View {
        Text(Self.label(time)).font(.jSmall).foregroundStyle(Color.inkFaint)
            .frame(maxWidth: .infinity).padding(.top, 6)
            .accessibilityLabel("对话时间 " + Self.label(time))
    }
    private static let clock: DateFormatter = { let f = DateFormatter(); f.dateFormat = "HH:mm"; return f }()
    static func label(_ time: Double) -> String {
        let date = Date(timeIntervalSince1970: time), calendar = Calendar.current
        let clock = Self.clock.string(from: date)
        if calendar.isDateInToday(date) { return clock }
        if calendar.isDateInYesterday(date) { return "昨天 " + clock }
        let sameYear = calendar.component(.year, from: date) == calendar.component(.year, from: Date())
        return (sameYear ? "" : "\(calendar.component(.year, from: date))年") + "\(calendar.component(.month, from: date))月\(calendar.component(.day, from: date))日 " + clock
    }
}

/// A reply's text as the few messages it would be in a chat: split between plain paragraphs. Lists, tables and quotes stay with
/// the paragraph that leads into them; long or structured writing (code, a document) stays whole.
enum Bubbles {
    static func plain(_ part: String) -> Bool {
        !part.split(separator: "\n", omittingEmptySubsequences: false).contains { line in
            let text = line.drop { $0 == " " || $0 == "\t" }
            if line.hasPrefix("    ") && !text.isEmpty { return true }
            if let first = text.first, "#>|".contains(first) { return true }
            if let first = text.first, "-*+•".contains(first), text.dropFirst().first == " " { return true }
            let digits = text.prefix { $0.isNumber }
            if !digits.isEmpty, let mark = text.dropFirst(digits.count).first, ".)、".contains(mark) { return true }
            return false
        }
    }
    static func split(_ text: String) -> [String] {
        guard text.count <= 1500, !text.contains("```"), !text.contains("~~~") else { return [text] }
        var parts: [String] = [], current: [Substring] = []
        for line in text.split(separator: "\n", omittingEmptySubsequences: false) {
            if line.allSatisfy(\.isWhitespace) {
                if !current.isEmpty { parts.append(current.joined(separator: "\n")); current = [] }
            } else { current.append(line) }
        }
        if !current.isEmpty { parts.append(current.joined(separator: "\n")) }
        var out: [String] = [], before = true
        for part in parts {
            let now = plain(part)
            if !out.isEmpty && !(now && before) { out[out.count - 1] += "\n\n" + part } else { out.append(part) }
            before = now
        }
        while out.count > 4 { let tail = out.removeLast(); out[out.count - 1] += "\n\n" + tail }
        return out.isEmpty ? [text] : out
    }
}

/// What Jurvis asked and the user has not decided yet, one question at a time, above the composer wherever they are in the conversation.
struct WaitingQuestions: View {
    @EnvironmentObject var store: AppStore
    @State private var index = 0
    @State private var sending: String?
    #if os(macOS)
    private let tap: CGFloat = 32
    #else
    private let tap: CGFloat = 44
    #endif

    var body: some View {
        let items = store.waitingQuestions
        if !items.isEmpty {
            let at = min(index, items.count - 1)
            let asked = items[at]
            VStack(alignment: .leading, spacing: 8) {
                HStack(spacing: 10) {
                    Text(items.count > 1 ? "等你决定 · \(at + 1)/\(items.count)" : "等你决定").font(.jSmall).foregroundStyle(Color.signal)
                    Spacer(minLength: 8)
                    if items.count > 1 {
                        Button("下一个") { index = (at + 1) % items.count }.buttonStyle(.plain).font(.jSmall).foregroundStyle(Color.inkSoft)
                    }
                    Button { Task { await store.dropQuestion(asked.id) } } label: {
                        Image(systemName: "xmark").font(.system(size: 11, weight: .medium)).foregroundStyle(Color.inkSoft)
                            .frame(width: tap, height: 28).contentShape(Rectangle())
                    }
                    .buttonStyle(.plain).help("不用决定了").accessibilityLabel("不用决定了")
                }
                Button { Task { await store.open(message: asked.message_id) } } label: {
                    Text(asked.question).font(.jBody).foregroundStyle(Color.ink).multilineTextAlignment(.leading).frame(maxWidth: .infinity, alignment: .leading)
                        .contentShape(Rectangle())
                }
                .buttonStyle(.plain).accessibilityHint("转到当时说到的地方")
                ScrollView(.horizontal, showsIndicators: false) {
                    HStack(spacing: 8) {
                        ForEach(asked.options, id: \.self) { option in
                            Button {
                                guard sending == nil else { return }
                                sending = option
                                Task {
                                    do { try await store.send(option, assets: [], answerTo: asked.id) } catch { store.report(error) }
                                    sending = nil
                                }
                            } label: {
                                HStack(spacing: 6) {
                                    Text(option).font(.jCallout).lineLimit(1)
                                    if sending == option { ProgressView().controlSize(.small) }
                                }
                                .foregroundStyle(Color.ink).padding(.horizontal, 12).frame(minHeight: tap)
                                .overlay(Capsule().strokeBorder(Color.stroke, style: StrokeStyle(lineWidth: 1, dash: [4, 3])))
                                .contentShape(Capsule())
                            }
                            .buttonStyle(.plain).disabled(sending != nil || store.chatBusy)
                        }
                    }
                    .padding(.vertical, 1)
                }
            }
            .padding(.horizontal, 14).padding(.vertical, 10)
            .background(Color.card, in: RoundedRectangle(cornerRadius: 14, style: .continuous))
            .overlay(RoundedRectangle(cornerRadius: 14, style: .continuous).strokeBorder(Color.hairline))
        }
    }
}
