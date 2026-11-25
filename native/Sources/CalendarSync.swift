import EventKit
import Foundation

/// A booking Jurvis made on the phone, waiting to be put in the calendar (engine/phone.py).
struct CalendarEntry: Codable, Equatable, Identifiable {
    var id: String
    var title: String
    var place: String
    var number: String
    /// "2026-11-28T19:00", on the clock of `timezone`; an empty time zone means this device's own clock.
    var start: String
    var timezone: String
    var minutes: Int
    var notes: String
}

/// Puts bookings in the system calendar. The engine offers each one to every app; the first to claim it adds it, to the
/// calendar new events go to by default, and the user's calendar account carries it to their other devices. Adding needs only
/// write access; reading the calendar is a separate choice the user makes in 设置 (Reach.swift).
@MainActor final class CalendarSync {
    static let shared = CalendarSync()
    private let events = EKEventStore()
    private let key = "calendar.given-up"
    private var busy = false

    func add(_ entries: [CalendarEntry], store: AppStore) async {
        guard !busy, let api = store.api else { return }
        busy = true
        defer { busy = false }
        var givenUp = UserDefaults.standard.stringArray(forKey: key) ?? []
        for entry in entries where !givenUp.contains(entry.id) {
            // Another device may have taken it a moment ago: then this one leaves it alone.
            guard let mine: CalendarEntry = try? await api.request("/api/calendar/\(entry.id)/claim", method: "POST", body: [String: String]()) else { continue }
            var ok = false
            do {
                try await save(mine)
                ok = true
            } catch {
                givenUp.append(entry.id)  // no access, or no calendar on this device: it goes back for another device to add
                UserDefaults.standard.set(Array(givenUp.suffix(200)), forKey: key)
            }
            let _: JSONValue? = try? await api.request("/api/calendar/\(entry.id)/done", method: "POST", body: ["ok": ok])
        }
    }

    private func save(_ entry: CalendarEntry) async throws {
        guard try await events.requestWriteOnlyAccessToEvents() else { throw CalendarError.denied }
        guard let calendar = events.defaultCalendarForNewEvents, let start = Self.date(entry.start, zone: entry.timezone) else { throw CalendarError.unusable }
        let event = EKEvent(eventStore: events)
        event.calendar = calendar
        event.title = entry.title
        event.location = entry.place
        event.notes = entry.notes
        event.startDate = start
        event.endDate = start.addingTimeInterval(TimeInterval(max(entry.minutes, 30) * 60))
        if let zone = TimeZone(identifier: entry.timezone) { event.timeZone = zone }  // shown on the restaurant's clock when travelling
        event.addAlarm(EKAlarm(relativeOffset: -2 * 3600))
        try events.save(event, span: .thisEvent, commit: true)
    }

    /// "2026-11-28T19:00" on the given clock (this device's when the zone is empty or unknown) as a moment in time.
    static func date(_ text: String, zone: String) -> Date? {
        let parts = text.split(whereSeparator: { "-T:".contains($0) }).compactMap { Int($0) }
        guard parts.count >= 5 else { return nil }
        var calendar = Calendar(identifier: .gregorian)
        calendar.timeZone = TimeZone(identifier: zone) ?? .current
        return calendar.date(from: DateComponents(year: parts[0], month: parts[1], day: parts[2], hour: parts[3], minute: parts[4]))
    }

    enum CalendarError: Error { case denied, unusable }
}
