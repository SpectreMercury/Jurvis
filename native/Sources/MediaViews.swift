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
    var body: some View {
        // The image lives in an overlay so it can never change the thumbnail's layout size.
        Color.subtleFill
            .overlay {
                if let image {
                    image.resizable().aspectRatio(contentMode: fill ? .fill : .fit)
                } else {
                    Image(systemName: asset.isVideo ? "film" : "photo").font(.system(size: 22, weight: .light)).foregroundStyle(Color.jurvisAccent)
                }
            }
            .overlay(alignment: .bottomLeading) {
                if asset.isVideo {
                    Image(systemName: "play.circle.fill").font(.title3).symbolRenderingMode(.palette).foregroundStyle(.white, .black.opacity(0.45)).padding(6)
                }
            }
            .clipShape(RoundedRectangle(cornerRadius: 10))
            .overlay(RoundedRectangle(cornerRadius: 10).strokeBorder(Color.hairline))
            .task(id: asset.id) { image = await store.thumbnail(asset) }
            .accessibilityLabel(asset.name)
    }
}

struct MediaDetail: View {
    @EnvironmentObject var store: AppStore
    @Environment(\.dismiss) var dismiss
    var asset: Asset
    @State private var url: URL?
    @State private var player: AVPlayer?
    var aspect: CGFloat {
        if let w = asset.metadata["width"].number, let h = asset.metadata["height"].number, w > 0, h > 0 { return w / h }
        return 9 / 16
    }
    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 16) {
                    Group {
                        if asset.isVideo {
                            if let player { PlayerView(player: player).aspectRatio(aspect, contentMode: .fit).frame(maxHeight: 560) }
                            else { ProgressView("正在载入视频…").frame(maxWidth: .infinity, minHeight: 300) }
                        } else {
                            MediaThumb(asset: asset, fill: false).frame(minHeight: 280, maxHeight: 620)
                        }
                    }.frame(maxWidth: .infinity)
                    VStack(alignment: .leading, spacing: 8) {
                        Text(asset.name).font(.headline)
                        HStack(spacing: 10) {
                            Text(asset.size.fileSize)
                            Text(asset.created_at.relativeDate)
                            if case .number(let w) = asset.metadata["width"], case .number(let h) = asset.metadata["height"] { Text("\(Int(w))×\(Int(h))") }
                        }.font(.caption).foregroundStyle(.secondary)
                        if !asset.metadata["prompt"].string.isEmpty {
                            Text(asset.metadata["prompt"].string).font(.callout).foregroundStyle(.secondary).textSelection(.enabled)
                        }
                    }
                    HStack {
                        if asset.isVideo {
                            Button { Task { await store.perform { _ = try await store.submit("/api/assets/\(asset.id)/frames", body: [:]); dismiss() } } } label: {
                                Label("提取首尾帧", systemImage: "rectangle.split.2x1")
                            }.buttonStyle(.bordered).help("把首尾帧存为图片，用于续拍下一个镜头")
                        }
                        Spacer()
                        if let url { ShareLink(item: url) { Label("导出", systemImage: "square.and.arrow.up") }.buttonStyle(.borderedProminent) }
                    }
                }.padding(20)
            }
            .navigationTitle(asset.kind == "film" ? "成片" : asset.isVideo ? "视频" : "图片")
            .toolbar { ToolbarItem(placement: .cancellationAction) { Button("完成") { dismiss() } } }
            .task {
                do { url = try await store.mediaURL(asset); if asset.isVideo, let url { player = AVPlayer(url: url) } }
                catch { store.report(error) }
            }
            .onDisappear { player?.pause() }
        }
        #if os(macOS)
        .frame(minWidth: 640, minHeight: 560)
        #endif
    }
}

struct LibraryView: View {
    @EnvironmentObject var store: AppStore
    @State private var filter = "全部"
    @State private var selected: Asset?
    @State private var importing = false
    var assets: [Asset] {
        (store.snapshot?.assets ?? []).filter { filter == "全部" || (filter == "图片" && !$0.isVideo) || (filter == "视频" && $0.isVideo) }
    }
    var body: some View {
        Page(title: "素材", subtitle: "参考图、生成的画面、镜头和成片，都保存在 Mac 上。") {
            Button { importing = true } label: { Label("导入图片", systemImage: "plus") }.buttonStyle(.bordered)
        } content: {
            Picker("类型", selection: $filter) { ForEach(["全部", "图片", "视频"], id: \.self) { Text($0) } }
                .pickerStyle(.segmented).labelsHidden().fixedSize()
            if assets.isEmpty {
                EmptyState(icon: "photo.on.rectangle.angled", title: "还没有素材", detail: "导入一张参考图，或在助理、创作中生成第一张画面。")
            }
            LazyVGrid(columns: [GridItem(.adaptive(minimum: 160), spacing: 14)], spacing: 16) {
                ForEach(assets) { asset in
                    Button { selected = asset } label: {
                        VStack(alignment: .leading, spacing: 6) {
                            MediaThumb(asset: asset).aspectRatio(1, contentMode: .fit)
                            Text(asset.name).font(.caption.weight(.medium)).lineLimit(1).foregroundStyle(.primary)
                            Text(asset.created_at.relativeDate).font(.caption2).foregroundStyle(.secondary)
                        }
                    }.buttonStyle(.plain)
                }
            }
        }
        .sheet(item: $selected) { MediaDetail(asset: $0).environmentObject(store) }
        .fileImporter(isPresented: $importing, allowedContentTypes: [.image]) { result in
            Task { await store.perform { _ = try await store.uploadImage(try result.get()) } }
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
                Image(systemName: job.icon).foregroundStyle(Color.jurvisAccent).frame(width: 20)
                Text(job.title).font(.callout.weight(.semibold))
                Badge(text: job.statusLabel, tone: job.tone)
                Spacer()
                Text(job.created_at.relativeDate).font(.caption2).foregroundStyle(.tertiary)
            }
            if !job.payload["prompt"].string.isEmpty {
                Text(job.payload["prompt"].string).font(.callout).foregroundStyle(.secondary).lineLimit(details ? nil : 2).textSelection(.enabled)
            }
            if job.isActive {
                HStack(spacing: 8) {
                    if job.progress > 0 && job.progress < 1 { ProgressView(value: job.progress).frame(maxWidth: 200) } else { ProgressView().controlSize(.small) }
                    Text(job.phase).font(.caption).foregroundStyle(.secondary)
                }
            }
            if let error = job.error { Label(error, systemImage: "exclamationmark.triangle").font(.caption).foregroundStyle(Color.warning).textSelection(.enabled) }
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
                    Button("取消", role: .destructive) { Task { await store.cancel(job) } }
                } else if ["failed", "interrupted", "cancelled"].contains(job.status) {
                    Button("重试") { Task { await store.retry(job) } }
                }
                if !job.result["enhanced_prompt"].string.isEmpty || job.payload["prompt"].string.count > 120 {
                    Button(details ? "收起" : "展开详情") { details.toggle() }
                }
                Spacer()
            }
            .font(.caption).buttonStyle(.borderless)
        }
    }
}

struct TasksView: View {
    @EnvironmentObject var store: AppStore
    @State private var filter = "全部"
    @State private var selected: Asset?
    var jobs: [Job] {
        (store.snapshot?.jobs ?? []).filter {
            filter == "全部" || (filter == "进行中" && $0.isActive) || (filter == "未完成" && ["failed", "interrupted", "cancelled"].contains($0.status))
        }
    }
    var body: some View {
        Page(title: "任务", subtitle: "任务在 Mac 上依次执行，避免大模型同时占用内存。关闭手机界面不会停止任务。", width: 900) {
            Picker("筛选", selection: $filter) { ForEach(["全部", "进行中", "未完成"], id: \.self) { Text($0) } }
                .pickerStyle(.segmented).labelsHidden().frame(width: 240)
        } content: {
            if jobs.isEmpty { EmptyState(icon: "checkmark.circle", title: "没有任务", detail: "对话、生成和测试任务会出现在这里。") }
            LazyVStack(spacing: 10) {
                ForEach(jobs) { JobCard(job: $0, open: { selected = $0 }) }
            }
        }
        .sheet(item: $selected) { MediaDetail(asset: $0).environmentObject(store) }
    }
}
