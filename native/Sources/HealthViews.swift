import SwiftUI
import Charts
import UserNotifications
#if os(iOS)
import UIKit
#else
import AppKit
#endif
#if os(iOS) && canImport(HealthKit)
import HealthKit
#endif

/// 健康: sleep, activity, heart and recovery from Apple Health (read on the iPhone), WHOOP and Google Health, and the menstrual cycle.
/// The page reads them straight from the Mac; models see only period summaries, and the cycle only the local model.
struct HealthView: View {
    @EnvironmentObject var store: AppStore
    @Environment(\.openURL) private var openURL
    @State private var page: JSONValue?
    @State private var metric = "sleep_min"
    @State private var syncing = false
    static let metrics: [(key: String, name: String, scale: Double, unit: String)] = [
        ("sleep_min", "睡眠", 60, "小时"), ("steps", "步数", 1, "步"), ("resting_hr", "静息心率", 1, "次/分"), ("hrv_ms", "心率变异性", 1, "毫秒"), ("recovery", "恢复", 1, "%")]

    var body: some View {
        Page(title: "健康", subtitle: "睡眠、运动、心率和经期都只存在你的 Mac 上。问 Jurvis 时模型只看到汇总；经期记录只给本机模型看。", width: 860) {
            #if os(iOS) && canImport(HealthKit)
            Button { Task { await syncApple() } } label: { Label(syncing ? "正在同步" : "同步 Apple 健康", systemImage: "arrow.triangle.2.circlepath") }
                .buttonStyle(.jurvis(.primary)).disabled(syncing)
            #endif
        } content: {
            if let page {
                accounts(page)
                summary(page["summary"])
                chart(page["days"].array)
                CycleCard(cycle: page["cycle"], flowDays: Set(page["flow_days"].array.map { $0["day"].string })) { await load() }
                workouts(page["workouts"].array)
            } else {
                LoadingState(text: "正在载入健康数据")
            }
        }
        .task { while !Task.isCancelled { await load(); try? await Task.sleep(for: .seconds(10)) } }
    }

    func load() async {
        guard let next: JSONValue = try? await store.requireAPI().request("/api/health?days=30") else { return }
        if next != page { page = next }
    }

    #if os(iOS) && canImport(HealthKit)
    func syncApple() async {
        syncing = true
        defer { syncing = false }
        do { _ = try await HealthKitSync.shared.sync(store: store, force: true); await load() } catch { store.report(error) }
    }
    #endif

    func accounts(_ page: JSONValue) -> some View {
        VStack(spacing: 0) {
            ForEach(Array(page["accounts"].array.enumerated()), id: \.offset) { n, c in
                if n > 0 { Divider().overlay(Color.hairline) }
                HealthConnectorRow(connector: c) { await load() }.padding(.horizontal, 14)
            }
        }
        .surface()
    }

    func summary(_ s: JSONValue) -> some View {
        Card {
            if (s["days_with_data"].number ?? 0) == 0 {
                EmptyState(icon: "heart.text.square", title: "还没有健康数据", detail: "同步 Apple 健康，或者连接 WHOOP / Google Health。")
            } else {
                Text("最近 7 天 · \(s["from"].string) 至 \(s["to"].string)").font(.jSmall).foregroundStyle(Color.inkSoft)
                LazyVGrid(columns: [GridItem(.flexible(), spacing: 16), GridItem(.flexible())], alignment: .leading, spacing: 8) {
                    ForEach(facts(s), id: \.0) { label, value in
                        HStack { Text(label).foregroundStyle(Color.inkFaint); Spacer(minLength: 4); Text(value).monospacedDigit() }.font(.jCallout)
                    }
                }
                if case .object = s["stress"] {
                    VStack(alignment: .leading, spacing: 3) {
                        Text("身体压力：" + s["stress"]["level"].string).font(.jHeading).foregroundStyle(s["stress"]["level"].string == "平稳" ? Color.ink : Color.danger)
                        if !s["stress"]["signs"].array.isEmpty { Text(s["stress"]["signs"].array.map(\.string).joined(separator: "；")).font(.jCallout) }
                        Text(s["stress"]["note"].string).font(.jSmall).foregroundStyle(Color.inkFaint)
                    }
                    .padding(10).frame(maxWidth: .infinity, alignment: .leading)
                    .background(Color.bubble, in: RoundedRectangle(cornerRadius: 10, style: .continuous))
                }
            }
        }
    }

    func facts(_ s: JSONValue) -> [(String, String)] {
        var out: [(String, String)] = []
        if case .object = s["sleep"] { out += [("平均睡眠", s["sleep"]["average"].string), ("不足 6 小时", "\(Int(s["sleep"]["short_nights_under_6h"].number ?? 0)) 晚")] }
        if let steps = s["activity"]["steps_average"].number { out.append(("日均步数", Int(steps).formatted())) }
        if let n = s["activity"]["workouts"].number, n > 0 { out.append(("运动", "\(Int(n)) 次 · \(Int(s["activity"]["workout_minutes"].number ?? 0)) 分钟")) }
        if let v = s["heart"]["resting_hr_average"].number { out.append(("静息心率", "\(Int(v)) 次/分")) }
        if let v = s["heart"]["hrv_average_ms"].number { out.append(("心率变异性", "\(Int(v)) 毫秒")) }
        if let v = s["recovery"]["average"].number { out.append(("平均恢复", "\(Int(v))%")) }
        if let v = s["mood"]["average_1_to_5"].number { out.append(("心情", "\(v.formatted(.number.precision(.fractionLength(0...1)))) / 5")) }
        return out
    }

    func chart(_ days: [JSONValue]) -> some View {
        let available = Self.metrics.filter { m in days.contains { $0[m.key].number != nil } }
        let current = available.first { $0.key == metric } ?? available.first ?? Self.metrics[0]
        let points = days.compactMap { day -> (String, Double)? in day[current.key].number.map { (String(day["day"].string.suffix(5)), $0 / current.scale) } }
        return Card {
            HStack {
                Text("最近 30 天").font(.jHeading)
                Spacer()
                if available.count > 1 {
                    Picker("指标", selection: $metric) { ForEach(available, id: \.key) { Text($0.name).tag($0.key) } }
                        .pickerStyle(.menu).labelsHidden().fixedSize()
                }
            }
            if points.isEmpty {
                Text("还没有数据").font(.jCallout).foregroundStyle(Color.inkSoft)
            } else {
                Chart(points, id: \.0) { point in
                    BarMark(x: .value("日期", point.0), y: .value(current.unit, point.1)).foregroundStyle(Color.ink.opacity(0.6)).cornerRadius(2)
                }
                .chartXAxis(.hidden)
                .frame(height: 140)
                .accessibilityLabel("最近 30 天\(current.name)")
                HStack { Text(points.first!.0.replacingOccurrences(of: "-", with: "/")); Spacer(); Text("今天") }.font(.jSmall).foregroundStyle(Color.inkFaint)
            }
        }
    }

    func workouts(_ list: [JSONValue]) -> some View {
        Card {
            Text("运动").font(.jHeading)
            if list.isEmpty {
                Text("最近 30 天没有运动记录。").font(.jCallout).foregroundStyle(Color.inkSoft)
            } else {
                ForEach(Array(list.prefix(20).enumerated()), id: \.offset) { index, w in
                    if index > 0 { Divider().overlay(Color.hairline) }
                    HStack(spacing: 12) {
                        VStack(alignment: .leading, spacing: 2) {
                            Text(w["kind_label"].string).font(.jHeading)
                            Text((w["start"].number ?? 0).dayAndTime).font(.jSmall).foregroundStyle(Color.inkSoft)
                        }
                        Spacer(minLength: 8)
                        Text([w["distance_km"].number.map { "\($0.formatted()) 公里" }, w["kcal"].number.map { "\(Int($0)) 千卡" }].compactMap { $0 }.joined(separator: " · "))
                            .font(.jSmall).foregroundStyle(Color.inkSoft)
                        Text("\(Int(w["minutes"].number ?? 0)) 分钟").font(.jHeading).monospacedDigit()
                    }
                }
            }
        }
    }

    func ago(_ seconds: Double?) -> String {
        guard let seconds else { return "" }
        let minutes = Int((Date().timeIntervalSince1970 - seconds) / 60)
        return minutes < 2 ? "刚刚" : minutes < 60 ? "\(minutes) 分钟前" : minutes < 1440 ? "\(minutes / 60) 小时前" : "\(minutes / 1440) 天前"
    }



}

/// Where the user is in their cycle, the days around today, and buttons to say it started or ended.
struct CycleCard: View {
    @EnvironmentObject var store: AppStore
    var cycle: JSONValue
    var flowDays: Set<String>
    var changed: () async -> Void
    static let day: DateFormatter = { let f = DateFormatter(); f.calendar = Calendar(identifier: .gregorian); f.locale = Locale(identifier: "en_US_POSIX"); f.dateFormat = "yyyy-MM-dd"; return f }()

    var body: some View {
        let today = Self.day.string(from: Date())
        Card {
            HStack {
                Text("经期").font(.jHeading)
                Spacer()
                if flowDays.contains(today) {
                    Button("今天结束了") { Task { await send(["ended": true, "day": today]) } }.buttonStyle(.jurvis(.secondary, .small))
                } else {
                    Button("今天来了") { Task { await send(["start": today]) } }.buttonStyle(.jurvis(.primary, .small))
                }
            }
            if (cycle["periods_recorded"].number ?? 0) == 0 {
                Text("还没有记录。对 Jurvis 说「今天来例假了」，或者点上面的按钮；Apple 健康里的经期记录也会同步过来。").font(.jCallout).foregroundStyle(Color.inkSoft)
            } else {
                let until = Int(cycle["days_until_next"].number ?? 0)
                HStack(alignment: .firstTextBaseline, spacing: 6) {
                    Text(until >= 0 ? "\(until) 天后" : "随时可能来").font(.system(size: 28, weight: .semibold)).monospacedDigit()
                    Text("预计 " + cycle["next_period_estimate"].string.suffix(5).replacingOccurrences(of: "-", with: "/")).font(.jCallout).foregroundStyle(Color.inkSoft)
                }
                Text("周期第 \(Int(cycle["cycle_day_today"].number ?? 0)) 天 · \(cycle["phase"].string) · 平均周期 \(Int(cycle["average_cycle_days"].number ?? 28)) 天，经期 \(Int(cycle["average_period_days"].number ?? 5)) 天")
                    .font(.jSmall).foregroundStyle(Color.inkSoft)
                strip(today)
                if !cycle["common_symptoms_90_days"].array.isEmpty { FlowChips(items: cycle["common_symptoms_90_days"].array.map(\.string)) }
                Text("根据你自己的记录估算，不能用于避孕。").font(.jSmall).foregroundStyle(Color.inkFaint)
            }
        }
    }

    func strip(_ today: String) -> some View {
        let next = Self.day.date(from: cycle["next_period_estimate"].string)
        let length = Int(cycle["average_period_days"].number ?? 5)
        return HStack(spacing: 3) {
            ForEach(-20...20, id: \.self) { offset in
                let date = Calendar.current.date(byAdding: .day, value: offset, to: Date())!
                let key = Self.day.string(from: date)
                let predicted = next.map { date >= Calendar.current.startOfDay(for: $0) && date < Calendar.current.date(byAdding: .day, value: length, to: $0)! } ?? false
                RoundedRectangle(cornerRadius: 3, style: .continuous)
                    .fill(flowDays.contains(key) ? Color(red: 0.88, green: 0.11, blue: 0.28) : predicted ? Color(red: 0.99, green: 0.64, blue: 0.69) : Color.hairline)
                    .overlay(RoundedRectangle(cornerRadius: 3, style: .continuous).strokeBorder(offset == 0 ? Color.ink : .clear, lineWidth: 1.5))
                    .frame(height: 14)
            }
        }
        .accessibilityLabel("前后 20 天的经期和预计")
    }

    func send(_ body: [String: Any]) async {
        do { let _: JSONValue = try await store.requireAPI().request("/api/health/period", method: "POST", body: body); await changed() } catch { store.report(error) }
    }
}

#if os(iOS) && canImport(HealthKit)
/// Reads the iPhone's Apple Health data and posts daily figures, workouts, cycle days and moods to the Mac.
/// Nothing is sent anywhere else; it runs when the user turns it on, then each time the app comes to the front (at most every 30 minutes).
@MainActor
final class HealthKitSync {
    static let shared = HealthKitSync()
    private let health = HKHealthStore()
    private let enabledKey = "healthkit.enabled", lastKey = "healthkit.last"
    var enabled: Bool { UserDefaults.standard.bool(forKey: enabledKey) }

    private let sums: [(HKQuantityTypeIdentifier, String, HKUnit)] = [
        (.stepCount, "steps", .count()), (.activeEnergyBurned, "active_kcal", .kilocalorie()), (.appleExerciseTime, "exercise_min", .minute()),
        (.distanceWalkingRunning, "distance_km", .meterUnit(with: .kilo))]
    private let averages: [(HKQuantityTypeIdentifier, String, HKUnit)] = [
        (.restingHeartRate, "resting_hr", HKUnit.count().unitDivided(by: .minute())), (.heartRateVariabilitySDNN, "hrv_ms", .secondUnit(with: .milli)),
        (.oxygenSaturation, "spo2", .percent()), (.respiratoryRate, "resp_rate", HKUnit.count().unitDivided(by: .minute())), (.bodyMass, "weight_kg", .gramUnit(with: .kilo))]

    private var readTypes: Set<HKObjectType> {
        var types = Set<HKObjectType>((sums + averages).map { HKQuantityType($0.0) })
        types.formUnion([HKCategoryType(.sleepAnalysis), HKCategoryType(.mindfulSession), HKCategoryType(.menstrualFlow), HKObjectType.workoutType(), HKObjectType.stateOfMindType()])
        return types
    }

    /// Runs when the app comes to the front; does nothing until the user has turned it on once.
    func syncIfDue(store: AppStore?, every: Double = 1800) async {
        guard enabled, Date().timeIntervalSince1970 - UserDefaults.standard.double(forKey: lastKey) > every else { return }
        _ = try? await sync(store: store, force: false)
    }

    /// New sleep, heart-rate variability, resting heart rate or a mood logged in Health wakes the app, even in the background,
    /// and is sent to the Mac right away, so Jurvis can check in on the user as it happens instead of the next time they open the app.
    /// Set up as the app launches (a background launch included), once the user has turned Apple Health on.
    private var watching = false
    func watch() {
        guard enabled, !watching, HKHealthStore.isHealthDataAvailable() else { return }
        watching = true
        let types: [HKSampleType] = [HKQuantityType(.heartRateVariabilitySDNN), HKQuantityType(.restingHeartRate), HKCategoryType(.sleepAnalysis), HKObjectType.stateOfMindType()]
        for type in types {
            let query = HKObserverQuery(sampleType: type, predicate: nil) { _, done, error in
                guard error == nil else { done(); return }
                Task { @MainActor in
                    await HealthKitSync.shared.syncIfDue(store: nil, every: 300)
                    done()
                }
            }
            health.execute(query)
            health.enableBackgroundDelivery(for: type, frequency: .immediate) { _, _ in }
        }
    }

    func sync(store: AppStore?, force: Bool) async throws -> JSONValue {
        // In the background there may be no window and no store yet: the pairing in the keychain is enough to reach the Mac.
        guard let api = (try? store?.requireAPI()) ?? Keychain.load().map({ API($0) }) else { throw APIError.message("请先连接 Mac") }
        guard HKHealthStore.isHealthDataAvailable() else { throw HealthKitError.unavailable }
        try await health.requestAuthorization(toShare: [], read: readTypes)
        UserDefaults.standard.set(true, forKey: enabledKey)
        watch()
        let last = UserDefaults.standard.double(forKey: lastKey)
        let calendar = Calendar.current
        let today = calendar.startOfDay(for: Date())
        let days = last > 0 && !force ? max(3, Int((Date().timeIntervalSince1970 - last) / 86400) + 2) : 30
        let start = calendar.date(byAdding: .day, value: -(days - 1), to: today)!
        var daily: [String: [String: Double]] = [:]
        let key = { (date: Date) in CycleCard.day.string(from: date) }

        for (id, name, unit) in sums + averages {
            let isSum = sums.contains { $0.0 == id }
            let query = HKStatisticsCollectionQueryDescriptor(
                predicate: .quantitySample(type: HKQuantityType(id), predicate: HKQuery.predicateForSamples(withStart: start, end: Date())),
                options: isSum ? .cumulativeSum : .discreteAverage, anchorDate: today, intervalComponents: DateComponents(day: 1))
            guard let collection = try? await query.result(for: health) else { continue }
            collection.enumerateStatistics(from: start, to: Date()) { stats, _ in
                guard let quantity = isSum ? stats.sumQuantity() : stats.averageQuantity() else { return }
                var value = quantity.doubleValue(for: unit)
                if id == .oxygenSaturation { value *= 100 }
                daily[key(stats.startDate), default: [:]][name] = (value * 10).rounded() / 10
            }
        }

        // Sleep: what was asleep, merged across the watch and the phone so nothing is counted twice; a night belongs to the day it ends.
        let sleep = try await samples(HKCategoryType(.sleepAnalysis), from: calendar.date(byAdding: .hour, value: -12, to: start)!)
        var asleep: [String: [(Date, Date)]] = [:], deep: [String: Double] = [:], rem: [String: Double] = [:], awake: [String: Double] = [:], inBed: [String: [(Date, Date)]] = [:]
        for case let s as HKCategorySample in sleep {
            let day = key(s.endDate), minutes = s.endDate.timeIntervalSince(s.startDate) / 60
            switch HKCategoryValueSleepAnalysis(rawValue: s.value) {
            case .asleepDeep: deep[day, default: 0] += minutes; asleep[day, default: []].append((s.startDate, s.endDate))
            case .asleepREM: rem[day, default: 0] += minutes; asleep[day, default: []].append((s.startDate, s.endDate))
            case .asleepCore, .asleepUnspecified: asleep[day, default: []].append((s.startDate, s.endDate))
            case .awake: awake[day, default: 0] += minutes
            case .inBed: inBed[day, default: []].append((s.startDate, s.endDate))
            default: break
            }
        }
        for (day, spans) in asleep { daily[day, default: [:]]["sleep_min"] = merged(spans).rounded() }
        for (day, spans) in inBed { daily[day, default: [:]]["in_bed_min"] = merged(spans).rounded() }
        for (day, v) in deep { daily[day, default: [:]]["deep_min"] = v.rounded() }
        for (day, v) in rem { daily[day, default: [:]]["rem_min"] = v.rounded() }
        for (day, v) in awake { daily[day, default: [:]]["awake_min"] = v.rounded() }
        for case let s as HKCategorySample in try await samples(HKCategoryType(.mindfulSession), from: start) {
            daily[key(s.startDate), default: [:]]["mindful_min", default: 0] += (s.endDate.timeIntervalSince(s.startDate) / 60).rounded()
        }

        // The cycle: HealthKit's flow values 1 unspecified, 2 light, 3 medium, 4 heavy, 5 none.
        let flows = [1: "unspecified", 2: "light", 3: "medium", 4: "heavy", 5: "none"]
        let cycle = try await samples(HKCategoryType(.menstrualFlow), from: calendar.date(byAdding: .day, value: -(force || last == 0 ? 180 : days), to: today)!)
            .compactMap { $0 as? HKCategorySample }
            .map { ["day": key($0.startDate), "flow": flows[$0.value] ?? "unspecified"] }

        let workouts = try await samples(HKObjectType.workoutType(), from: start).compactMap { $0 as? HKWorkout }.map { w -> [String: Any] in
            var item: [String: Any] = ["id": w.uuid.uuidString, "start": w.startDate.timeIntervalSince1970, "end": w.endDate.timeIntervalSince1970, "kind": Self.kind(w.workoutActivityType)]
            if let kcal = w.statistics(for: HKQuantityType(.activeEnergyBurned))?.sumQuantity()?.doubleValue(for: .kilocalorie()) { item["kcal"] = kcal.rounded() }
            for distance in [HKQuantityTypeIdentifier.distanceWalkingRunning, .distanceCycling, .distanceSwimming] {
                if let km = w.statistics(for: HKQuantityType(distance))?.sumQuantity()?.doubleValue(for: .meterUnit(with: .kilo)) { item["distance_km"] = (km * 100).rounded() / 100; break }
            }
            if let hr = w.statistics(for: HKQuantityType(.heartRate))?.averageQuantity()?.doubleValue(for: HKUnit.count().unitDivided(by: .minute())) { item["avg_hr"] = hr.rounded() }
            return item
        }

        // How they felt, logged in the Health app (State of Mind): valence -1…1 becomes 1…5.
        let moods = try await samples(HKObjectType.stateOfMindType(), from: start).compactMap { $0 as? HKStateOfMind }.map {
            ["id": $0.uuid.uuidString, "at": $0.startDate.timeIntervalSince1970, "score": Int((($0.valence + 1) * 2).rounded()) + 1] as [String: Any]
        }

        let body: [String: Any] = ["source": "apple", "days": daily.map { ["day": $0.key, "metrics": $0.value] }, "workouts": workouts, "cycle": cycle, "moods": moods]
        let result: JSONValue = try await api.request("/api/health/import", method: "POST", body: body)
        UserDefaults.standard.set(Date().timeIntervalSince1970, forKey: lastKey)
        return result
    }

    private func samples(_ type: HKSampleType, from start: Date) async throws -> [HKSample] {
        let descriptor = HKSampleQueryDescriptor(predicates: [.sample(type: type, predicate: HKQuery.predicateForSamples(withStart: start, end: Date()))],
                                                 sortDescriptors: [SortDescriptor(\.startDate)], limit: 5000)
        return (try? await descriptor.result(for: health)) ?? []
    }

    /// Minutes covered by a set of time spans, overlaps counted once.
    private func merged(_ spans: [(Date, Date)]) -> Double {
        var total = 0.0, end = Date.distantPast
        for (a, b) in spans.sorted(by: { $0.0 < $1.0 }) {
            if b <= end { continue }
            total += b.timeIntervalSince(max(a, end))
            end = b
        }
        return total / 60
    }

    static func kind(_ type: HKWorkoutActivityType) -> String {
        switch type {
        case .running: return "running"
        case .walking: return "walking"
        case .cycling: return "cycling"
        case .swimming: return "swimming"
        case .traditionalStrengthTraining, .functionalStrengthTraining, .coreTraining: return "strength"
        case .yoga: return "yoga"
        case .highIntensityIntervalTraining: return "hiit"
        case .hiking: return "hiking"
        case .rowing: return "rowing"
        case .elliptical: return "elliptical"
        case .pilates: return "pilates"
        case .socialDance, .cardioDance: return "dance"
        case .tennis: return "tennis"
        case .basketball: return "basketball"
        case .soccer: return "football"
        case .badminton: return "badminton"
        case .climbing: return "climbing"
        default: return "other"
        }
    }
}

enum HealthKitError: LocalizedError {
    case unavailable
    var errorDescription: String? { "这台设备不能读取 Apple 健康数据" }
}
#endif

/// 健康与运动: Apple Health and Apple Fitness (read on the iPhone), WHOOP and Google Health (an OAuth app of the user's own, entered here).
struct HealthConnectors: View {
    @EnvironmentObject var store: AppStore
    @State private var list: [JSONValue] = []

    var body: some View {
        RowGroup(title: "健康与运动", footnote: "睡眠、运动、心率和经期只存在 Mac 上；问 Jurvis 时模型只看到汇总数字。") {
            ForEach(Array(list.enumerated()), id: \.offset) { _, c in
                HealthConnectorRow(connector: c) { await load() }
            }
            if list.isEmpty { InfoRow(title: "正在读取…") { EmptyView() } }
        }
        .task { await load() }
    }

    func load() async {
        if let next: JSONValue = try? await store.requireAPI().request("/api/health/connectors") { list = next["connectors"].array }
    }
}

/// One health connector: what it brings, where it stands, and the buttons that connect, sync or set it up.
struct HealthConnectorRow: View {
    @EnvironmentObject var store: AppStore
    @Environment(\.openURL) private var openURL
    var connector: JSONValue
    var changed: () async -> Void
    @State private var settingUp = false
    @State private var busy = false

    var provider: String { connector["provider"].string }
    var device: Bool { connector["kind"].string == "device" }
    var connected: Bool { connector["connected"] == .bool(true) }
    var configured: Bool { connector["configured"] == .bool(true) }

    var body: some View {
        HStack(alignment: .top, spacing: 12) {
            Image(systemName: icon).font(.system(size: 17)).foregroundStyle(connected ? Color.signal : Color.inkSoft).frame(width: 24).padding(.top, 2)
            VStack(alignment: .leading, spacing: 2) {
                Text(connector["name"].string).font(.jHeading)
                Text(connector["detail"].string).font(.jSmall).foregroundStyle(Color.inkSoft).fixedSize(horizontal: false, vertical: true)
                Text(status).font(.jSmall).foregroundStyle(connector["error"].string.isEmpty ? Color.inkFaint : Color.danger).fixedSize(horizontal: false, vertical: true)
            }
            Spacer(minLength: 8)
            ViewThatFits { HStack(spacing: 6) { buttons }; VStack(alignment: .trailing, spacing: 6) { buttons } }
        }
        .padding(.vertical, 10)
        .sheet(isPresented: $settingUp) { HealthClientSheet(connector: connector) { await changed() } }
    }

    var icon: String { ["apple": "heart.fill", "fitness": "figure.run", "whoop": "waveform.path.ecg", "google": "g.circle"][provider] ?? "heart" }

    var status: String {
        let ago = HealthConnectorRow.ago(connector["synced_at"].number)
        if device {
            if connected { return "已同步 · \(ago)" + (provider == "fitness" ? " · 最近 30 天 \(Int(connector["workouts_30_days"].number ?? 0)) 次运动" : "") }
            #if os(iOS)
            return "点「连接」，允许 Jurvis 读取健康数据"
            #else
            return "在 iPhone 上打开 Jurvis，在这里点「连接」"
            #endif
        }
        if connected { return connector["error"].string.isEmpty ? (connector["synced_at"].number == nil ? "已连接 · 正在同步" : "已连接 · 同步于 \(ago)") : connector["error"].string }
        if configured { return connector["on_this_mac"] == .bool(true) ? "未连接 · 请在 Mac 上点「连接」" : "未连接" }
        return "需要先填一个你自己的开发者应用（只需一次）"
    }

    @ViewBuilder var buttons: some View {
        if device {
            #if os(iOS) && canImport(HealthKit)
            Button(busy ? "正在同步" : connected ? "同步" : "连接") { Task { await syncApple() } }.buttonStyle(.jurvis(connected ? .secondary : .primary, .small)).disabled(busy)
            #endif
        } else if connected {
            Button("同步") { Task { await call("/api/health/sync/\(provider)", "POST") } }.buttonStyle(.jurvis(.secondary, .small)).disabled(busy)
            Button("断开") { Task { await call("/api/health/connect/\(provider)", "DELETE") } }.buttonStyle(.jurvis(.ghost, .small))
        } else {
            Button("设置") { settingUp = true }.buttonStyle(.jurvis(configured ? .ghost : .secondary, .small))
            if configured { Button("连接") { Task { await connect() } }.buttonStyle(.jurvis(.primary, .small)) }
        }
    }

    static func ago(_ seconds: Double?) -> String {
        guard let seconds else { return "" }
        let minutes = Int((Date().timeIntervalSince1970 - seconds) / 60)
        return minutes < 2 ? "刚刚" : minutes < 60 ? "\(minutes) 分钟前" : minutes < 1440 ? "\(minutes / 60) 小时前" : "\(minutes / 1440) 天前"
    }

    #if os(iOS) && canImport(HealthKit)
    func syncApple() async {
        busy = true
        defer { busy = false }
        do { _ = try await HealthKitSync.shared.sync(store: store, force: true); await changed() } catch { store.report(error) }
    }
    #endif

    func connect() async {
        do {
            let reply: JSONValue = try await store.requireAPI().request("/api/health/connect/\(provider)", method: "POST")
            if let url = URL(string: reply["url"].string) { openURL(url) }
        } catch { store.report(error) }
    }

    func call(_ path: String, _ method: String) async {
        busy = true
        defer { busy = false }
        do { let _: JSONValue = try await store.requireAPI().request(path, method: method); await changed() } catch { store.report(error) }
    }
}

/// The user's own WHOOP or Google OAuth app: where to make it, the callback address to give it, and its ID and secret.
struct HealthClientSheet: View {
    @EnvironmentObject var store: AppStore
    @Environment(\.dismiss) private var dismiss
    @Environment(\.openURL) private var openURL
    var connector: JSONValue
    var saved: () async -> Void
    @State private var clientID = ""
    @State private var secret = ""
    @State private var busy = false

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            Text(connector["name"].string).font(.jTitle)
            Text(connector["setup"].string).font(.jSmall).foregroundStyle(Color.inkSoft).fixedSize(horizontal: false, vertical: true)
            Button("打开开发者后台") { if let url = URL(string: connector["console"].string) { openURL(url) } }.buttonStyle(.jurvis(.ghost, .small))
            VStack(alignment: .leading, spacing: 4) {
                Text("回调地址").font(.jSmall).foregroundStyle(Color.inkFaint)
                Text(connector["redirect_uri"].string).font(.jMono).textSelection(.enabled)
            }
            if connector["client_from"].string == "env" {
                Text("现在用的是 .env 里的应用；在这里保存的只在 .env 没填时生效。").font(.jSmall).foregroundStyle(Color.inkSoft)
            }
            TextField("Client ID", text: $clientID).textFieldStyle(.roundedBorder).autocorrectionDisabled()
            SecureField(connector["configured"] == .bool(true) ? "Client Secret（留空则保留已保存的）" : "Client Secret", text: $secret).textFieldStyle(.roundedBorder)
            Text("只保存在 Mac 上，不会再发回任何设备。").font(.jSmall).foregroundStyle(Color.inkFaint)
            HStack {
                Spacer()
                Button("取消") { dismiss() }.buttonStyle(.jurvis(.ghost))
                Button(busy ? "正在保存" : "保存") { Task { await save() } }.buttonStyle(.jurvis(.primary)).disabled(busy || clientID.isEmpty)
            }
        }
        .padding(24).frame(minWidth: 420)
        .onAppear { clientID = connector["client_id"].string }
    }

    func save() async {
        busy = true
        defer { busy = false }
        do {
            let _: JSONValue = try await store.requireAPI().request("/api/health/client/\(connector["provider"].string)", method: "PUT",
                body: ["client_id": clientID.trimmingCharacters(in: .whitespaces), "client_secret": secret])
            await saved()
            dismiss()
        } catch { store.report(error) }
    }
}

/// 邮件: mailboxes Jurvis may read (IMAP, read-only). Passwords stay on the Mac; by default only the local model reads them.
struct MailSettings: View {
    @EnvironmentObject var store: AppStore
    @State private var page: JSONValue?
    @State private var adding = false

    var body: some View {
        RowGroup(title: "邮件", footnote: "只读：Jurvis 只看、不发、不标已读。密码只存在 Mac 上，默认只有本机模型能读邮件。") {
            ForEach(Array((page?["accounts"].array ?? []).enumerated()), id: \.offset) { _, a in
                InfoRow(title: a["address"].string, detail: a["host"].string + " · " + (a["cloud"] == .bool(true) ? "本机和云端模型都能读" : "只有本机模型能读")) {
                    Button(a["cloud"] == .bool(true) ? "只给本机" : "也给云端") { Task { await share(a) } }.buttonStyle(.jurvis(.ghost, .small))
                    Button("移除") { Task { await remove(a["id"].string) } }.buttonStyle(.jurvis(.ghost, .small))
                }
            }
            InfoRow(title: (page?["accounts"].array ?? []).isEmpty ? "还没有连接邮箱" : "再添加一个") {
                Button("添加邮箱") { adding = true }.buttonStyle(.jurvis(.secondary, .small))
            }
        }
        .task { await load() }
        .sheet(isPresented: $adding) { AddMailSheet(presets: page?["presets"].array ?? []) { await load() } }
    }

    func load() async { if let next: JSONValue = try? await store.requireAPI().request("/api/mail") { page = next } }

    func share(_ a: JSONValue) async {
        do {
            let _: JSONValue = try await store.requireAPI().request("/api/mail/\(a["id"].string)", method: "PUT",
                body: ["kind": a["kind"].string, "address": a["address"].string, "host": a["host"].string, "cloud": a["cloud"] != .bool(true)])
            await load()
        } catch { store.report(error) }
    }

    func remove(_ id: String) async {
        do { let _: JSONValue = try await store.requireAPI().request("/api/mail/\(id)", method: "DELETE"); await load() } catch { store.report(error) }
    }
}

struct AddMailSheet: View {
    @EnvironmentObject var store: AppStore
    @Environment(\.dismiss) private var dismiss
    var presets: [JSONValue]
    var saved: () async -> Void
    @State private var kind = "gmail"
    @State private var address = ""
    @State private var password = ""
    @State private var host = ""
    @State private var busy = false
    static let names = ["gmail": "Gmail", "icloud": "iCloud", "outlook": "Outlook", "qq": "QQ 邮箱", "163": "163 邮箱", "126": "126 邮箱", "imap": "其他（IMAP）"]

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            Text("添加邮箱").font(.jTitle)
            Picker("邮箱", selection: $kind) { ForEach(presets.map { $0["kind"].string }, id: \.self) { Text(Self.names[$0] ?? $0).tag($0) } }
            Text(presets.first { $0["kind"].string == kind }?["hint"].string ?? "").font(.jSmall).foregroundStyle(Color.inkSoft)
            TextField("地址", text: $address).textFieldStyle(.roundedBorder).autocorrectionDisabled()
            SecureField("应用专用密码或授权码", text: $password).textFieldStyle(.roundedBorder)
            if kind == "imap" { TextField("IMAP 服务器，例如 imap.example.com", text: $host).textFieldStyle(.roundedBorder).autocorrectionDisabled() }
            HStack {
                Spacer()
                Button("取消") { dismiss() }.buttonStyle(.jurvis(.ghost))
                Button(busy ? "正在连接" : "连接") { Task { await save() } }.buttonStyle(.jurvis(.primary)).disabled(busy || address.isEmpty || password.isEmpty)
            }
        }
        .padding(24).frame(minWidth: 380)
    }

    func save() async {
        busy = true
        defer { busy = false }
        let id = String(address.split(separator: "@").first ?? "mail").lowercased().filter { $0.isLetter && $0.isASCII || $0.isNumber || $0 == "-" || $0 == "_" }.prefix(24)
        do {
            let _: JSONValue = try await store.requireAPI().request("/api/mail/\(id.isEmpty ? "mail" : String(id))", method: "PUT",
                body: ["kind": kind, "address": address.trimmingCharacters(in: .whitespaces), "password": password, "host": host])
            await saved()
            dismiss()
        } catch { store.report(error) }
    }
}

/// 主动关心: the moment sleep, stress, recovery or mood clearly worsen, Jurvis checks in on its own; quiet hours hold it until morning.
struct CareSettings: View {
    @EnvironmentObject var store: AppStore
    @State private var care: JSONValue?

    var body: some View {
        RowGroup(title: "主动关心", footnote: "睡眠、压力、恢复或心情一变差，Jurvis 就会用本机模型看看最近的邮件和对话，马上主动问候你，并发通知。同一件事不重复说；安静时段先攒着，到点再发。") {
            InfoRow(title: "身体数据变差时主动问候我") {
                Toggle("", isOn: Binding(get: { care?["enabled"] == .bool(true) }, set: { on in Task { await update(["enabled": on]) } })).labelsHidden()
            }
            InfoRow(title: "安静时段", detail: "\(care?["quiet_start"].string ?? "22:30") 到 \(care?["quiet_end"].string ?? "08:30") 不打扰") { EmptyView() }
            InfoRow(title: "现在看看", detail: care?["last_at"].number.map { "上一次问候：" + $0.dayAndTime }) {
                Button("看看") { Task { await now() } }.buttonStyle(.jurvis(.secondary, .small))
            }
        }
        .task { care = try? await store.requireAPI().request("/api/checkin") }
    }

    func update(_ body: [String: Any]) async {
        do { care = try await store.requireAPI().request("/api/checkin", method: "PUT", body: body) } catch { store.report(error) }
    }

    func now() async {
        do { let _: JSONValue = try await store.requireAPI().request("/api/checkin/now", method: "POST"); store.error = nil } catch { store.report(error) }
    }
}

/// A message Jurvis wrote on its own (a check-in when body data turned), from /api/state.
struct Outreach: Codable, Equatable, Hashable { var id: String; var session_id: String; var content: String; var created_at: Double; var title: String }

/// Jurvis's own messages reach the user as a notification, once each, as soon as this app sees them.
/// Tapping one opens its conversation. Nothing is shown for the conversation the user is already looking at.
@MainActor final class Notifier: NSObject, UNUserNotificationCenterDelegate {
    static let shared = Notifier()
    weak var store: AppStore?
    private let key = "outreach.delivered"
    private var asked = false

    func deliver(_ list: [Outreach], store: AppStore) async {
        self.store = store
        let center = UNUserNotificationCenter.current()
        if center.delegate == nil { center.delegate = self }
        var done = UserDefaults.standard.stringArray(forKey: key) ?? []
        let fresh = list.filter { !done.contains($0.id) && Date().timeIntervalSince1970 - $0.created_at < 6 * 3600 }
        guard !fresh.isEmpty else { return }
        if !asked { asked = true; _ = try? await center.requestAuthorization(options: [.alert, .sound, .badge]) }
        for item in fresh.reversed() {
            done.append(item.id)
            if store.section == .assistant && !store.streamPast && Self.appActive { continue }  // it is on screen as it arrives
            let content = UNMutableNotificationContent()
            content.title = "Jurvis"
            content.body = String(item.content.prefix(180))
            content.sound = .default
            content.threadIdentifier = "outreach"
            content.userInfo = ["session_id": item.session_id]
            try? await center.add(UNNotificationRequest(identifier: item.id, content: content, trigger: nil))
        }
        UserDefaults.standard.set(Array(done.suffix(200)), forKey: key)
    }

    func open(_ session: String) async { store?.section = .assistant; await store?.loadNewest() }

    static var appActive: Bool {
        #if os(iOS)
        return UIApplication.shared.applicationState == .active
        #else
        return NSApplication.shared.isActive
        #endif
    }

    nonisolated func userNotificationCenter(_ center: UNUserNotificationCenter, willPresent notification: UNNotification) async -> UNNotificationPresentationOptions {
        [.banner, .list, .sound]
    }

    nonisolated func userNotificationCenter(_ center: UNUserNotificationCenter, didReceive response: UNNotificationResponse) async {
        let session = response.notification.request.content.userInfo["session_id"] as? String ?? ""
        guard !session.isEmpty else { return }
        await Notifier.shared.open(session)
    }
}
