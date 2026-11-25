import SwiftUI
import UniformTypeIdentifiers

// 目标 and 账本: what Jurvis keeps doing for the user over time, and what the user spent.
// Both are set up in conversation; these pages list them and let the user remove or correct entries.

struct Goal: Codable, Identifiable, Equatable { var id: String; var title: String; var detail: String; var created_at: Double; var page: String? }
/// A goal opened as its space: what Jurvis knows about it, how often it looks at it, and where it was talked about.
struct Space: Codable {
    struct Fact: Codable, Identifiable { var id: String; var content: String }
    struct Stretch: Codable, Identifiable { var id: String; var title: String; var updated_at: Double; var text: String }
    var goal: Goal; var file: String?; var facts: [Fact]; var schedules: [Schedule]; var stretches: [Stretch]
}
struct Schedule: Codable, Identifiable, Equatable {
    var id: String; var title: String; var brief: String; var when: String; var next_run: Double; var last_run: Double?
}
/// Work that starts when something happens: an email arrives, an event is about to begin, a budget is used up, a device changes.
struct Watch: Codable, Identifiable, Equatable {
    var id: String; var title: String; var brief: String; var when: String; var last_fired: Double?; var fired: Int
}
struct Agenda: Codable, Equatable { var goals: [Goal]; var schedules: [Schedule]; var watches: [Watch]? }

struct LedgerEntry: Codable, Identifiable, Equatable {
    var id: String; var at: Double; var amount_cents: Int; var direction: String; var category: String; var merchant: String; var note: String
    var income: Bool { direction == "income" }
}
struct LedgerPage: Codable, Equatable { var summary: JSONValue; var transactions: [LedgerEntry] }
struct BillImport: Codable { var added: Int; var already_there: Int; var source: String; var uncategorized: Int }

extension Double {
    /// A date with its weekday and time: a schedule's next run is usually days away, so the time alone says too little.
    var dayAndTime: String {
        let formatter = DateFormatter()
        formatter.locale = Locale(identifier: "zh_CN")
        formatter.dateFormat = "M月d日 EEE HH:mm"
        return formatter.string(from: Date(timeIntervalSince1970: self))
    }
}

func yuan(_ cents: Int) -> String {
    let formatter = NumberFormatter()
    formatter.numberStyle = .decimal
    formatter.minimumFractionDigits = cents % 100 == 0 ? 0 : 2
    formatter.maximumFractionDigits = 2
    return "¥" + (formatter.string(from: NSNumber(value: Double(abs(cents)) / 100)) ?? "\(abs(cents) / 100)")
}

struct GoalsView: View {
    @EnvironmentObject var store: AppStore
    @State private var agenda: Agenda?
    @State private var opened: Goal?

    var body: some View {
        Page(title: "目标", subtitle: "告诉 Jurvis 你想长期跟踪的事，或者让它按周、按月帮你做一件事，它们会出现在这里。", width: 860) { EmptyView() } content: {
            if let agenda {
                Eyebrow(text: "长期目标")
                if agenda.goals.isEmpty { EmptyState(icon: "scope", title: "还没有长期目标", detail: "比如「我想坚持每周跑步三次」。") }
                ForEach(agenda.goals) { goal in
                    Card {
                        Label(goal.title, systemImage: "scope").font(.jHeading).lineLimit(2)
                        if !goal.detail.isEmpty { Text(goal.detail).font(.jCallout).foregroundStyle(Color.inkSoft).textSelection(.enabled) }
                        HStack {
                            Text("记于 " + goal.created_at.dayAndTime).font(.jSmall).foregroundStyle(Color.inkFaint)
                            Spacer()
                            Button { opened = goal } label: { Label("打开", systemImage: "rectangle.portrait.on.rectangle.portrait") }.buttonStyle(.jurvis(.secondary, .small))
                            ActionButton(title: "删除", busyTitle: "正在删除", icon: "trash", kind: .danger, scale: .small) {
                                let _: JSONValue = try await store.requireAPI().request("/api/goals/\(goal.id)", method: "DELETE")
                                await load()
                            }
                        }
                    }
                }
                Eyebrow(text: "定时任务").padding(.top, 8)
                if agenda.schedules.isEmpty { EmptyState(icon: "calendar.badge.clock", title: "还没有定时任务", detail: "比如「每周一早上给我一份质押项目简报」或「明早 8 点提醒我交房租」。") }
                ForEach(agenda.schedules) { item in
                    Card {
                        HStack(spacing: 8) {
                            Label(item.title, systemImage: "calendar.badge.clock").font(.jHeading).lineLimit(2)
                            Badge(text: item.when)
                        }
                        Text(item.brief).font(.jCallout).foregroundStyle(Color.inkSoft).lineLimit(4).textSelection(.enabled)
                        HStack {
                            Text("下次 " + item.next_run.dayAndTime + (item.last_run.map { " · 上次 " + $0.dayAndTime } ?? "")).font(.jSmall).foregroundStyle(Color.inkFaint)
                            Spacer()
                            ActionButton(title: "取消", busyTitle: "正在取消", icon: "xmark", kind: .danger, scale: .small) {
                                let _: JSONValue = try await store.requireAPI().request("/api/schedules/\(item.id)", method: "DELETE")
                                await load()
                            }
                        }
                    }
                }
                Eyebrow(text: "事件触发").padding(.top, 8)
                if (agenda.watches ?? []).isEmpty { EmptyState(icon: "bolt", title: "还没有事件触发", detail: "比如「房东来邮件时告诉我他要什么」或「开会前半小时把相关的邮件整理给我」。") }
                ForEach(agenda.watches ?? []) { item in
                    Card {
                        HStack(spacing: 8) {
                            Label(item.title, systemImage: "bolt").font(.jHeading).lineLimit(2)
                            Badge(text: item.when)
                        }
                        Text(item.brief).font(.jCallout).foregroundStyle(Color.inkSoft).lineLimit(4).textSelection(.enabled)
                        HStack {
                            Text(item.last_fired.map { "触发过 \(item.fired) 次 · 上次 " + $0.dayAndTime } ?? "还没有触发过").font(.jSmall).foregroundStyle(Color.inkFaint)
                            Spacer()
                            ActionButton(title: "取消", busyTitle: "正在取消", icon: "xmark", kind: .danger, scale: .small) {
                                let _: JSONValue = try await store.requireAPI().request("/api/watches/\(item.id)", method: "DELETE")
                                await load()
                            }
                        }
                    }
                }
            } else {
                LoadingState(text: "正在载入目标")
            }
        }
        .task { while !Task.isCancelled { await load(); try? await Task.sleep(for: .seconds(5)) } }
        .sheet(item: $opened) { SpaceView(goal: $0).environmentObject(store) }
    }

    func load() async {
        guard let next: Agenda = try? await store.requireAPI().request("/api/agenda") else { return }
        if next != agenda { agenda = next }
    }
}

struct LedgerView: View {
    @EnvironmentObject var store: AppStore
    @State private var page: LedgerPage?
    @State private var period = "this_month"
    @State private var importing = false
    static let periods = [("this_month", "本月"), ("last_month", "上月"), ("this_year", "今年")]
    static let categories = ["餐饮", "交通", "购物", "日用", "住房", "娱乐", "医疗", "教育", "通讯", "旅行", "订阅", "转账", "其他"]

    var body: some View {
        Page(title: "账本", subtitle: "在对话里说「午饭 35」就能记一笔，也可以导入支付宝或微信支付导出的 CSV 账单。每一笔明细只存在这台 Mac 上。", width: 860) {
            Picker("时间范围", selection: $period) { ForEach(Self.periods, id: \.0) { Text($0.1).tag($0.0) } }
                .pickerStyle(.segmented).labelsHidden().fixedSize()
            Button { importing = true } label: { Label("导入账单", systemImage: "square.and.arrow.down") }.buttonStyle(.jurvis(.primary))
        } content: {
            if let page {
                LedgerSummary(summary: page.summary)
                if page.transactions.isEmpty {
                    EmptyState(icon: "list.bullet.rectangle", title: "这段时间还没有记录", detail: "对 Jurvis 说一句「打车 48」，或者导入一份账单。")
                } else {
                    VStack(spacing: 0) {
                        ForEach(Array(page.transactions.enumerated()), id: \.element.id) { index, entry in
                            if index > 0 { Divider().overlay(Color.hairline) }
                            LedgerRow(entry: entry) { await load() }
                        }
                    }
                    .surface()
                }
            } else {
                LoadingState(text: "正在载入账本")
            }
        }
        .task(id: period) { while !Task.isCancelled { await load(); try? await Task.sleep(for: .seconds(5)) } }
        .fileImporter(isPresented: $importing, allowedContentTypes: [.commaSeparatedText, .plainText]) { result in
            guard case .success(let url) = result else { return }
            Task { await importBill(url) }
        }
    }

    func load() async {
        guard let next: LedgerPage = try? await store.requireAPI().request("/api/ledger?period=\(period)") else { return }
        if next != page { page = next }
    }

    func importBill(_ url: URL) async {
        let scoped = url.startAccessingSecurityScopedResource()
        defer { if scoped { url.stopAccessingSecurityScopedResource() } }
        do {
            let data = try Data(contentsOf: url)
            let _: BillImport = try await store.requireAPI().upload("/api/ledger/import", data: data, name: url.lastPathComponent, mime: "text/csv")
            await load()
        } catch { store.report(error) }
    }
}

/// The period's total, a line of facts, and the share of each category.
struct LedgerSummary: View {
    var summary: JSONValue

    var body: some View {
        let groups = summary["by_category"].array
        Card {
            Text("\(summary["period"].string)支出 · \(summary["from"].string) 至 \(summary["to"].string)").font(.jSmall).foregroundStyle(Color.inkSoft)
            Text(yuan(Int(((summary["spent"].number ?? 0) * 100).rounded()))).font(.system(size: 34, weight: .semibold)).monospacedDigit()
            Text(facts).font(.jSmall).foregroundStyle(Color.inkSoft)
            if !groups.isEmpty {
                ShareBars(items: groups.map { (label: $0["category"].string, value: yuan(Int((($0["spent"].number ?? 0) * 100).rounded())), share: ($0["share_percent"].number ?? 0) / 100) })
                    .padding(.top, 4)
            }
        }
        .frame(maxWidth: 560, alignment: .leading)
    }

    var facts: String {
        var parts = ["\(Int(summary["payments"].number ?? 0)) 笔", "日均 " + yuan(Int(((summary["per_day"].number ?? 0) * 100).rounded()))]
        if let change = summary["change_percent"].number { parts.append("较上期 \(change > 0 ? "+" : "")\(change.formatted(.number.precision(.fractionLength(0...1))))%") }
        if let income = summary["income"].number, income > 0 { parts.append("收入 " + yuan(Int((income * 100).rounded()))) }
        if let used = summary["budget"]["used_percent"].number, let limit = summary["budget"]["limit"].number {
            parts.append("预算 " + yuan(Int((limit * 100).rounded())) + " · 已用 \(used.formatted(.number.precision(.fractionLength(0...1))))%")
        }
        return parts.joined(separator: " · ")
    }
}

/// Shares of a total: label, bar, amount. Also drawn inside the ledger card of a conversation.
struct ShareBars: View {
    var items: [(label: String, value: String, share: Double)]
    var body: some View {
        VStack(spacing: 6) {
            ForEach(Array(items.enumerated()), id: \.offset) { _, item in
                HStack(spacing: 10) {
                    Text(item.label).foregroundStyle(Color.inkSoft).frame(width: 40, alignment: .leading)
                    GeometryReader { box in
                        ZStack(alignment: .leading) {
                            Capsule().fill(Color.bubble)
                            Capsule().fill(Color.ink).frame(width: max(3, box.size.width * min(1, max(0, item.share))))
                        }
                    }
                    .frame(height: 6)
                    Text(item.value).monospacedDigit().frame(minWidth: 64, alignment: .trailing)
                }
                .font(.jCallout)
            }
        }
    }
}

struct LedgerRow: View {
    @EnvironmentObject var store: AppStore
    var entry: LedgerEntry
    var changed: () async -> Void

    var body: some View {
        HStack(spacing: 12) {
            VStack(alignment: .leading, spacing: 2) {
                Text(entry.merchant.isEmpty ? (entry.note.isEmpty ? entry.category : entry.note) : entry.merchant).font(.jHeading).lineLimit(1)
                Text(entry.at.dayAndTime + (entry.merchant.isEmpty || entry.note.isEmpty ? "" : " · " + entry.note)).font(.jSmall).foregroundStyle(Color.inkSoft).lineLimit(1)
            }
            Spacer(minLength: 8)
            Menu(entry.category) {
                ForEach(LedgerView.categories, id: \.self) { category in
                    Button(category) { Task { await move(to: category) } }
                }
            }
            .menuStyle(.borderlessButton).fixedSize().font(.jCallout)
            Text((entry.income ? "+" : "") + yuan(entry.amount_cents)).font(.jHeading).monospacedDigit()
                .foregroundStyle(entry.income ? Color.signal : Color.ink).frame(minWidth: 76, alignment: .trailing)
            Button { Task { await remove() } } label: { Image(systemName: "trash") }
                .buttonStyle(.jurvisIcon(.ghost, .regular)).help("删除").accessibilityLabel("删除这笔记录")
        }
        .padding(.horizontal, 14).padding(.vertical, 8).frame(minHeight: 52)
    }

    func move(to category: String) async {
        do {
            let _: JSONValue = try await store.requireAPI().request("/api/ledger/transactions/\(entry.id)", method: "PATCH", body: ["category": category])
            await changed()
        } catch { store.report(error) }
    }

    func remove() async {
        do {
            let _: JSONValue = try await store.requireAPI().request("/api/ledger/transactions/\(entry.id)", method: "DELETE")
            await changed()
        } catch { store.report(error) }
    }
}

/// A long-term goal as one place: its notes, its rhythm, and the conversations about it.
struct SpaceView: View {
    @EnvironmentObject var store: AppStore
    @Environment(\.dismiss) private var dismiss
    var goal: Goal
    @State private var space: Space?

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 14) {
                HStack(alignment: .firstTextBaseline) {
                    Text(goal.title).font(.jSection).lineLimit(3)
                    Spacer(minLength: 12)
                    Button("完成") { dismiss() }.buttonStyle(.jurvis(.secondary, .small)).keyboardShortcut(.cancelAction)
                }
                if let space {
                    Eyebrow(text: "知道的")
                    if space.facts.isEmpty { Text("还没有记下什么。聊到这件事时，Jurvis 会把了解到的放在这里。").font(.jCallout).foregroundStyle(Color.inkSoft) }
                    ForEach(space.facts) { fact in
                        HStack(alignment: .firstTextBaseline, spacing: 8) {
                            Text("•").foregroundStyle(Color.inkFaint)
                            Text(fact.content).font(.jBody).textSelection(.enabled).frame(maxWidth: .infinity, alignment: .leading)
                        }
                    }
                    Eyebrow(text: "节奏").padding(.top, 6)
                    if space.schedules.isEmpty { Text("没有定时。可以在对话里说「每周给我一份简报」。").font(.jCallout).foregroundStyle(Color.inkSoft) }
                    ForEach(space.schedules) { item in
                        HStack(spacing: 8) {
                            Image(systemName: "calendar.badge.clock").foregroundStyle(Color.inkSoft)
                            Text(item.title).font(.jBody).lineLimit(1)
                            Badge(text: item.when, tone: .neutral)
                            Spacer(minLength: 8)
                            Text("下次 " + item.next_run.dayAndTime).font(.jSmall).foregroundStyle(Color.inkFaint)
                        }
                    }
                    Eyebrow(text: "聊到过").padding(.top, 6)
                    if space.stretches.isEmpty { Text("还没有相关的对话。").font(.jCallout).foregroundStyle(Color.inkSoft) }
                    ForEach(space.stretches) { stretch in
                        Button { dismiss(); Task { await store.open(stretch: stretch.id) } } label: {
                            VStack(alignment: .leading, spacing: 2) {
                                Text(stretch.title).font(.jBody).foregroundStyle(Color.ink).lineLimit(1)
                                Text(StretchMark.label(stretch.updated_at) + (stretch.text.isEmpty ? "" : " · " + stretch.text)).font(.jSmall).foregroundStyle(Color.inkFaint).lineLimit(2)
                            }
                            .frame(maxWidth: .infinity, minHeight: 44, alignment: .leading).contentShape(Rectangle())
                        }
                        .buttonStyle(.plain)
                    }
                    #if os(macOS)
                    if let file = space.file {
                        Button { NSWorkspace.shared.activateFileViewerSelecting([URL(fileURLWithPath: file)]) } label: { Label("在访达中显示笔记文件", systemImage: "folder") }
                            .buttonStyle(.jurvis(.secondary, .small)).padding(.top, 8)
                    }
                    #endif
                } else {
                    LoadingState(text: "正在打开")
                }
            }
            .padding(24).frame(maxWidth: .infinity, alignment: .leading)
        }
        .background(Color.canvas)
        #if os(macOS)
        .frame(minWidth: 520, idealWidth: 620, minHeight: 420, idealHeight: 620)
        #endif
        .task { space = try? await store.requireAPI().request("/api/spaces/\(goal.id)") }
    }
}
