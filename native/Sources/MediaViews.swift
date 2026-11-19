import SwiftUI
import AVKit
import UniformTypeIdentifiers

// MARK: - Media

/// Native AVKit player. SwiftUI's `VideoPlayer` aborts during type-metadata setup on
/// macOS 27 (`_AVKit_SwiftUI` cannot resolve its `AVPlayerView` superclass), so the
/// platform player view is wrapped directly instead.
#if os(macOS)
struct PlayerView: NSViewRepresentable {
    let player: AVPlayer
    func makeNSView(context: Context) -> AVPlayerView {
        let view = AVPlayerView()
        view.player = player
        view.controlsStyle = .inline
        view.showsFullScreenToggleButton = true
        return view
    }
    func updateNSView(_ view: AVPlayerView, context: Context) { if view.player !== player { view.player = player } }
}
#else
struct PlayerView: UIViewControllerRepresentable {
    let player: AVPlayer
    func makeUIViewController(context: Context) -> AVPlayerViewController {
        let controller = AVPlayerViewController()
        controller.player = player
        return controller
    }
    func updateUIViewController(_ controller: AVPlayerViewController, context: Context) { if controller.player !== player { controller.player = player } }
}
#endif

struct MediaThumb: View {
    @EnvironmentObject var store: AppStore
    var asset: Asset
    var fill = true
    @State private var image: Image?
    @State private var loading = true
    var body: some View {
        // The image lives in an overlay so it can never change the thumbnail's layout size.
        Color.subtleFill
            .overlay {
                if let image {
                    image.resizable().aspectRatio(contentMode: fill ? .fill : .fit)
                } else if loading {
                    BusyRing(size: 16)
                } else {
                    Image(systemName: asset.isVideo ? "film" : "photo").font(.system(size: 22, weight: .light)).foregroundStyle(Color.inkFaint)
                }
            }
            .overlay(alignment: .bottomLeading) {
                if asset.isVideo {
                    Image(systemName: "play.circle.fill").font(.system(size: 20)).symbolRenderingMode(.palette).foregroundStyle(.white, .black.opacity(0.45)).padding(6)
                }
            }
            .clipShape(RoundedRectangle(cornerRadius: 10, style: .continuous))
            .overlay(RoundedRectangle(cornerRadius: 10, style: .continuous).strokeBorder(Color.hairline))
            .task(id: asset.id) { loading = true; image = await store.thumbnail(asset); loading = false }
            .accessibilityLabel(asset.name)
    }
}

/// The full-window viewer for every picture and clip in the app: original resolution, zoom, and
/// stepping through the set it was opened from. `RootView` hosts it above everything else.
struct MediaViewer: View {
    @EnvironmentObject var store: AppStore
    var viewing: MediaViewing
    @State private var zoom: CGFloat = 1
    @State private var details = false
    @State private var url: URL?
    #if os(macOS)
    @State private var keys: Any?
    #endif
    var asset: Asset { viewing.current }

    var body: some View {
        GeometryReader { geometry in
            let wide = geometry.size.width > 760
            HStack(spacing: 0) {
                VStack(spacing: 0) {
                    bar
                    stage.frame(maxWidth: .infinity, maxHeight: .infinity).clipped()
                    if details && !wide { info.frame(maxHeight: 240) }
                }
                if details && wide { info.frame(width: 300) }
            }
        }
        .background(Color.stage.ignoresSafeArea())
        .environment(\.colorScheme, .dark)
        .foregroundStyle(Color.ink)
        .task(id: asset.id) {
            zoom = 1; url = nil
            do { url = try await store.mediaURL(asset) } catch { store.report(error) }
        }
        #if os(macOS)
        .onAppear { keys = NSEvent.addLocalMonitorForEvents(matching: .keyDown) { press($0) ? nil : $0 } }
        .onDisappear { if let keys { NSEvent.removeMonitor(keys) } }
        #endif
    }

    var bar: some View {
        HStack(spacing: 6) {
            Button { store.viewing = nil } label: { Image(systemName: "xmark") }.buttonStyle(.jurvisIcon(.ghost, .regular)).help("关闭（Esc）").accessibilityLabel("关闭")
            Text(asset.name).font(.jHeading).lineLimit(1)
            if viewing.assets.count > 1 {
                Text("\(viewing.index + 1) / \(viewing.assets.count)").font(.jSmall.monospacedDigit()).foregroundStyle(Color.inkSoft).fixedSize()
            }
            Spacer(minLength: 8)
            if !asset.isVideo {
                Button { setZoom(zoom / 1.5) } label: { Image(systemName: "minus.magnifyingglass") }.buttonStyle(.jurvisIcon(.ghost, .regular)).disabled(zoom <= 1).help("缩小").accessibilityLabel("缩小")
                Button { setZoom(1) } label: { Text("\(Int(zoom * 100))%").font(.jSmall.monospacedDigit()).frame(width: 40) }.buttonStyle(.plain).foregroundStyle(Color.inkSoft).help("恢复适合大小").accessibilityLabel("恢复适合大小")
                Button { setZoom(zoom * 1.5) } label: { Image(systemName: "plus.magnifyingglass") }.buttonStyle(.jurvisIcon(.ghost, .regular)).disabled(zoom >= ZoomableImage.limit).help("放大").accessibilityLabel("放大")
            }
            Button { details.toggle() } label: { Image(systemName: details ? "info.circle.fill" : "info.circle") }.buttonStyle(.jurvisIcon(.ghost, .regular)).help("详情").accessibilityLabel("详情")
            if let url { ShareLink(item: url) { Image(systemName: "square.and.arrow.up") }.buttonStyle(.jurvisIcon(.ghost, .regular)).help("导出").accessibilityLabel("导出") }
        }
        .padding(.horizontal, 12).padding(.vertical, 8)
    }

    var stage: some View {
        ZStack {
            // A click on the empty stage closes, like clicking outside a sheet.
            Color.clear.contentShape(Rectangle()).onTapGesture { store.viewing = nil }
            Group {
                if asset.isVideo { ClipStage(asset: asset, url: url) }
                else { ZoomableImage(asset: asset, zoom: $zoom) { store.stepViewing($0) } }
            }
            .id(asset.id)
            .padding(.horizontal, 16).padding(.bottom, 16)
            if viewing.assets.count > 1, zoom == 1 {
                HStack {
                    step(-1, icon: "chevron.left", title: "上一个").opacity(viewing.index > 0 ? 1 : 0)
                    Spacer()
                    step(1, icon: "chevron.right", title: "下一个").opacity(viewing.index < viewing.assets.count - 1 ? 1 : 0)
                }
                .padding(.horizontal, 16)
            }
        }
    }

    func step(_ by: Int, icon: String, title: String) -> some View {
        Button { store.stepViewing(by) } label: { Image(systemName: icon) }.buttonStyle(.jurvisIcon(.secondary, .large)).help(title).accessibilityLabel(title)
    }

    var info: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 10) {
                Text(asset.name).font(.jHeading).textSelection(.enabled)
                HStack(spacing: 10) {
                    Text(asset.size.fileSize)
                    Text(asset.created_at.relativeDate)
                    if case .number(let w) = asset.metadata["width"], case .number(let h) = asset.metadata["height"] { Text("\(Int(w))×\(Int(h))") }
                }.font(.jSmall).foregroundStyle(Color.inkSoft)
                if !asset.metadata["prompt"].string.isEmpty {
                    Text(asset.metadata["prompt"].string).font(.jCallout).foregroundStyle(Color.inkSoft).lineSpacing(3).textSelection(.enabled)
                }
                if asset.isVideo {
                    ActionButton(title: "提取首尾帧", busyTitle: "正在提交", icon: "rectangle.split.2x1", scale: .small) {
                        _ = try await store.submit("/api/assets/\(asset.id)/frames", body: [:]); store.viewing = nil
                    }.help("把首尾帧存为图片，用于续拍下一个镜头")
                }
            }
            .padding(16).frame(maxWidth: .infinity, alignment: .leading)
        }
        .background(Color.card)
    }

    func setZoom(_ value: CGFloat) { withAnimation(.easeOut(duration: 0.15)) { zoom = min(max(value, 1), ZoomableImage.limit) } }

    #if os(macOS)
    /// Handled here rather than with shortcuts, so they work while a text field behind the viewer has focus.
    func press(_ event: NSEvent) -> Bool {
        switch event.keyCode {
        case 53: store.viewing = nil
        case 123: store.stepViewing(-1)
        case 124: store.stepViewing(1)
        default:
            switch event.charactersIgnoringModifiers {
            case "=", "+": if asset.isVideo { return false }; setZoom(zoom * 1.5)
            case "-": if asset.isVideo { return false }; setZoom(zoom / 1.5)
            case "0": if asset.isVideo { return false }; setZoom(1)
            default: return false
            }
        }
        return true
    }
    #endif
}

/// A picture at its original resolution. Pinch or double-click to zoom, drag to pan; swiping an unzoomed picture steps.
struct ZoomableImage: View {
    static let limit: CGFloat = 6
    @EnvironmentObject var store: AppStore
    var asset: Asset
    @Binding var zoom: CGFloat
    var swipe: (Int) -> Void
    @State private var picture: CGImage?
    @State private var failed = false
    @State private var pinch: CGFloat = 1
    @State private var offset = CGSize.zero
    @State private var drag = CGSize.zero

    var body: some View {
        GeometryReader { geometry in
            if let picture {
                let scale = min(max(zoom * pinch, 1), Self.limit)
                let fit = min(geometry.size.width / CGFloat(picture.width), geometry.size.height / CGFloat(picture.height))
                let size = CGSize(width: CGFloat(picture.width) * fit, height: CGFloat(picture.height) * fit)
                let shown = clamped(CGSize(width: offset.width + drag.width, height: offset.height + drag.height), size: size, scale: scale, in: geometry.size)
                Image(decorative: picture, scale: 1).resizable().interpolation(.high)
                    .frame(width: size.width, height: size.height)
                    .scaleEffect(scale)
                    .offset(shown)
                    .gesture(panning(size: size, in: geometry.size))
                    .simultaneousGesture(pinching)
                    .onTapGesture(count: 2) { withAnimation(.easeOut(duration: 0.18)) { zoom = zoom > 1 ? 1 : 2.5; offset = .zero } }
                    .position(x: geometry.size.width / 2, y: geometry.size.height / 2)
                    .accessibilityLabel(asset.name)
                    .onChange(of: zoom) { _, value in
                        offset = value <= 1 ? .zero : clamped(offset, size: size, scale: value, in: geometry.size)
                    }
            } else if failed {
                Image(systemName: "photo").font(.system(size: 28, weight: .light)).foregroundStyle(Color.inkFaint).frame(maxWidth: .infinity, maxHeight: .infinity).allowsHitTesting(false)
            } else {
                BusyRing(size: 20).frame(maxWidth: .infinity, maxHeight: .infinity).allowsHitTesting(false)
            }
        }
        .task { picture = await store.fullImage(asset); failed = picture == nil }
    }

    /// The picture may slide only as far as it overflows the stage, so it can never be lost off screen.
    func clamped(_ value: CGSize, size: CGSize, scale: CGFloat, in stage: CGSize) -> CGSize {
        let x = max(0, (size.width * scale - stage.width) / 2), y = max(0, (size.height * scale - stage.height) / 2)
        return CGSize(width: min(max(value.width, -x), x), height: min(max(value.height, -y), y))
    }

    func panning(size: CGSize, in stage: CGSize) -> some Gesture {
        DragGesture(minimumDistance: 4, coordinateSpace: .global)
            .onChanged { if zoom > 1 { drag = $0.translation } }
            .onEnded { value in
                if zoom > 1 {
                    offset = clamped(CGSize(width: offset.width + drag.width, height: offset.height + drag.height), size: size, scale: zoom, in: stage)
                    drag = .zero
                } else if abs(value.translation.width) > 60 {
                    swipe(value.translation.width < 0 ? 1 : -1)
                }
            }
    }

    var pinching: some Gesture {
        MagnifyGesture()
            .onChanged { pinch = $0.magnification }
            .onEnded { value in zoom = min(max(zoom * value.magnification, 1), Self.limit); pinch = 1 }
    }
}

/// A clip, playing as soon as it has loaded.
struct ClipStage: View {
    var asset: Asset
    var url: URL?
    @State private var player: AVPlayer?
    var aspect: CGFloat {
        if let w = asset.metadata["width"].number, let h = asset.metadata["height"].number, w > 0, h > 0 { return w / h }
        return 9 / 16
    }
    var body: some View {
        Group {
            if let player { PlayerView(player: player).aspectRatio(aspect, contentMode: .fit) }
            else { BusyRing(size: 20).allowsHitTesting(false) }
        }
        .onChange(of: url, initial: true) { _, url in
            guard let url, player == nil else { return }
            player = AVPlayer(url: url)
            #if !UI_SMOKE
            player?.play()
            #endif
        }
        .onDisappear { player?.pause() }
    }
}

struct LibraryView: View {
    @EnvironmentObject var store: AppStore
    @State private var filter = "全部"
    @State private var importing = false
    @State private var uploading = false
    var all: [Asset] { store.snapshot?.assets ?? [] }
    var assets: [Asset] {
        all.filter { filter == "全部" || (filter == "图片" && !$0.isVideo) || (filter == "视频" && $0.isVideo) }
    }
    var body: some View {
        let videos = all.filter(\.isVideo).count
        Page(title: "素材") {
            if !all.isEmpty {
                Button { importing = true } label: { ButtonFace(title: uploading ? "正在导入" : "导入", icon: "plus", busy: uploading) }
                    .buttonStyle(.jurvis(.secondary, .small)).disabled(uploading).accessibilityLabel("导入图片")
            }
        } content: {
            if store.snapshot == nil {
                LoadingState(text: "正在载入素材")
            } else if all.isEmpty {
                welcome
            } else {
                FilterChips(options: ["全部", "图片", "视频"], selection: $filter,
                            icons: ["全部": "square.grid.2x2", "图片": "photo", "视频": "film"],
                            counts: ["全部": all.count, "图片": all.count - videos, "视频": videos])
                if assets.isEmpty {
                    EmptyState(icon: filter == "视频" ? "film" : "photo", title: "没有\(filter)", detail: "换一个类别看看。")
                }
                LazyVGrid(columns: [GridItem(.adaptive(minimum: 150), spacing: 12)], spacing: 18) {
                    ForEach(assets) { asset in
                        Button { store.view(asset, among: assets) } label: {
                            VStack(alignment: .leading, spacing: 8) {
                                MediaThumb(asset: asset).aspectRatio(1, contentMode: .fit)
                                VStack(alignment: .leading, spacing: 2) {
                                    Text(asset.name).font(.jCallout).lineLimit(1).truncationMode(.middle).foregroundStyle(Color.ink)
                                    Text(asset.created_at.relativeDate).font(.jMicro).foregroundStyle(Color.inkFaint)
                                }
                            }
                        }
                        .buttonStyle(PressStyle())
                    }
                }
            }
        }
        .fileImporter(isPresented: $importing, allowedContentTypes: [.image]) { result in
            uploading = true
            Task { await store.perform { _ = try await store.uploadImage(try result.get()) }; uploading = false }
        }
    }

    var welcome: some View {
        WelcomeCard(icon: "photo.on.rectangle.angled", title: "还没有素材", detail: "参考图、生成的画面、镜头和成片都会出现在这里，保存在 Mac 上。",
                    hint: "也可以在任务里说“画一张…”，让 Jurvis 生成") {
            Button { importing = true } label: { ButtonFace(title: uploading ? "正在导入" : "导入图片", icon: "square.and.arrow.down", busy: uploading) }
                .buttonStyle(.jurvis(.primary)).disabled(uploading)
        }
    }
}

// MARK: - Studio

struct JobCard: View {
    @EnvironmentObject var store: AppStore
    var job: Job
    var open: ((Asset) -> Void)? = nil
    @State private var details = false

    var body: some View {
        Card(padding: 14) {
            HStack(spacing: 10) {
                Image(systemName: job.icon).foregroundStyle(Color.inkSoft).frame(width: 20)
                Text(job.title).font(.jHeading).lineLimit(1)
                Badge(text: job.statusLabel, tone: job.tone)
                Spacer()
                Text(job.created_at.relativeDate).font(.jMicro).foregroundStyle(Color.inkFaint)
            }
            if !job.payload["prompt"].string.isEmpty {
                Text(job.payload["prompt"].string).font(.jCallout).foregroundStyle(Color.inkSoft).lineLimit(details ? nil : 2).textSelection(.enabled)
            }
            if job.isActive {
                HStack(spacing: 8) {
                    SignalDot()
                    Text(job.phase).font(.jSmall).foregroundStyle(Color.inkSoft)
                    if job.progress > 0 && job.progress < 1 { ProgressBar(value: job.progress).frame(maxWidth: 200) }
                }
            }
            if let error = job.error { Label(error, systemImage: "exclamationmark.triangle").font(.jSmall).foregroundStyle(Color.danger).textSelection(.enabled) }
            let outputs = store.outputs(of: job)
            if !outputs.isEmpty {
                ScrollView(.horizontal, showsIndicators: false) {
                    HStack(spacing: 8) {
                        ForEach(outputs) { asset in
                            Button { open?(asset) } label: { MediaThumb(asset: asset).frame(width: 150, height: 150) }.buttonStyle(.plain).disabled(open == nil)
                        }
                    }
                }
            }
            if details, !job.result["enhanced_prompt"].string.isEmpty {
                CodeBlock(title: "编排后的提示词", text: job.result["enhanced_prompt"].string)
            }
            HStack(spacing: 12) {
                if job.isActive {
                    ActionButton(title: "取消", busyTitle: "正在停止", icon: "stop.circle", scale: .small, isBusy: store.isCancelling(job)) { await store.cancel(job) }
                } else if ["failed", "interrupted", "cancelled"].contains(job.status) {
                    ActionButton(title: "重试", busyTitle: "正在重试", icon: "arrow.clockwise", scale: .small) { await store.retry(job) }
                }
                if !job.result["enhanced_prompt"].string.isEmpty || job.payload["prompt"].string.count > 120 {
                    Button(details ? "收起" : "展开详情") { details.toggle() }.buttonStyle(.jurvis(.ghost, .small))
                }
                Spacer()
            }
        }
    }
}

struct TasksView: View {
    @EnvironmentObject var store: AppStore
    @State private var filter = "全部"
    var jobs: [Job] {
        (store.snapshot?.jobs ?? []).filter {
            filter == "全部" || (filter == "进行中" && $0.isActive) || (filter == "未完成" && ["failed", "interrupted", "cancelled"].contains($0.status))
        }
    }
    var filterTabs: some View {
        SegmentedControl(options: ["全部", "进行中", "未完成"], selection: $filter, icons: ["全部": "list.bullet", "进行中": "play.circle", "未完成": "exclamationmark.circle"])
    }
    var body: some View {
        Page(title: "后台任务", subtitle: "任务在 Mac 上依次执行，避免大模型同时占用内存。关闭手机界面不会停止任务。", width: 900) {
            #if os(macOS)
            filterTabs
            #endif
        } content: {
            // A phone's navigation bar has no room for tabs; they lead the list instead.
            #if os(iOS)
            filterTabs
            #endif
            if store.snapshot == nil { LoadingState(text: "正在载入任务") }
            else if jobs.isEmpty { EmptyState(icon: "checkmark.circle", title: "没有任务", detail: "对话、生成和测试任务会出现在这里。") }
            LazyVStack(spacing: 10) {
                ForEach(jobs) { job in JobCard(job: job, open: { store.view($0, among: store.outputs(of: job)) }) }
            }
        }
    }
}
