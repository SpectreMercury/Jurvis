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
                HealthConnectorRow(connector: c) { await load() }
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
            ServiceMark(kind: provider, size: 36)
            VStack(alignment: .leading, spacing: 2) {
                Text(connector["name"].string).font(.jHeading)
                Text(connector["detail"].string).font(.jSmall).foregroundStyle(Color.inkSoft).fixedSize(horizontal: false, vertical: true)
                Text(status).font(.jSmall).foregroundStyle(connector["error"].string.isEmpty ? Color.inkFaint : Color.danger).fixedSize(horizontal: false, vertical: true)
            }
            Spacer(minLength: 8)
            ViewThatFits { HStack(spacing: 6) { buttons }; VStack(alignment: .trailing, spacing: 6) { buttons } }
        }
        .padding(.horizontal, 16).padding(.vertical, 11)
        .sheet(isPresented: $settingUp) { OAuthAppSheet(app: connector, title: connector["name"].string) { await changed() } }
    }

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

/// The user's own Google, WHOOP or Microsoft app: where to make it, the callback address to give it, and its ID and secret.
/// One sheet for every connector that signs in; Google Health and Gmail share the Google app.
struct OAuthAppSheet: View {
    @EnvironmentObject var store: AppStore
    @Environment(\.dismiss) private var dismiss
    @Environment(\.openURL) private var openURL
    var app: JSONValue
    var title: String
    var saved: () async -> Void
    @State private var clientID = ""
    @State private var secret = ""
    @State private var busy = false

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 14) {
                HStack(spacing: 12) {
                    ServiceMark(kind: app["app"].string, size: 40)
                    Text(title).font(.jTitle)
                }
                Text(app["setup"].string).font(.jSmall).foregroundStyle(Color.inkSoft).fixedSize(horizontal: false, vertical: true)
                Button("打开 \(app["app_name"].string) 开发者后台") { if let url = URL(string: app["console"].string) { openURL(url) } }.buttonStyle(.jurvis(.ghost, .small))
                VStack(alignment: .leading, spacing: 4) {
                    Text("回调地址").font(.jSmall).foregroundStyle(Color.inkFaint)
                    Text(app["redirect_uri"].string).font(.jMono).textSelection(.enabled)
                }
                if app["client_from"].string == "env" {
                    Text("现在用的是 .env 里的应用；在这里保存的只在 .env 没填时生效。").font(.jSmall).foregroundStyle(Color.inkSoft)
                }
                TextField("Client ID", text: $clientID).textFieldStyle(.roundedBorder).autocorrectionDisabled()
                SecureField(app["configured"] == .bool(true) ? "Client Secret（留空则保留已保存的）" : "Client Secret", text: $secret).textFieldStyle(.roundedBorder)
                Text("只保存在 Mac 上，不会再发回任何设备。").font(.jSmall).foregroundStyle(Color.inkFaint)
                HStack {
                    Spacer()
                    Button("取消") { dismiss() }.buttonStyle(.jurvis(.ghost))
                    Button(busy ? "正在保存" : "保存") { Task { await save() } }.buttonStyle(.jurvis(.primary)).disabled(busy || clientID.isEmpty)
                }
            }
            .padding(24)
        }
        .frame(minWidth: 420)
        .onAppear { clientID = app["client_id"].string }
    }

    func save() async {
        busy = true
        defer { busy = false }
        do {
            let _: JSONValue = try await store.requireAPI().request("/api/oauth/\(app["app"].string)", method: "PUT",
                body: ["client_id": clientID.trimmingCharacters(in: .whitespaces), "client_secret": secret])
            await saved()
            dismiss()
        } catch { store.report(error) }
    }
}

/// A brand's mark on a light tile, like an app icon, in the brand's colour; brands without a mark get their letters on their colour.
/// A brand's mark in its own colour on a faint wash of that colour, matching the web tile: the wash and the mark both follow
/// light and dark, so a tile never sits as a white block in a dark window. Marks without a colour use ink.
struct ServiceMark: View {
    var kind: String
    var size: CGFloat = 36
    @Environment(\.colorScheme) private var scheme

    var body: some View {
        let shape = RoundedRectangle(cornerRadius: size * 0.26, style: .continuous)
        let dark = scheme == .dark
        let ink: UInt32 = dark ? 0xEDEDED : 0x171717
        let brand = Brands.colors[kind] ?? ink
        let wash = Self.color(Self.mix(brand, dark ? 0x0A0A0A : 0xFFFFFF, dark ? 0.20 : 0.11))
        let mark = Self.color(Self.mix(brand, ink, dark ? 0.58 : 0.92))
        Group {
            if Brands.marks.contains(kind) {
                shape.fill(wash)
                    .overlay(Image("Brand-\(kind)").renderingMode(.template).resizable().scaledToFit()
                        .foregroundStyle(mark).padding(size * 0.22))
            } else {
                shape.fill(wash)
                    .overlay(Text(Brands.letters[kind] ?? String(kind.prefix(1)).uppercased())
                        .font(.system(size: size * ((Brands.letters[kind]?.count ?? 1) > 2 ? 0.3 : 0.38), weight: .semibold))
                        .foregroundStyle(mark).lineLimit(1).minimumScaleFactor(0.6).padding(.horizontal, 3))
            }
        }
        .overlay(shape.strokeBorder(Self.color(brand).opacity(0.14)))
        .frame(width: size, height: size)
        .accessibilityHidden(true)
    }

    /// `amount` of `a` laid over `b`, channel by channel, like CSS color-mix in sRGB.
    static func mix(_ a: UInt32, _ b: UInt32, _ amount: Double) -> UInt32 {
        func channel(_ shift: UInt32) -> UInt32 {
            let x = Double((a >> shift) & 0xFF), y = Double((b >> shift) & 0xFF)
            return UInt32((x * amount + y * (1 - amount)).rounded()) << shift
        }
        return channel(16) | channel(8) | channel(0)
    }

    static func color(_ hex: UInt32) -> Color {
        Color(red: Double((hex >> 16) & 0xFF) / 255, green: Double((hex >> 8) & 0xFF) / 255, blue: Double(hex & 0xFF) / 255)
    }
}

/// 邮件: mailboxes Jurvis may read, read-only. Gmail and Outlook are connected by signing in; iCloud, QQ and 163 offer no sign-in
/// for other apps, so they take an app-specific password or authorization code the user generates on the provider's site.
struct MailSettings: View {
    @EnvironmentObject var store: AppStore
    @Environment(\.openURL) private var openURL
    @State private var page: JSONValue?
    @State private var adding: JSONValue?
    @State private var settingUp: JSONValue?

    var accounts: [JSONValue] { page?["accounts"].array ?? [] }
    var presets: [JSONValue] { page?["presets"].array ?? [] }

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            RowGroup(title: "邮件", footnote: "只读：Jurvis 只看，不发信，也不标已读。Gmail 和 Outlook 用账号登录授权；默认只有本机模型能读。") {
                ForEach(Array(accounts.enumerated()), id: \.offset) { _, a in
                    HStack(spacing: 12) {
                        ServiceMark(kind: a["kind"].string, size: 36)
                        VStack(alignment: .leading, spacing: 2) {
                            Text(a["address"].string).font(.jBody).lineLimit(1).truncationMode(.middle)
                            Text(a["error"].string.isEmpty ? "\(a["name"].string) · \(a["auth"].string == "oauth" ? "已登录授权" : "专用密码") · 只读" : a["error"].string)
                                .font(.jSmall).foregroundStyle(a["error"].string.isEmpty ? Color.inkFaint : Color.danger)
                        }
                        Spacer(minLength: 8)
                        Menu {
                            Button { Task { await share(a, cloud: false) } } label: { Label("仅本机模型能读", systemImage: a["cloud"] == .bool(true) ? "" : "checkmark") }
                            Button { Task { await share(a, cloud: true) } } label: { Label("本机和云端模型都能读", systemImage: a["cloud"] == .bool(true) ? "checkmark" : "") }
                            Divider()
                            Button("移除", role: .destructive) { Task { await remove(a["id"].string) } }
                        } label: {
                            Text(a["cloud"] == .bool(true) ? "本机和云端" : "仅本机").font(.jSmall).foregroundStyle(Color.inkSoft)
                                .padding(.horizontal, 10).padding(.vertical, 6).background(Capsule().strokeBorder(Color.hairline))
                        }
                        .menuStyle(.button).buttonStyle(.plain).fixedSize()
                    }
                    .padding(.horizontal, 16).padding(.vertical, 11)
                }
                if accounts.isEmpty { InfoRow(title: page == nil ? "正在读取…" : "还没有连接邮箱", detail: page == nil ? nil : "在下面选一个邮箱连接") { EmptyView() } }
            }
            LazyVGrid(columns: [GridItem(.adaptive(minimum: 104), spacing: 8)], spacing: 8) {
                ForEach(Array(presets.enumerated()), id: \.offset) { _, p in
                    Button { Task { await connect(p) } } label: {
                        VStack(alignment: .leading, spacing: 2) {
                            ServiceMark(kind: p["kind"].string, size: 40).padding(.bottom, 8)
                            Text(p["name"].string).font(.jCallout).foregroundStyle(Color.ink).lineLimit(1).minimumScaleFactor(0.8)
                            Text(p["auth"].string == "oauth" ? (p["kind"].string == "gmail" ? "用 Google 登录" : "用 Microsoft 登录") : p["secret"].string)
                                .font(.jSmall).foregroundStyle(Color.inkFaint).lineLimit(1).minimumScaleFactor(0.8)
                        }
                        .frame(maxWidth: .infinity, alignment: .leading).padding(12)
                        .surface()
                    }
                    .buttonStyle(.plain)
                }
            }
        }
        .task { await load() }
        .sheet(item: Binding(get: { adding.map(Sheet.init) }, set: { adding = $0?.value })) { item in AddMailSheet(preset: item.value) { await load() } }
        .sheet(item: Binding(get: { settingUp.map(Sheet.init) }, set: { settingUp = $0?.value })) { item in
            OAuthAppSheet(app: item.value["app"], title: item.value["kind"].string == "gmail" ? "Google 开发者应用" : "Microsoft 开发者应用") { await load() }
        }
    }

    struct Sheet: Identifiable { var value: JSONValue; var id: String { value["kind"].string } }

    func load() async { if let next: JSONValue = try? await store.requireAPI().request("/api/mail") { page = next } }

    func connect(_ p: JSONValue) async {
        guard p["auth"].string == "oauth" else { adding = p; return }
        guard p["app"]["configured"] == .bool(true) else { settingUp = p; return }
        do {
            let reply: JSONValue = try await store.requireAPI().request("/api/mail/connect/\(p["kind"].string)", method: "POST", body: [String: String]())
            if let url = URL(string: reply["url"].string) { openURL(url) }
            for _ in 0..<60 where !Task.isCancelled {  // the account appears once the browser comes back to the Mac
                try? await Task.sleep(for: .seconds(3))
                let before = accounts.count
                await load()
                if accounts.count > before { break }
            }
        } catch { store.report(error) }
    }

    func share(_ a: JSONValue, cloud: Bool) async {
        do {
            let _: JSONValue = try await store.requireAPI().request("/api/mail/\(a["id"].string)", method: "PATCH", body: ["cloud": cloud])
            await load()
        } catch { store.report(error) }
    }

    func remove(_ id: String) async {
        do { let _: JSONValue = try await store.requireAPI().request("/api/mail/\(id)", method: "DELETE"); await load() } catch { store.report(error) }
    }
}

/// iCloud, QQ, 163 and other IMAP mail: how to make the app-specific password or authorization code, then the address and that code.
struct AddMailSheet: View {
    @EnvironmentObject var store: AppStore
    @Environment(\.dismiss) private var dismiss
    @Environment(\.openURL) private var openURL
    var preset: JSONValue
    var saved: () async -> Void
    @State private var address = ""
    @State private var secret = ""
    @State private var host = ""
    @State private var busy = false

    var kind: String { preset["kind"].string }

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 14) {
                HStack(spacing: 12) {
                    ServiceMark(kind: kind, size: 40)
                    Text("连接 \(preset["name"].string)").font(.jTitle)
                }
                Text(preset["hint"].string).font(.jSmall).foregroundStyle(Color.inkSoft).fixedSize(horizontal: false, vertical: true)
                if let link = URL(string: preset["link"].string), !preset["link"].string.isEmpty {
                    Button("打开 \(preset["name"].string) 的设置页") { openURL(link) }.buttonStyle(.jurvis(.ghost, .small))
                }
                TextField("邮箱地址", text: $address).textFieldStyle(.roundedBorder).autocorrectionDisabled()
                    #if os(iOS)
                    .textInputAutocapitalization(.never).keyboardType(.emailAddress)
                    #endif
                SecureField(preset["secret"].string, text: $secret).textFieldStyle(.roundedBorder)
                if kind == "imap" { TextField("IMAP 服务器，例如 imap.example.com", text: $host).textFieldStyle(.roundedBorder).autocorrectionDisabled() }
                Text("\(preset["secret"].string)只保存在 Mac 上，只用来读取收件箱；不会发信，也不会把邮件标为已读。").font(.jSmall).foregroundStyle(Color.inkFaint)
                HStack {
                    Spacer()
                    Button("取消") { dismiss() }.buttonStyle(.jurvis(.ghost))
                    Button(busy ? "正在连接" : "连接") { Task { await save() } }.buttonStyle(.jurvis(.primary)).disabled(busy || address.isEmpty || secret.isEmpty)
                }
            }
            .padding(24)
        }
        .frame(minWidth: 400)
    }

    func save() async {
        busy = true
        defer { busy = false }
        let id = String(address.split(separator: "@").first ?? "mail").lowercased().filter { $0.isLetter && $0.isASCII || $0.isNumber || $0 == "-" || $0 == "_" }.prefix(24)
        do {
            let _: JSONValue = try await store.requireAPI().request("/api/mail/\(id.isEmpty ? "mail" : String(id))", method: "PUT",
                body: ["kind": kind, "address": address.trimmingCharacters(in: .whitespaces), "password": secret, "host": host])
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

/// 电话: the telephone provider account Jurvis places calls through (the user's own: their number, their bill), and the calls it made.
struct PhoneSettings: View {
    @EnvironmentObject var store: AppStore
    @State private var page: JSONValue?
    @State private var binding: BindTarget?
    @State private var showing: ShownCall?

    struct BindTarget: Identifiable { var spec: JSONValue; var current: JSONValue?; var id: String { spec["kind"].string } }
    struct ShownCall: Identifiable { var id: String }
    static let live = ["dialing": "正在拨号", "ringing": "对方响铃中", "talking": "通话中"]

    var bound: JSONValue? { page.flatMap { $0["binding"].isNull ? nil : $0["binding"] } }
    var catalog: [JSONValue] { page?["catalog"].array ?? [] }
    var calls: [JSONValue] { Array((page?["calls"].array ?? []).prefix(8)) }
    var blockers: [String] {
        guard let page, bound != nil else { return [] }
        var out: [String] = []
        if page["public_url"].string.isEmpty { out.append(page["problem"].string.isEmpty ? "还没有公网地址，电话服务商连不到这台 Mac。" : page["problem"].string) }
        if page["speech"]["stt"] != .bool(true) || page["speech"]["tts"] != .bool(true) { out.append("打电话要用本机的语音识别和语音合成模型，请先在“语音”里装好。") }
        return out
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            RowGroup(title: "电话", footnote: "Jurvis 可以替你给餐厅等商家打电话订位、询问。用的是你自己的电话服务商账户：对方看到的是你的号码，话费记在你的账上。每一通都会先问你。") {
                if let bound {
                    HStack(spacing: 12) {
                        ServiceMark(kind: bound["kind"].string, size: 36)
                        VStack(alignment: .leading, spacing: 2) {
                            Text(bound["caller"].string).font(.jBody).lineLimit(1)
                            Text("\(bound["title"].string) · 主叫号码" + (bound["country"].string.isEmpty ? "" : " · 默认区号 \(bound["country"].string)"))
                                .font(.jSmall).foregroundStyle(Color.inkFaint)
                        }
                        Spacer(minLength: 8)
                        Menu {
                            Button("修改") { if let spec = catalog.first(where: { $0["kind"] == bound["kind"] }) { binding = BindTarget(spec: spec, current: bound) } }
                            Button("解绑", role: .destructive) { Task { await unbind() } }
                        } label: {
                            Text("管理").font(.jSmall).foregroundStyle(Color.inkSoft)
                                .padding(.horizontal, 10).padding(.vertical, 6).background(Capsule().strokeBorder(Color.hairline))
                        }
                        .menuStyle(.button).buttonStyle(.plain).fixedSize()
                    }
                    .padding(.horizontal, 16).padding(.vertical, 11)
                    ForEach(blockers, id: \.self) { text in
                        Text(text).font(.jSmall).foregroundStyle(Color.danger).fixedSize(horizontal: false, vertical: true)
                            .frame(maxWidth: .infinity, alignment: .leading).padding(.horizontal, 16).padding(.vertical, 9)
                    }
                    if blockers.isEmpty {
                        InfoRow(title: "已经可以用了", detail: "在对话里说“帮我给某某餐厅打电话订周六晚上 7 点 4 位”试试") { EmptyView() }
                    }
                } else {
                    InfoRow(title: page == nil ? "正在读取…" : "还没有绑定电话服务商", detail: page == nil ? nil : "在下面选一个绑定") { EmptyView() }
                }
            }
            if page != nil, bound == nil {
                LazyVGrid(columns: [GridItem(.adaptive(minimum: 150), spacing: 8)], spacing: 8) {
                    ForEach(Array(catalog.enumerated()), id: \.offset) { _, c in
                        Button { binding = BindTarget(spec: c, current: nil) } label: {
                            VStack(alignment: .leading, spacing: 2) {
                                ServiceMark(kind: c["kind"].string, size: 40).padding(.bottom, 8)
                                Text(c["title"].string).font(.jCallout).foregroundStyle(Color.ink).lineLimit(1)
                                Text(c["description"].string).font(.jSmall).foregroundStyle(Color.inkFaint).lineLimit(2).fixedSize(horizontal: false, vertical: true)
                            }
                            .frame(maxWidth: .infinity, alignment: .leading).padding(12)
                            .surface()
                        }
                        .buttonStyle(.plain)
                    }
                }
            }
            if !calls.isEmpty {
                RowGroup(title: "最近的通话") {
                    ForEach(Array(calls.enumerated()), id: \.offset) { _, c in
                        Button { showing = ShownCall(id: c["id"].string) } label: {
                            HStack(spacing: 12) {
                                VStack(alignment: .leading, spacing: 2) {
                                    Text(c["place"].string.isEmpty ? c["number"].string : c["place"].string).font(.jBody).foregroundStyle(Color.ink).lineLimit(1)
                                    Text(c["status"].string == "ended" ? (c["summary"].string.isEmpty ? c["ended"].string : c["summary"].string) : c["purpose"].string)
                                        .font(.jSmall).foregroundStyle(Color.inkFaint).lineLimit(1)
                                }
                                Spacer(minLength: 8)
                                Text(c["status"].string == "ended" ? (c["outcome_label"].string.isEmpty ? "已结束" : c["outcome_label"].string) : (Self.live[c["status"].string] ?? ""))
                                    .font(.jSmall).foregroundStyle(c["status"].string == "ended" ? Color.inkSoft : Color.danger)
                                    .padding(.horizontal, 10).padding(.vertical, 4).background(Capsule().strokeBorder(Color.hairline))
                            }
                            .padding(.horizontal, 16).padding(.vertical, 11).contentShape(Rectangle())
                        }
                        .buttonStyle(.plain)
                    }
                }
            }
        }
        .task { await load() }
        .onChange(of: store.snapshot?.last_event) { _, _ in Task { await load() } }
        .sheet(item: $binding) { target in BindPhoneSheet(spec: target.spec, current: target.current) { await load() } }
        .sheet(item: $showing) { call in PhoneCallSheet(id: call.id) { await load() } }
    }

    func load() async { if let next: JSONValue = try? await store.requireAPI().request("/api/phone") { page = next } }

    func unbind() async {
        do { let _: JSONValue = try await store.requireAPI().request("/api/phone", method: "DELETE"); await load() } catch { store.report(error) }
    }
}

/// The provider's own instructions, then the few values it needs. Credentials are checked with the provider before they are saved.
struct BindPhoneSheet: View {
    @EnvironmentObject var store: AppStore
    @Environment(\.dismiss) private var dismiss
    @Environment(\.openURL) private var openURL
    var spec: JSONValue
    var current: JSONValue?
    var saved: () async -> Void
    @State private var values: [String: String] = [:]
    @State private var country = ""
    @State private var busy = false

    var fields: [JSONValue] { spec["fields"].array }
    var ready: Bool {
        fields.allSatisfy { f in
            f["optional"] == .bool(true) || !(values[f["name"].string] ?? "").isEmpty || (current != nil && f["secret"] == .bool(true))
        }
    }

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 14) {
                HStack(spacing: 12) {
                    ServiceMark(kind: spec["kind"].string, size: 40)
                    Text("\(current == nil ? "绑定" : "修改") \(spec["title"].string)").font(.jTitle)
                }
                Text(spec["setup"].string).font(.jSmall).foregroundStyle(Color.inkSoft).fixedSize(horizontal: false, vertical: true)
                if let link = URL(string: spec["console"].string) {
                    Button("打开 \(spec["title"].string) 后台") { openURL(link) }.buttonStyle(.jurvis(.ghost, .small))
                }
                ForEach(Array(fields.enumerated()), id: \.offset) { _, f in
                    let name = f["name"].string
                    let label = f["label"].string + (f["optional"] == .bool(true) ? "（可选）" : "")
                    let text = Binding(get: { values[name] ?? "" }, set: { values[name] = $0 })
                    if f["secret"] == .bool(true) {
                        SecureField(current == nil ? label : label + "（已保存，留空则不改）", text: text).textFieldStyle(.roundedBorder)
                    } else {
                        TextField(label, text: text).textFieldStyle(.roundedBorder).autocorrectionDisabled()
                            #if os(iOS)
                            .textInputAutocapitalization(.never)
                            #endif
                    }
                }
                TextField("默认国家区号（可选），例如 +86", text: $country).textFieldStyle(.roundedBorder).autocorrectionDisabled()
                    #if os(iOS)
                    .keyboardType(.phonePad)
                    #endif
                Text("没写区号的号码会按这个区号拨打。凭据只保存在 Mac 上，绑定时会向服务商核对一次。").font(.jSmall).foregroundStyle(Color.inkFaint)
                HStack {
                    Spacer()
                    Button("取消") { dismiss() }.buttonStyle(.jurvis(.ghost))
                    Button(busy ? "正在核对" : (current == nil ? "绑定" : "保存")) { Task { await save() } }.buttonStyle(.jurvis(.primary)).disabled(busy || !ready)
                }
            }
            .padding(24)
        }
        .frame(minWidth: 420)
        .onAppear {
            guard let current else { return }
            country = current["country"].string
            for f in fields where f["secret"] != .bool(true) { values[f["name"].string] = current["settings"][f["name"].string].string }
        }
    }

    func save() async {
        busy = true
        defer { busy = false }
        do {
            let settings = values.mapValues { $0.trimmingCharacters(in: .whitespaces) }
            let _: JSONValue = try await store.requireAPI().request("/api/phone", method: "PUT",
                body: ["kind": spec["kind"].string, "settings": settings, "country": country.trimmingCharacters(in: .whitespaces)])
            await saved()
            dismiss()
        } catch { store.report(error) }
    }
}

/// One call: what it was for, what came of it, and what was said. A call still going on can be ended here.
struct PhoneCallSheet: View {
    @EnvironmentObject var store: AppStore
    @Environment(\.dismiss) private var dismiss
    var id: String
    var changed: () async -> Void
    @State private var call: JSONValue?

    var live: Bool { call.map { $0["status"].string != "ended" } ?? false }

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 14) {
                if let call {
                    Text(call["place"].string.isEmpty ? call["number"].string : call["place"].string).font(.jTitle)
                    Text([call["number"].string, call["created_at"].number?.dayAndTime ?? "",
                          live ? (PhoneSettings.live[call["status"].string] ?? "") : call["ended"].string].filter { !$0.isEmpty }.joined(separator: " · "))
                        .font(.jSmall).foregroundStyle(Color.inkFaint)
                    Text("要办的事：" + call["purpose"].string).font(.jSmall).foregroundStyle(Color.inkSoft).fixedSize(horizontal: false, vertical: true)
                    if !call["summary"].string.isEmpty {
                        Text((call["outcome_label"].string.isEmpty ? "结果" : call["outcome_label"].string) + "：" + call["summary"].string)
                            .font(.jBody).fixedSize(horizontal: false, vertical: true)
                    }
                    if !call["booking"].isNull {
                        let booking = call["booking"]
                        Label(booking["start"].string.replacingOccurrences(of: "T", with: " ") + (booking["timezone"].string.isEmpty ? "" : "（\(booking["timezone"].string)）")
                              + (booking["status"].string == "added" ? " · 已加入日历" : " · 等待加入日历"), systemImage: "calendar")
                            .font(.jSmall).foregroundStyle(Color.inkSoft)
                    }
                    let lines = call["transcript"].array
                    if lines.isEmpty {
                        Text(live ? "还没有人说话。" : "没有通话内容。").font(.jSmall).foregroundStyle(Color.inkFaint)
                    } else {
                        VStack(alignment: .leading, spacing: 8) {
                            ForEach(Array(lines.enumerated()), id: \.offset) { _, line in
                                HStack(alignment: .firstTextBaseline, spacing: 8) {
                                    Text(line["who"].string == "them" ? "对方" : "Jurvis").font(.jSmall).foregroundStyle(Color.inkFaint).frame(width: 44, alignment: .leading)
                                    Text(line["text"].string).font(.jCallout).fixedSize(horizontal: false, vertical: true).textSelection(.enabled)
                                }
                            }
                        }
                        .frame(maxWidth: .infinity, alignment: .leading).padding(12).surface()
                    }
                } else {
                    Text("正在读取…").font(.jSmall).foregroundStyle(Color.inkFaint)
                }
                HStack {
                    Spacer()
                    if live { Button("挂断") { Task { await hangup() } }.buttonStyle(.jurvis(.ghost)).foregroundStyle(Color.danger) }
                    Button("关闭") { dismiss() }.buttonStyle(.jurvis(.primary))
                }
            }
            .padding(24)
        }
        .frame(minWidth: 420, minHeight: 320)
        .task {
            while !Task.isCancelled {   // a live call's transcript grows as it is spoken
                if let next: JSONValue = try? await store.requireAPI().request("/api/phone/calls/\(id)") { call = next }
                if !live { break }
                try? await Task.sleep(for: .seconds(2))
            }
        }
    }

    func hangup() async {
        do {
            let _: JSONValue = try await store.requireAPI().request("/api/phone/calls/\(id)/hangup", method: "POST", body: [String: String]())
            await changed()
        } catch { store.report(error) }
    }
}
