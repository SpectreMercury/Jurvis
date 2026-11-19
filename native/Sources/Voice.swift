import SwiftUI
import AVFoundation

// Voice: recording from the microphone, sending it to the Mac to be transcribed, and playing replies the Mac reads aloud.
// The models run on the Mac inside the engine, so this file only records, uploads and plays. The screens are in VoiceViews.swift.

/// What the Mac can do with speech.
struct SpeechStatus: Codable, Equatable {
    var stt: Bool; var tts: Bool; var stt_model: String; var tts_model: String; var speakers: [String]; var speaker: String
}
private struct Transcript: Codable { var text: String }

/// The audio hardware of an iPhone or iPad is shared, so it is claimed while someone records or plays and given back after.
/// The Mac has no audio session; there this only counts.
@MainActor enum AudioSession {
    private static var users = 0
    static func begin() throws {
        #if os(iOS)
        if users == 0 {
            let session = AVAudioSession.sharedInstance()
            try session.setCategory(.playAndRecord, mode: .spokenAudio, options: [.defaultToSpeaker, .allowBluetoothHFP])
            try session.setActive(true)
        }
        #endif
        users += 1
    }
    static func end() {
        users = max(0, users - 1)
        #if os(iOS)
        if users == 0 { try? AVAudioSession.sharedInstance().setActive(false, options: .notifyOthersOnDeactivation) }
        #endif
    }
}

// MARK: - Recording

/// One recording at a time. It ends by itself once the speaker has been quiet for a moment.
@MainActor final class Recorder: ObservableObject {
    /// How loud the microphone is right now, 0 to 1, for the UI.
    @Published private(set) var level: Double = 0
    /// Speech has been heard in this recording.
    @Published private(set) var heard = false
    @Published private(set) var active = false
    private var busy = false
    private var stopping = false
    private var cancelled = false

    #if os(iOS)
    static let denied = "没有麦克风权限。请在「设置 › 隐私与安全性 › 麦克风」里允许 Jurvis。"
    #else
    static let denied = "没有麦克风权限。请在「系统设置 › 隐私与安全性 › 麦克风」里允许 Jurvis。"
    #endif

    /// Asks the first time; afterwards answers from what the user chose.
    static func allowed() async -> Bool {
        switch AVCaptureDevice.authorizationStatus(for: .audio) {
        case .authorized: return true
        case .notDetermined: return await AVCaptureDevice.requestAccess(for: .audio)
        default: return false
        }
    }

    /// Ends the recording and keeps what was said.
    func stop() { stopping = true }
    /// Ends the recording and throws it away.
    func cancel() { cancelled = true; stopping = true }

    /// Records until the speaker goes quiet. Returns the recording (AAC in .m4a), or nil when nothing was said or it was cancelled.
    func listen(quiet: Double = 1.3, patience: Double = 8, longest: Double = 90) async throws -> Data? {
        guard !busy else { return nil }
        busy = true; stopping = false; cancelled = false
        defer { busy = false }
        guard await Self.allowed() else { throw APIError.message(Self.denied) }
        if stopping { return nil }
        try AudioSession.begin()
        defer { AudioSession.end() }
        let url = FileManager.default.temporaryDirectory.appendingPathComponent("jurvis-speech-\(UUID().uuidString).m4a")
        defer { try? FileManager.default.removeItem(at: url) }
        let recorder = try AVAudioRecorder(url: url, settings: [
            AVFormatIDKey: Int(kAudioFormatMPEG4AAC), AVSampleRateKey: 44100, AVNumberOfChannelsKey: 1,
            AVEncoderBitRateKey: 64000, AVEncoderAudioQualityKey: AVAudioQuality.high.rawValue,
        ])
        recorder.isMeteringEnabled = true
        guard recorder.record() else { throw APIError.message("没有找到麦克风。") }
        active = true; heard = false
        defer { active = false; level = 0 }

        let began = Date()
        var floor = 1.0, quietAt: Double?
        while !stopping {
            try? await Task.sleep(for: .milliseconds(50))
            recorder.updateMeters()
            // Average power is in decibels below full scale; as an amplitude it compares with the thresholds the web client uses.
            let amplitude = pow(10, Double(recorder.averagePower(forChannel: 0)) / 20)
            let now = Date().timeIntervalSince(began)
            if now < 0.4 { floor = min(floor, max(amplitude, 0.002)) }   // the room's own noise, measured before speech starts
            if amplitude > max(0.018, floor * 3.5) {
                if !heard { heard = true }
                quietAt = nil
            } else if heard, quietAt == nil { quietAt = now }
            level = min(1, amplitude * 9)
            if now > longest || (heard && quietAt.map { now - $0 > quiet } ?? false) || (!heard && now > patience) { break }
        }
        recorder.stop()
        if cancelled || !heard { return nil }
        return try Data(contentsOf: url)
    }
}

// MARK: - Speech on the Mac

@MainActor final class Voice: ObservableObject {
    static let shared = Voice()
    /// Nil until the Mac has answered.
    @Published var status: SpeechStatus?
    /// What is being read aloud: a message's id, or a name such as "conversation".
    @Published private(set) var speaking: String?
    private var player: AVAudioPlayer?
    /// Counts readings, so one that was stopped or replaced knows it is no longer the current one.
    private var reading = 0

    var canListen: Bool { status?.stt ?? false }
    var canSpeak: Bool { status?.tts ?? false }

    /// Asks the Mac what it can do. The answer is kept; pass `force` to ask again.
    func refresh(_ api: API?, force: Bool = false) async {
        guard let api, force || status == nil else { return }
        if let fresh: SpeechStatus = try? await api.request("/api/speech"), fresh != status { status = fresh }
    }

    /// Loads the models now, so the first sentence does not wait for them.
    func warm(_ api: API?) {
        Task { let _: JSONValue? = try? await api?.request("/api/speech/warm", method: "POST", body: [String: Any]()) }
    }

    /// The words in a recording; empty when nothing was said.
    func transcribe(_ audio: Data, api: API) async throws -> String {
        let answer: Transcript = try await api.upload("/api/speech/transcribe", data: audio, name: "speech.m4a", mime: "audio/mp4", timeout: 120)
        return answer.text.trimmingCharacters(in: .whitespacesAndNewlines)
    }

    func stopSpeaking() {
        reading += 1
        player?.stop(); player = nil
        if speaking != nil { speaking = nil }
    }

    /// Reads text aloud, a sentence or two at a time, and returns when it has finished or been stopped.
    func speak(_ text: String, key: String, api: API) async throws {
        stopSpeaking()
        let parts = Self.pieces(text)
        guard !parts.isEmpty else { return }
        try AudioSession.begin()
        reading += 1
        let mine = reading
        speaking = key
        // The next piece is fetched while this one plays, so there is no gap between them.
        let fetch = { (index: Int) in Task { try await api.bytes("/api/speech/speak", body: ["text": parts[index]], timeout: 120) } }
        var next: Task<Data, Error>? = fetch(0)
        defer {
            next?.cancel()
            AudioSession.end()
            if reading == mine { speaking = nil; player = nil }
        }
        do {
            for index in parts.indices {
                guard let audio = try await next?.value, reading == mine else { return }
                next = index + 1 < parts.count ? fetch(index + 1) : nil
                let player = try AVAudioPlayer(data: audio)
                guard player.play() else { throw APIError.message("声音播放不了") }
                self.player = player
                while reading == mine, player.isPlaying { try? await Task.sleep(for: .milliseconds(40)) }
            }
        } catch {
            if reading == mine { throw error }   // a reading that was stopped has nobody to tell
        }
    }

    /// What to read for a Markdown reply, in pieces short enough that the first one starts quickly. Follows `pieces()` in web/voice.js.
    static func pieces(_ text: String, limit: Int = 140) -> [String] {
        var clean = text.replacingOccurrences(of: "\r\n", with: "\n")
        let rules: [(String, String)] = [
            ("```[\\s\\S]*?(```|\\z)", " "),                              // code blocks
            ("!\\[[^\\]]*\\]\\([^)]*\\)", " "),                           // images
            ("\\[([^\\]]+)\\]\\([^)]*\\)", "$1"),                         // links keep their text
            ("https?://\\S+", " "),
            ("(?m)^\\s{0,3}(#{1,6}|[-*+]|\\d+[.)]|>)\\s+", ""),           // heading, list and quote marks
            ("(?m)^\\s*\\|?[\\s:|-]{3,}\\|?\\s*$", " "),                  // table rules
            ("[*_`~|]+", " "),
        ]
        for (pattern, with) in rules { clean = clean.replacingOccurrences(of: pattern, with: with, options: .regularExpression) }

        // Sentences: a run of text and the marks that end it.
        var sentences: [String] = [], sentence = "", ended = false
        for character in clean {
            let end = "。！？!?；;".contains(character) || character.isNewline
            if ended && !end { sentences.append(sentence); sentence = ""; ended = false }
            if end && sentence.isEmpty { continue }
            sentence.append(character); ended = end
        }
        if !sentence.isEmpty { sentences.append(sentence) }

        var out: [String] = [], now = ""
        func trimmed(_ value: String) -> String { value.trimmingCharacters(in: .whitespacesAndNewlines) }
        for sentence in sentences {
            var piece = Array(sentence.replacingOccurrences(of: "\\s+", with: " ", options: .regularExpression))
            // A sentence longer than the limit is cut at a comma or a space.
            while piece.count > limit {
                let cut = piece[..<limit].lastIndex { "，,、 ".contains($0) }.map { $0 + 1 } ?? limit
                if !trimmed(now).isEmpty { out.append(trimmed(now)) }
                out.append(trimmed(String(piece[..<cut])))
                now = ""
                piece = Array(piece[cut...])
            }
            let full = now.count >= 40 && (trimmed(now).last.map { "。！？!?".contains($0) } ?? false)
            if now.count + piece.count > limit || full { out.append(trimmed(now)); now = "" }
            now += String(piece)
        }
        if !trimmed(now).isEmpty { out.append(trimmed(now)) }
        return out.filter { $0.contains { $0.isLetter || $0.isNumber } }
    }
}

extension Message {
    /// What the model wrote, without the tools in between: the text a reader would read aloud.
    var spokenTexts: [String] {
        let texts = steps.map(\.text).filter { !$0.isEmpty }
        return texts.isEmpty && !content.isEmpty ? [content] : texts
    }
}

extension AppStore {
    /// The open task has stopped to ask for a yes or no. The engine says so in the job's phase.
    var awaitingConfirmation: Bool { chatJob?.phase.hasPrefix("等待你确认") ?? false }
}
