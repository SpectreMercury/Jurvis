import SwiftUI
import EventKit
import Contacts
import UserNotifications

// How Jurvis reaches the user when the app is closed, and what this device shares of the user's day:
// - a push token for the iPhone and iPad app (the Mac sends through the user's own Apple key, engine/push.py);
// - notices the engine wants told (a background result, an action waiting for a yes), announced by the open app;
// - the calendar, reminders and contacts, read here with the user's permission and sent to the Mac (engine/personal.py);
// - new events and reminders the assistant made, added here to the system calendar and reminders.

/// Something the engine wants the user to hear about, from /api/state.
struct Notice: Codable, Equatable, Identifiable {
    var id: String; var kind: String; var title: String; var body: String; var session_id: String; var created_at: Double
    var extras: JSONValue?
}

/// A new event or reminder the assistant made, waiting for a device to add it.
struct OutboxEntry: Codable, Equatable, Identifiable {
    var id: String; var kind: String; var title: String
    /// "2026-11-28T19:00" on this device's clock; empty for a reminder without a date.
    var start: String?; var due: String?; var minutes: Int?; var place: String?; var notes: String?
}

// MARK: - push

#if os(iOS)
/// Receives the device token from iOS. The token goes to the Mac once the app is paired (PushRegistration.send).
final class AppDelegate: NSObject, UIApplicationDelegate {
    func application(_ application: UIApplication, didFinishLaunchingWithOptions launchOptions: [UIApplication.LaunchOptionsKey: Any]? = nil) -> Bool {
        Task { @MainActor in Notifier.shared.prepare() }  // before launch ends, so a button pressed on a notification reaches us
        return true
    }
    func application(_ application: UIApplication, didRegisterForRemoteNotificationsWithDeviceToken deviceToken: Data) {
        let token = deviceToken.map { String(format: "%02x", $0) }.joined()
        Task { @MainActor in PushRegistration.shared.token = token }
    }
    func application(_ application: UIApplication, didFailToRegisterForRemoteNotificationsWithError error: Error) {
        Task { @MainActor in PushRegistration.shared.problem = error.localizedDescription }
    }
}
#endif

/// This device's address for pushes, and whether the Mac has it.
@MainActor final class PushRegistration: ObservableObject {
    static let shared = PushRegistration()
    @Published var token: String?
    @Published var problem: String?
    @Published var sent: String?

    /// Asks iOS for a token when notifications are allowed. Asking again is harmless: iOS hands back the same token.
    func register() async {
        #if os(iOS)
        let settings = await UNUserNotificationCenter.current().notificationSettings()
        guard settings.authorizationStatus == .authorized || settings.authorizationStatus == .provisional else { return }
        UIApplication.shared.registerForRemoteNotifications()
        #endif
    }

    /// Asks for permission to notify, then for a token.
    func enable() async {
        _ = try? await UNUserNotificationCenter.current().requestAuthorization(options: [.alert, .sound, .badge])
        await register()
    }

    /// Tells the Mac where this device is reached, once per token.
    func send(store: AppStore) async {
        guard let token, token != sent, let api = store.api, let topic = Bundle.main.bundleIdentifier else { return }
        #if DEBUG
        let sandbox = true   // a build run from Xcode gets a token for Apple's development push service
        #else
        let sandbox = false
        #endif
        let body: [String: Any] = ["kind": "apns", "token": token, "topic": topic, "sandbox": sandbox]
        if let _: JSONValue = try? await api.request("/api/push/subscribe", method: "POST", body: body) { sent = token }
    }
}

extension Notifier {
    /// Sets up the delegate and the buttons on a notification that asks for a yes.
    func prepare() {
        let center = UNUserNotificationCenter.current()
        center.delegate = self
        let allow = UNNotificationAction(identifier: "run", title: "允许", options: [.authenticationRequired])
        let refuse = UNNotificationAction(identifier: "deny", title: "拒绝", options: [.destructive])
        center.setNotificationCategories([UNNotificationCategory(identifier: "approval", actions: [allow, refuse], intentIdentifiers: [])])
    }

    /// Announces what the engine wants told, once each. A notice that already arrived as a push is not shown twice, and nothing
    /// is shown for what is on screen as it happens.
    func announce(_ list: [Notice], store: AppStore) async {
        self.store = store
        let key = "notices.delivered"
        var done = UserDefaults.standard.stringArray(forKey: key) ?? []
        let fresh = list.filter { !done.contains($0.id) && Date().timeIntervalSince1970 - $0.created_at < 6 * 3600 }
        guard !fresh.isEmpty else { return }
        let center = UNUserNotificationCenter.current()
        if center.delegate == nil { prepare() }
        if await center.notificationSettings().authorizationStatus == .notDetermined {
            _ = try? await center.requestAuthorization(options: [.alert, .sound, .badge])
            await PushRegistration.shared.register()
        }
        let shown = Set(await center.deliveredNotifications().map(\.request.identifier))
        for item in fresh.reversed() {
            done.append(item.id)
            if shown.contains(item.id) || (store.section == .assistant && !store.streamPast && Self.appActive) { continue }
            let content = UNMutableNotificationContent()
            content.title = item.title
            content.body = String(item.body.prefix(180))
            content.sound = .default
            content.threadIdentifier = item.kind
            var info: [String: String] = ["session_id": item.session_id]
            if item.kind == "approval" {
                content.categoryIdentifier = "approval"
                info["message_id"] = item.extras?["message_id"].string ?? ""
                info["call_id"] = item.extras?["call_id"].string ?? ""
            }
            content.userInfo = info
            try? await center.add(UNNotificationRequest(identifier: item.id, content: content, trigger: nil))
        }
        UserDefaults.standard.set(Array(done.suffix(200)), forKey: key)
    }

    /// 允许 or 拒绝 pressed on a notification. The app may have been started for this alone: wait until it has reached the Mac.
    func decide(message: String, call: String, _ decision: String) async {
        for _ in 0..<40 where store?.api == nil { try? await Task.sleep(for: .milliseconds(250)) }
        guard let api = store?.api else { return }
        let _: JSONValue? = try? await api.request("/api/messages/\(message)/commands/\(call)", method: "POST", body: ["decision": decision])
    }
}

// MARK: - calendar, reminders, contacts

/// Reads what the user chose to share from this device and sends a copy to the Mac; adds what the assistant made.
@MainActor final class PersonalSync: ObservableObject {
    static let shared = PersonalSync()
    enum Kind: String, CaseIterable { case calendar, reminders, contacts
        var title: String { ["calendar": "日历", "reminders": "提醒事项", "contacts": "通讯录"][rawValue]! }
        var detail: String {
            switch self {
            case .calendar: return "过去一周到未来两个月的日程"
            case .reminders: return "还没完成的提醒"
            case .contacts: return "姓名、公司、电话和邮箱；模型一次只能按名字查一个人"
            }
        }
    }
    private let events = EKEventStore()
    private let people = CNContactStore()
    private var busy = false
    private var adding = false
    @Published var lastSync: Date? = UserDefaults.standard.object(forKey: "personal.synced") as? Date
    @Published var problem: String?

    func shares(_ kind: Kind) -> Bool { UserDefaults.standard.bool(forKey: "personal.share." + kind.rawValue) }

    /// Switching a kind on asks the system for access; a refusal leaves it off.
    func set(_ kind: Kind, _ on: Bool, store: AppStore) async {
        if on {
            guard await allowed(kind) else {
                problem = "没有\(kind.title)的访问权限。请在系统设置的「隐私与安全性」里允许 Jurvis。"
                objectWillChange.send(); return
            }
        }
        problem = nil
        UserDefaults.standard.set(on, forKey: "personal.share." + kind.rawValue)
        objectWillChange.send()
        if on { await sync(store: store, force: true) }
        else if let api = store.api { let _: JSONValue? = try? await api.request("/api/personal/\(kind.rawValue)", method: "DELETE") }
    }

    private func allowed(_ kind: Kind) async -> Bool {
        switch kind {
        case .calendar: return (try? await events.requestFullAccessToEvents()) ?? false
        case .reminders: return (try? await events.requestFullAccessToReminders()) ?? false
        case .contacts: return (try? await people.requestAccess(for: .contacts)) ?? false
        }
    }

    /// Sends a fresh copy when the last one is older than a quarter of an hour (or now, when forced).
    func sync(store: AppStore, force: Bool = false) async {
        let kinds = Kind.allCases.filter(shares)
        guard !kinds.isEmpty, !busy, let api = store.api else { return }
        if !force, let lastSync, Date().timeIntervalSince(lastSync) < 900 { return }
        busy = true
        defer { busy = false }
        var body: [String: Any] = [:]
        if kinds.contains(.calendar), EKEventStore.authorizationStatus(for: .event) == .fullAccess { body["events"] = readEvents() }
        if kinds.contains(.reminders), EKEventStore.authorizationStatus(for: .reminder) == .fullAccess { body["reminders"] = await readReminders() }
        if kinds.contains(.contacts), CNContactStore.authorizationStatus(for: .contacts) == .authorized { body["contacts"] = await readContacts() }
        guard !body.isEmpty else { return }
        do {
            let _: JSONValue = try await api.request("/api/personal/sync", method: "POST", body: body)
            lastSync = Date(); problem = nil
            UserDefaults.standard.set(lastSync, forKey: "personal.synced")
        } catch { problem = error.localizedDescription }
    }

    private func readEvents() -> [[String: Any]] {
        let from = Date().addingTimeInterval(-7 * 86400), to = Date().addingTimeInterval(60 * 86400)
        return events.events(matching: events.predicateForEvents(withStart: from, end: to, calendars: nil)).prefix(2000).map { event in
            // A repeating event has one identifier for all its days: the start makes each day its own entry.
            ["id": "\(event.calendarItemIdentifier)@\(Int(event.startDate.timeIntervalSince1970))", "title": event.title ?? "",
             "start": event.startDate.timeIntervalSince1970, "end": event.endDate.timeIntervalSince1970, "all_day": event.isAllDay,
             "place": event.location ?? "", "calendar": event.calendar?.title ?? "", "notes": String((event.notes ?? "").prefix(500))]
        }
    }

    private func readReminders() async -> [[String: Any]] {
        let found: [EKReminder] = await withCheckedContinuation { done in
            events.fetchReminders(matching: events.predicateForIncompleteReminders(withDueDateStarting: nil, ending: nil, calendars: nil)) { done.resume(returning: $0 ?? []) }
        }
        return found.prefix(1000).map { item in
            var entry: [String: Any] = ["id": item.calendarItemIdentifier, "title": item.title ?? "", "done": item.isCompleted,
                                        "list": item.calendar?.title ?? "", "notes": String((item.notes ?? "").prefix(500))]
            if let parts = item.dueDateComponents, let due = Calendar.current.date(from: parts) { entry["due"] = due.timeIntervalSince1970 }
            return entry
        }
    }

    private func readContacts() async -> [[String: Any]] {
        let people = self.people
        return await Task.detached { () -> [[String: Any]] in  // enumerating a large address book must not hold the main thread
            let keys = [CNContactGivenNameKey, CNContactFamilyNameKey, CNContactNicknameKey, CNContactOrganizationNameKey, CNContactPhoneNumbersKey, CNContactEmailAddressesKey] as [CNKeyDescriptor]
            var out: [[String: Any]] = []
            try? people.enumerateContacts(with: CNContactFetchRequest(keysToFetch: keys)) { contact, stop in
                // Family name first reads right for Chinese and Japanese names, which is what the user's own contacts mostly are.
                let latin = (contact.familyName + contact.givenName).unicodeScalars.allSatisfy { $0.value < 0x2E80 }
                var name = latin ? [contact.givenName, contact.familyName].filter { !$0.isEmpty }.joined(separator: " ") : contact.familyName + contact.givenName
                if name.isEmpty { name = contact.nickname }
                out.append(["id": contact.identifier, "name": name, "org": contact.organizationName,
                            "phones": contact.phoneNumbers.prefix(6).map { $0.value.stringValue }, "emails": contact.emailAddresses.prefix(6).map { $0.value as String }])
                if out.count >= 5000 { stop.pointee = true }
            }
            return out
        }.value
    }

    // MARK: adding what the assistant made

    func add(_ entries: [OutboxEntry], store: AppStore) async {
        guard !adding, let api = store.api else { return }
        adding = true
        defer { adding = false }
        var givenUp = UserDefaults.standard.stringArray(forKey: "outbox.given-up") ?? []
        var added = false
        for entry in entries where !givenUp.contains(entry.id) {
            // Another device may have taken it a moment ago: then this one leaves it alone.
            guard let mine: OutboxEntry = try? await api.request("/api/personal/outbox/\(entry.id)/claim", method: "POST", body: [String: String]()) else { continue }
            var ok = false
            do { try await save(mine); ok = true; added = true } catch {
                givenUp.append(entry.id)  // no access on this device: it goes back for another device to add
                UserDefaults.standard.set(Array(givenUp.suffix(200)), forKey: "outbox.given-up")
            }
            let _: JSONValue? = try? await api.request("/api/personal/outbox/\(entry.id)/done", method: "POST", body: ["ok": ok])
        }
        if added { await sync(store: store, force: true) }  // the Mac's copy then has what was just added
    }

    private func save(_ entry: OutboxEntry) async throws {
        if entry.kind == "reminder" {
            guard try await events.requestFullAccessToReminders(), let list = events.defaultCalendarForNewReminders() else { throw AddError.unusable }
            let reminder = EKReminder(eventStore: events)
            reminder.calendar = list
            reminder.title = entry.title
            reminder.notes = entry.notes
            if let due = entry.due, let date = CalendarSync.date(due, zone: "") {
                reminder.dueDateComponents = Calendar.current.dateComponents([.year, .month, .day, .hour, .minute], from: date)
                reminder.addAlarm(EKAlarm(absoluteDate: date))
            }
            try events.save(reminder, commit: true)
            return
        }
        let granted = EKEventStore.authorizationStatus(for: .event) == .fullAccess ? true : try await events.requestWriteOnlyAccessToEvents()
        guard granted, let calendar = events.defaultCalendarForNewEvents, let start = CalendarSync.date(entry.start ?? "", zone: "") else { throw AddError.unusable }
        let event = EKEvent(eventStore: events)
        event.calendar = calendar
        event.title = entry.title
        event.location = entry.place
        event.notes = [entry.notes ?? "", "由 Jurvis 添加"].filter { !$0.isEmpty }.joined(separator: "\n")
        event.startDate = start
        event.endDate = start.addingTimeInterval(TimeInterval(max(entry.minutes ?? 60, 5) * 60))
        try events.save(event, span: .thisEvent, commit: true)
    }

    enum AddError: Error { case unusable }
}

// MARK: - settings

/// A kind of action the user allowed for good (engine/permits.py).
struct Permit: Codable, Identifiable, Equatable { var id: String; var tool: String; var label: String; var created_at: Double; var uses: Int }
struct PermitList: Codable { var permits: [Permit] }

/// 设置 → 通知、日历与通讯录、不用再问的操作.
struct ReachSettings: View {
    @EnvironmentObject var store: AppStore
    @ObservedObject private var personal = PersonalSync.shared
    @ObservedObject private var push = PushRegistration.shared
    @State private var permits: [Permit] = []
    @State private var copies: JSONValue?
    @State private var notifying = false

    var body: some View {
        VStack(alignment: .leading, spacing: 26) {
            #if os(iOS)
            RowGroup(title: "通知", footnote: "有事找你时（主动问候、后台任务做完了、有个操作等你确认），App 开着会自己提醒；App 关着时靠推送。推送经 Apple 的服务转发，需要先在 Mac 上的网页版「设置 → 通知」里填你自己的 Apple 推送密钥。") {
                InfoRow(title: "这台设备接收推送", detail: push.problem ?? (push.sent != nil ? "已登记到 Mac" : notifying ? "已允许通知，等待 Apple 分配地址" : "还没有打开")) {
                    if push.sent == nil { Button("打开") { Task { await push.enable(); notifying = true; await push.send(store: store) } }.buttonStyle(.jurvis(.secondary, .small)) }
                    else { Image(systemName: "checkmark.circle.fill").foregroundStyle(Color.signal) }
                }
            }
            #endif
            RowGroup(title: "日历与通讯录", footnote: "打开后，这台设备会把所选的内容复制一份送到你的 Mac，之后每次打开 App 更新。默认只有本机模型能读，要让云端模型也能读，在 Mac 上的网页版设置里逐项打开。Jurvis 只会新增日程和提醒，不改不删。"
                     + (personal.problem.map { "\n" + $0 } ?? "")) {
                ForEach(PersonalSync.Kind.allCases, id: \.self) { kind in
                    InfoRow(title: kind.title, detail: detail(kind)) {
                        Toggle("", isOn: Binding(get: { personal.shares(kind) }, set: { on in Task { await personal.set(kind, on, store: store); await load() } })).labelsHidden()
                    }
                }
            }
            RowGroup(title: "不用再问的操作", footnote: "确认卡片上选了「以后都允许」的那一类操作会列在这里，随时可以收回。电话、邮件、门锁和摄像头每次都问。") {
                if permits.isEmpty { InfoRow(title: "现在每一类操作都会先问你") { EmptyView() } }
                ForEach(permits) { permit in
                    InfoRow(title: permit.label.replacingOccurrences(of: "以后", with: "", options: .anchored).replacingOccurrences(of: "都不用问", with: ""),
                            detail: permit.uses > 0 ? "用过 \(permit.uses) 次" : "还没用过") {
                        Button("收回") { Task { await revoke(permit) } }.buttonStyle(.jurvis(.secondary, .small))
                    }
                }
            }
        }
        .task { await load(); await push.send(store: store) }
    }

    func detail(_ kind: PersonalSync.Kind) -> String {
        guard personal.shares(kind) else { return kind.detail }
        let row = copies?["kinds"].array.first { $0["kind"].string == kind.rawValue }
        guard let row, let at = row["synced_at"].number else { return "正在送到 Mac…" }
        return "\(Int(row["count"].number ?? 0)) 条 · \(at.dayAndTime) 更新" + (row["cloud"] == .bool(true) ? " · 云端模型也能读" : " · 仅本机模型")
    }

    func load() async {
        guard let api = store.api else { return }
        if let list: PermitList = try? await api.request("/api/permits") { permits = list.permits }
        copies = try? await api.request("/api/personal")
    }

    func revoke(_ permit: Permit) async {
        guard let api = store.api, let list: PermitList = try? await api.request("/api/permits/\(permit.id)", method: "DELETE") else { return }
        permits = list.permits
    }
}
