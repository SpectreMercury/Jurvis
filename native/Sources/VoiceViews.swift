import SwiftUI

// The controls and screens of voice: dictation in the composer, reading a reply aloud, the voice conversation, and its settings.
// Recording, upload and playback are in Voice.swift.

extension View {
    /// The face of a quiet round button in the composer: an icon, with room for a finger around it on iOS.
    func composerFace(_ size: CGFloat) -> some View {
        font(.system(size: 14)).frame(width: size, height: size)
            #if os(iOS)
            .frame(minWidth: 44, minHeight: 44)
            #endif
            .contentShape(Rectangle())
    }
}

// MARK: - Dictation

/// Tap to speak; it stops by itself on silence or on a second tap, and hands over the words.
/// The recorder lives here so the microphone level redraws this button and nothing else.
struct DictationButton: View {
    @EnvironmentObject var store: AppStore
    var size: CGFloat
    /// Something else wants the microphone: drop the recording.
    var interrupted: Bool
    var onText: (String) -> Void
    @StateObject private var recorder = Recorder()
    @State private var transcribing = false

    var body: some View {
        Button(action: tap) {
            Group {
                if transcribing { BusyRing(size: 14) } else { Image(systemName: "mic") }
            }
            .foregroundStyle(recorder.active ? Color.signal : .inkSoft)
            .frame(width: size, height: size)
            .background {
                if recorder.active {
                    Circle().fill(Color.signalSoft).scaleEffect(1 + recorder.level * 0.3)
                        .animation(.linear(duration: 0.08), value: recorder.level)
                }
            }
            .composerFace(size)
        }
        .buttonStyle(PressStyle()).disabled(transcribing)
        .help(recorder.active ? "停止录音" : "语音输入：说完自动转成文字")
        .accessibilityLabel(transcribing ? "正在识别" : recorder.active ? "停止录音" : "语音输入")
        .onChange(of: interrupted) { _, now in if now { recorder.cancel() } }
        .onDisappear { recorder.cancel() }
    }

    func tap() {
        if recorder.active { recorder.stop(); return }
        Task {
            do {
                guard let audio = try await recorder.listen() else { return }
                transcribing = true; defer { transcribing = false }
                let text = try await Voice.shared.transcribe(audio, api: store.requireAPI())
                if text.isEmpty { store.error = "没有听清，再说一次" } else { onText(text) }
            } catch { store.report(error) }
        }
    }
}

// MARK: - Read aloud

/// Reads a finished reply aloud; while it is being read the same button stops it.
struct ReadAloudButton: View {
    @EnvironmentObject var store: AppStore
    @ObservedObject private var voice = Voice.shared
    var id: String
    var text: String

    var body: some View {
        if voice.canSpeak {
            let reading = voice.speaking == id
            Button(action: tap) {
                if reading { Label("停止", systemImage: "stop.fill").foregroundStyle(Color.signal) }
                else { Label("朗读", systemImage: "speaker.wave.2") }
            }
            .buttonStyle(.jurvis(.ghost, .small))
            .help(reading ? "停止朗读" : "朗读").accessibilityLabel(reading ? "停止朗读" : "朗读")
        }
    }

    func tap() {
        if voice.speaking == id { voice.stopSpeaking(); return }
        Task {
            do { try await voice.speak(text, key: id, api: store.requireAPI()) } catch { store.report(error) }
        }
    }
}

// MARK: - Voice conversation

/// The loop of a voice conversation: listen, transcribe, ask, read the reply aloud, listen again, until the user ends it.
@MainActor final class VoiceConversation: ObservableObject {
    enum Phase { case listen, transcribe, think, speak, paused }
    static let needsConfirmation = "有一步需要你确认，请结束语音对话，在屏幕上处理。"

    @Published private(set) var phase: Phase = .listen
    /// The last thing the user said, and the reply to it.
    @Published private(set) var said = ""
    @Published private(set) var reply = ""
    @Published private(set) var problem: String?
    let recorder = Recorder()
    private var active = false
    private var misses = 0
    private var resume: CheckedContinuation<Void, Never>?
    /// The task stopped for a yes or no that cannot be given by voice.
    private var needsUser = false
    private var holdsAudio = false
    #if os(iOS)
    private var idleTimerWasDisabled = false
    #endif

    func start(_ store: AppStore) {
        guard !active else { return }
        active = true
        // Held for the whole conversation, so the hardware is not handed back between listening and speaking.
        holdsAudio = (try? AudioSession.begin()) != nil
        #if os(iOS)
        idleTimerWasDisabled = UIApplication.shared.isIdleTimerDisabled
        UIApplication.shared.isIdleTimerDisabled = true   // nobody touches the screen while talking
        #endif
        Voice.shared.warm(store.api)
        Task { await run(store) }
    }

    func close() {
        guard active else { return }
        active = false
        recorder.cancel()
        Voice.shared.stopSpeaking()
        resume?.resume(); resume = nil
        if holdsAudio { AudioSession.end(); holdsAudio = false }
        #if os(iOS)
        UIApplication.shared.isIdleTimerDisabled = idleTimerWasDisabled
        #endif
    }

    /// The circle: interrupts speech, resumes when paused, or ends the recording early.
    func tap() {
        switch phase {
        case .speak: Voice.shared.stopSpeaking()
        case .paused: resume?.resume(); resume = nil
        case .listen: recorder.stop()
        case .transcribe, .think: break
        }
    }

    private func pause() async {
        phase = .paused
        await withCheckedContinuation { resume = $0 }
        problem = nil
    }

    private func run(_ store: AppStore) async {
        while active {
            do {
                phase = .listen
                let audio = try await recorder.listen()
                guard active else { break }
                guard let audio else {
                    misses += 1
                    if misses >= 2 {   // nobody is talking: stop holding the microphone open until they tap
                        await pause()
                        misses = 0
                    }
                    continue
                }
                misses = 0
                phase = .transcribe
                let text = try await Voice.shared.transcribe(audio, api: store.requireAPI())
                guard active else { break }
                if text.isEmpty { continue }
                said = text; reply = ""; problem = nil
                phase = .think
                let answer = try await ask(text, store)
                guard active else { break }
                let readable = Voice.pieces(answer, limit: 400).joined(separator: " ")
                reply = readable.isEmpty ? answer : readable
                if !answer.isEmpty {
                    phase = .speak
                    try await Voice.shared.speak(answer, key: "conversation", api: store.requireAPI())
                    guard active else { break }
                }
                // Listening again would only wait for a reply that cannot come until the user has answered on screen.
                if needsUser { needsUser = false; await pause() }
            } catch {
                guard active else { break }
                problem = error.localizedDescription
                await pause()
            }
        }
    }

    /// Sends what was said into the open task and returns the reply's text once Jurvis has finished.
    private func ask(_ text: String, _ store: AppStore) async throws -> String {
        // An earlier reply is still on its way: wait for it, as the send button would.
        while active, store.chatBusy {
            if store.awaitingConfirmation { needsUser = true; return Self.needsConfirmation }
            try? await Task.sleep(for: .milliseconds(300))
        }
        guard active else { return "" }
        let job = try await store.send(text, assets: [], voice: true)
        while true {
            try? await Task.sleep(for: .milliseconds(400))
            guard active else { return "" }
            if store.awaitingConfirmation { needsUser = true; return Self.needsConfirmation }
            // The store's own polling keeps the job current; done means this job has ended and nothing else runs in the task.
            if let current = store.job(job.id), !current.isActive, !store.chatBusy { break }
        }
        await store.refreshMessages()
        let id = job.payload["message_id"].string
        guard let last = store.messages.last(where: { $0.id == id }) ?? store.messages.last(where: { !$0.isUser }) else { return "" }
        if last.status == "failed" { throw APIError.message(last.errorText.isEmpty ? "回复失败" : last.errorText) }
        return last.spokenTexts.last ?? ""
    }
}

/// The voice conversation: a circle that listens and speaks, what was said, and a way out.
struct VoiceConversationView: View {
    @EnvironmentObject var store: AppStore
    @Environment(\.dismiss) private var dismiss
    @StateObject private var talk = VoiceConversation()

    var title: String {
        switch talk.phase {
        case .listen: return "在听"
        case .transcribe: return "正在识别"
        case .think: return "正在想"
        case .speak: return "正在说"
        case .paused: return "已暂停"
        }
    }
    var hint: String {
        switch talk.phase {
        case .listen: return "说完会自动发送"
        case .transcribe: return ""
        case .think: return "需要一点时间"
        case .speak: return "点一下圆圈可以打断"
        case .paused: return "点一下圆圈继续说"
        }
    }

    var body: some View {
        VStack(spacing: 0) {
            VStack(spacing: 8) {
                VoiceOrb(phase: talk.phase, recorder: talk.recorder, action: talk.tap).padding(.bottom, 24)
                Text(title).font(.jTitle).lineLimit(1)
                Text(hint).font(.jCallout).foregroundStyle(Color.inkSoft).lineLimit(1).frame(minHeight: 20)
            }
            .frame(maxWidth: .infinity, maxHeight: .infinity)
            .accessibilityElement(children: .contain)
            ScrollView {
                VStack(alignment: .leading, spacing: 12) {
                    if let problem = talk.problem {
                        Label(problem, systemImage: "exclamationmark.triangle").font(.jSmall).foregroundStyle(Color.danger)
                            .fixedSize(horizontal: false, vertical: true)
                            .frame(maxWidth: .infinity, alignment: .center)
                    }
                    if !talk.said.isEmpty { words("你", talk.said, tint: .inkSoft) }
                    if !talk.reply.isEmpty { words("Jurvis", talk.reply, tint: .ink) }
                }
                .frame(maxWidth: .infinity, alignment: .leading)
            }
            .frame(maxWidth: 560).frame(height: 190)
            Button { dismiss() } label: { Label("结束", systemImage: "xmark").frame(minWidth: 72) }
                .buttonStyle(.jurvis(.secondary, .large)).keyboardShortcut(.cancelAction)
                .padding(.top, 20)
        }
        .padding(.horizontal, 24).padding(.top, 24).padding(.bottom, 20)
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .foregroundStyle(Color.ink)
        .background(Color.canvas)
        #if os(macOS)
        .frame(width: 460, height: 600)
        #endif
        .onAppear { talk.start(store) }
        .onDisappear { talk.close() }
    }

    func words(_ who: String, _ text: String, tint: Color) -> some View {
        VStack(alignment: .leading, spacing: 2) {
            Text(who).font(.jMicro).foregroundStyle(Color.inkSoft)
            Text(text).font(.jBody).lineSpacing(4).foregroundStyle(tint).fixedSize(horizontal: false, vertical: true)
        }
    }
}

/// The circle. Black at rest; green while it hears speech and while Jurvis speaks. The soft ring behind it follows the microphone.
struct VoiceOrb: View {
    var phase: VoiceConversation.Phase
    @ObservedObject var recorder: Recorder
    var action: () -> Void
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    private let size: CGFloat = 96

    var body: some View {
        let hearing = phase == .listen && recorder.heard
        Button(action: action) {
            ZStack {
                ring
                Circle().fill(hearing || phase == .speak ? Color.signal : phase == .paused ? .inkSoft : .ink)
                    .frame(width: size, height: size)
                    .animation(.easeOut(duration: 0.2), value: hearing)
                Image(systemName: "waveform").font(.system(size: 30)).foregroundStyle(Color.inkReversed)
            }
            .frame(width: 132, height: 132)
            .contentShape(Circle())
        }
        .buttonStyle(PressStyle())
        .accessibilityLabel("打断或继续")
    }

    @ViewBuilder var ring: some View {
        let soft = Circle().fill(Color.signalSoft).frame(width: size, height: size)
        switch phase {
        case .listen:
            soft.scaleEffect(1 + recorder.level * 0.55).animation(.linear(duration: 0.08), value: recorder.level)
        case .speak:
            // Driven by the clock, like the other signals in the app.
            TimelineView(.animation(paused: reduceMotion)) { context in
                let beat = (sin(context.date.timeIntervalSinceReferenceDate * 2 * .pi / 1.4) + 1) / 2
                soft.scaleEffect(reduceMotion ? 1.2 : 1.05 + 0.35 * beat)
            }
        case .transcribe, .think:
            TimelineView(.animation(paused: reduceMotion)) { context in
                let beat = (sin(context.date.timeIntervalSinceReferenceDate * 2 * .pi / 1.2) + 1) / 2
                Circle().fill(Color.bubble).frame(width: size, height: size).scaleEffect(1.14).opacity(reduceMotion ? 1 : 0.45 + 0.55 * beat)
            }
        case .paused:
            EmptyView()
        }
    }
}

// MARK: - Settings

/// Which speech models the Mac has, and the voice replies are read in.
struct VoiceSettings: View {
    @EnvironmentObject var store: AppStore
    @ObservedObject private var voice = Voice.shared

    /// The same names the web client shows.
    static let names = ["vivian": "Vivian · 女声", "serena": "Serena · 女声", "ono_anna": "Anna · 女声（日语）", "sohee": "Sohee · 女声（韩语）",
                        "uncle_fu": "傅叔 · 男声", "dylan": "Dylan · 男声", "eric": "Eric · 男声", "ryan": "Ryan · 男声（英语）", "aiden": "Aiden · 男声（英语）"]

    var footnote: String {
        let local = "语音识别和朗读都在这台 Mac 上完成，录音不会发到别处。"
        guard let status = voice.status, !status.stt || !status.tts else { return local }
        return local + "模型放在 Mac 上 Jurvis 目录的 models/speech/ 里，安装方法见 docs/MODELS.md。"
    }

    var body: some View {
        RowGroup(title: "语音", footnote: footnote) {
            if let status = voice.status {
                InfoRow(title: "语音识别", detail: status.stt ? status.stt_model : "未安装") { StatusMark(state: status.stt ? .done : .pending, size: 16) }
                InfoRow(title: "语音合成", detail: status.tts ? status.tts_model : "未安装") { StatusMark(state: status.tts ? .done : .pending, size: 16) }
                if status.tts, !status.speakers.isEmpty {
                    // The names can be long, so on a narrow screen the controls get their own line instead of squeezing the label.
                    ViewThatFits(in: .horizontal) {
                        HStack(spacing: 12) { Text("朗读的声音").font(.jBody).fixedSize(); Spacer(minLength: 8); picker(status); sample }
                        VStack(alignment: .leading, spacing: 8) {
                            Text("朗读的声音").font(.jBody)
                            HStack(spacing: 12) { picker(status).padding(.leading, -10); Spacer(minLength: 8); sample }
                        }
                    }
                    .padding(.horizontal, 16).padding(.vertical, 11).frame(maxWidth: .infinity, minHeight: 52, alignment: .leading)
                }
            } else {
                LoadingState(text: "正在检查语音模型")
            }
        }
        .task { await voice.refresh(store.api, force: true) }
    }

    func picker(_ status: SpeechStatus) -> some View {
        let current = status.speakers.contains(status.speaker) ? status.speaker : status.speakers[0]
        return Picker("朗读的声音", selection: Binding(get: { current }, set: { choose($0) })) {
            ForEach(status.speakers, id: \.self) { Text(Self.names[$0] ?? $0).tag($0) }
        }
        .labelsHidden().lineLimit(1).fixedSize()
    }

    var sample: some View {
        ActionButton(title: "试听", busyTitle: "正在朗读", icon: "speaker.wave.2", scale: .small) {
            try await voice.speak("你好，我是 Jurvis。Hello, this is Jurvis.", key: "sample", api: store.requireAPI())
        }
    }

    func choose(_ speaker: String) {
        voice.status?.speaker = speaker   // shown at once; the Mac's answer follows
        Task {
            await store.perform {
                let api = try store.requireAPI()
                let _: JSONValue = try await api.request("/api/settings", method: "PATCH", body: ["voice": ["speaker": speaker]])
            }
            await voice.refresh(store.api, force: true)
        }
    }
}
