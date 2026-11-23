// UI smoke test: compiles with the app sources (-D UI_SMOKE), connects to the running
// engine, renders every screen offscreen and writes PNG snapshots. A crash in any view
// (for example in AVKit) fails the run. Built and run by scripts/ui-smoke.sh.
import AppKit
import SwiftUI

let output = URL(fileURLWithPath: CommandLine.arguments.count > 1 ? CommandLine.arguments[1] : "build/ui-smoke")

@MainActor func capture<V: View>(_ name: String, size: CGSize = CGSize(width: 1280, height: 820), dark: Bool = false, wait: Double = 2.5, _ view: V) async {
    let window = NSWindow(contentRect: CGRect(origin: .zero, size: size), styleMask: [.titled, .resizable, .closable, .miniaturizable, .fullSizeContentView], backing: .buffered, defer: false)
    window.appearance = NSAppearance(named: dark ? .darkAqua : .aqua)
    let host = NSHostingView(rootView: view)
    window.contentView = host
    window.setFrameOrigin(CGPoint(x: -10000, y: -10000))
    window.orderBack(nil)
    try? await Task.sleep(for: .seconds(wait))
    host.layoutSubtreeIfNeeded()
    guard let rep = host.bitmapImageRepForCachingDisplay(in: host.bounds) else { print("FAIL \(name): no bitmap"); return }
    host.cacheDisplay(in: host.bounds, to: rep)
    try? rep.representation(using: .png, properties: [:])?.write(to: output.appendingPathComponent(name + ".png"))
    window.orderOut(nil)
    print("ok   \(name)")
}

extension AppStore {
    func reviewCount(_ project: Project) -> Int {
        project.document.shots.filter { $0.first_frame != nil }.count + project.document.characters.filter { $0.reference_asset != nil }.count
    }
}

@MainActor func run() async {
    let store = AppStore()
    await store.start()
    guard store.connected, let snapshot = store.snapshot else { print("FAIL engine not reachable: \(store.error ?? "")"); exit(1) }
    await store.reloadSettings()
    let root = RootView().environmentObject(store).environmentObject(store.companion).tint(Color.ink)

    for section in Section.allCases {
        store.section = section
        if section == .assistant { await store.newChat() }
        await capture("section-\(section.id)", root)
    }
    if let session = snapshot.sessions.first {
        await store.selectSession(session.id)
        await capture("chat-session", root)
        await capture("chat-session-dark", dark: true, root)
    }
    store.section = .tasks
    await capture("tasks-dark", dark: true, root)

    // The media viewer: the screen that used to crash on video.
    for asset in snapshot.assets {
        let kind = asset.isVideo ? "video" : "image"
        await capture("media-\(kind)-\(asset.kind)-\(asset.id.prefix(6))", size: CGSize(width: 900, height: 760), wait: 3,
                       MediaViewer(viewing: MediaViewing(assets: [asset], index: 0)).environmentObject(store))
    }
    // The drama with the most artwork, in both appearances and at a narrow width.
    if let project = snapshot.projects.max(by: { store.reviewCount($0) < store.reviewCount($1) }) {
        let page = NavigationStack { ProjectDetailView(projectID: project.id) }.environmentObject(store)
        await capture("drama-detail", size: CGSize(width: 1100, height: 1500), page)
        await capture("drama-detail-dark", size: CGSize(width: 1100, height: 1500), dark: true, page)
        await capture("drama-detail-narrow", size: CGSize(width: 420, height: 1200), page)
    }
    if let code = snapshot.jobs.first(where: { $0.kind == "code" && $0.status == "succeeded" && ($0.result["change_count"].number ?? 0) > 0 }) {
        await capture("code-change", size: CGSize(width: 820, height: 640), ScrollView { ChangeSetCard(jobID: code.id).padding(20) }.environmentObject(store))
    }
    // A conversation bound to a code project, and the home screen with a project picked.
    if let session = snapshot.sessions.first(where: { $0.code_project_id != nil }) {
        await store.selectSession(session.id)
        await capture("chat-code-session", root)
    }
    // Commands waiting for approval and finished, inside a goal.
    let sample = #"""
    [{"id":"m1","session_id":"s","role":"assistant","content":"","status":"streaming","created_at":0,"metadata":{"goal":{"status":"running","steps":3,"reviews":0},"steps":[
      {"text":"先把插件克隆下来。","calls":[{"id":"a","name":"run_command","label":"运行命令","status":"ok","summary":"完成 · 1.2 秒","command":"git clone https://github.com/example/demo-nodes.git","reason":"克隆插件仓库","cwd":"/Users/me/jurvis/.runtime/ComfyUI/custom_nodes","console":"Cloning into 'demo-nodes'...\ndone.","exit_code":0}]},
      {"text":"","calls":[{"id":"b","name":"run_command","label":"运行命令","status":"awaiting","command":"/Users/me/jurvis/.venv/bin/python -m pip install -r demo-nodes/requirements.txt","reason":"安装插件依赖","cwd":"/Users/me/jurvis/.runtime/ComfyUI/custom_nodes"}]}]}},
     {"id":"m2","session_id":"s","role":"assistant","content":"测试已全部通过。","status":"complete","created_at":1,"metadata":{"goal":{"status":"done","steps":6,"reviews":1},"steps":[
      {"text":"","calls":[{"id":"c","name":"code_read","label":"阅读源码","status":"ok","summary":"calc.py · 6 行"},{"id":"d","name":"run_command","label":"运行命令","status":"error","summary":"失败 · 退出码 1","command":"python3 -m unittest discover -s tests","reason":"运行测试","cwd":"/tmp/goal-code","console":"F..\nFAIL: test_total\nAssertionError: 6 != 5","exit_code":1}]},
      {"text":"测试已全部通过。","calls":[]}]}}]
    """#
    if let messages = try? JSONDecoder().decode([Message].self, from: Data(sample.utf8)) {
        await capture("goal-commands", size: CGSize(width: 820, height: 900), ScrollView {
            VStack(alignment: .leading, spacing: 30) { ForEach(messages) { AssistantMessage(message: $0, open: { _ in }) } }.padding(24)
        }.background(Color.canvas).environmentObject(store))
    } else { print("FAIL goal sample did not decode") }
    // Assistant replies: bubbles, a question with options, and every kind of card.
    let assistant = #"""
    [{"id":"a1","session_id":"s","role":"assistant","content":"","status":"complete","created_at":0,"metadata":{"steps":[
      {"text":"记下了，存到 Goals 里了。上海这几天转凉，出门加件外套。","calls":[
        {"id":"k1","name":"weather","label":"查天气","status":"ok","card":{"kind":"weather","title":"上海","subtitle":"上海市 · 中国","value":"11°","icon":"partly","rows":[{"label":"天气","value":"晴间多云"},{"label":"体感","value":"7°"},{"label":"湿度","value":"40%"},{"label":"风","value":"12 km/h"}],"days":[{"label":"今天","icon":"partly","high":12,"low":4},{"label":"明天","icon":"sun","high":13,"low":4},{"label":"周日","icon":"rain","high":15,"low":6},{"label":"周一","icon":"snow","high":8,"low":1}],"note":"Open-Meteo · 2026-11-20 14:00","outfit":{"wear":["风衣或夹克","卫衣","长裤","雨伞"],"tip":"有雨，带伞，鞋子选不怕湿的"}}},
        {"id":"k2","name":"crypto_price","label":"查币价","status":"ok","card":{"kind":"price","title":"Bitcoin","subtitle":"BTC · USD","value":"$61,874","icon":"coin","trend":"down","rows":[{"label":"24 小时","value":"-2.37%"},{"label":"7 天","value":"-9.78%"},{"label":"市值","value":"$1,244.6B"}],"series":[68,67.5,67,66,66.4,65,64.2,64.8,63.9,64,63.1,62.8,63.3,62.4,62,61.9],"note":"CoinGecko · 2026-11-20 06:05 UTC"}},
        {"id":"k3","name":"convert_currency","label":"换算汇率","status":"ok","card":{"kind":"currency","title":"100 USD","subtitle":"USD → CNY","value":"666.67 CNY","icon":"exchange","rows":[{"label":"汇率","value":"1 USD = 6.6667 CNY"}],"note":"欧洲央行参考汇率 · 2026-11-19"}}]}]}},
     {"id":"a1b","session_id":"s","role":"assistant","content":"","status":"complete","created_at":0,"metadata":{"steps":[
      {"text":"","calls":[
        {"id":"k4","name":"world_time","label":"查当地时间","status":"ok","card":{"kind":"clock","title":"纽约","subtitle":"纽约州 · 美国","value":"01:06","icon":"moon","rows":[{"label":"日期","value":"11月20日 周五"},{"label":"时差","value":"比你晚 13 小时"}],"note":"你这里现在 14:06"}},
        {"id":"k5","name":"countdown","label":"倒计时","status":"ok","card":{"kind":"countdown","title":"春节","subtitle":"2027-02-06 周六","value":"78","icon":"calendar","rows":[{"label":"","value":"还有 78 天"},{"label":"约","value":"11 周 1 天"}],"note":"从今天 11月20日 算起"}},
        {"id":"k6","name":"spending_summary","label":"看支出","status":"ok","card":{"kind":"ledger","title":"11 月支出","subtitle":"11/01 – 11/30","value":"¥3,280.50","icon":"wallet","bars":[{"label":"餐饮","value":"¥1,420","share":0.43},{"label":"购物","value":"¥980","share":0.30},{"label":"交通","value":"¥560.50","share":0.17},{"label":"订阅","value":"¥320","share":0.10}],"rows":[{"label":"笔数","value":"64 笔"},{"label":"日均","value":"¥164"},{"label":"较上期","value":"+12.4%"}],"note":"每一笔明细在「账本」里，只存在这台 Mac 上"}},
        {"id":"k7","name":"log_expense","label":"记一笔","status":"ok","card":{"kind":"expense","title":"午饭","subtitle":"已记一笔 · 餐饮","value":"¥35","icon":"wallet","rows":[{"label":"本月餐饮","value":"¥1,455"},{"label":"本月合计","value":"¥3,315.50"}],"note":"11月20日 12:10"}}]}]}},
     {"id":"a1c","session_id":"s","role":"assistant","content":"","status":"complete","created_at":0,"metadata":{"steps":[
      {"text":"这些是我存下和在做的：","calls":[
        {"id":"m1","name":"track_goal","label":"保存长期目标","status":"ok","card":{"kind":"goal","title":"L2 长期跟踪","subtitle":"长期目标"}},
        {"id":"m2","name":"schedule_task","label":"设置定时任务","status":"ok","card":{"kind":"schedule","title":"L2 每月深度复盘","subtitle":"定时任务 · 每月 1 号 09:00"}},
        {"id":"m3","name":"remember","label":"保存记忆","status":"ok","card":{"kind":"memory","title":"过敏","subtitle":"已记住","note":"用户对花生过敏"}},
        {"id":"m4","name":"save_file","label":"保存文件","status":"ok","card":{"kind":"file","title":"il.md","subtitle":"文档 · 8557 字","note":"# 集中流动性 IL 对照表 同一笔钱，区间选多窄，价格动多少，就亏多少。"}},
        {"id":"m5","name":"start_task","label":"开始后台任务","status":"ok","card":{"kind":"task","title":"供应商报价对比","subtitle":"后台任务 · 做完发在这里"}},
        {"id":"m6","name":"ask_user","label":"向你提问","status":"ok"}]},
      {"text":"你想按什么节奏跟？","calls":[]}],
      "ask":{"question":"你想按什么节奏跟？","options":["每周一次","每月一次","先不用，需要时再问"]}}},
     {"id":"a2","session_id":"s","role":"assistant","content":"","status":"streaming","created_at":1,"metadata":{"task":{"title":"IL 对照表"},"goal":{"status":"running","steps":2,"reviews":0},"steps":[
      {"text":"先把公式列出来。","calls":[{"id":"t1","name":"calculate","label":"计算","status":"ok","summary":"= 2"}]}]}}]
    """#
    if let messages = try? JSONDecoder().decode([Message].self, from: Data(assistant.utf8)) {
        // One message per screenful: a window cannot be taller than the screen.
        for (index, message) in messages.enumerated() {
            let column = ScrollView {
                AssistantMessage(message: message, open: { _ in }).padding(24).frame(maxWidth: .infinity, alignment: .leading)
            }.background(Color.canvas).environmentObject(store)
            await capture("assistant-cards-\(index + 1)", size: CGSize(width: 640, height: 900), wait: 1.5, column)
            await capture("assistant-cards-\(index + 1)-dark", size: CGSize(width: 390, height: 900), dark: true, wait: 1.5, column)
        }
    } else { print("FAIL assistant sample did not decode") }
    // Health, cycle and the quiet conversation the assistant opens for what the user is going through.
    let health = #"""
    {"id":"h1","session_id":"s","role":"assistant","content":"","status":"complete","created_at":0,"metadata":{"steps":[
      {"text":"这周平均睡了 6 个多小时，最近三晚都偏短。","calls":[
        {"id":"c1","name":"health_summary","label":"看健康数据","status":"ok","card":{"kind":"health","title":"最近 7 天 · 健康","subtitle":"11/14 – 11/20","value":"6 小时 55 分","icon":"heart",
          "rows":[{"label":"平均睡眠","value":"6 小时 55 分"},{"label":"不足 6 小时","value":"3 晚"},{"label":"日均步数","value":"8,412"},{"label":"静息心率","value":"60 次/分"},{"label":"身体压力","value":"偏高"}],
          "series":[7.5,7.8,7.1,7.6,5.8,5.6,5.9],"note":"估算：根据心率变异性、静息心率和睡眠与你自己 30 天的平均相比，不是医学测量"}},
        {"id":"c2","name":"cycle_status","label":"看经期","status":"ok","card":{"kind":"cycle","title":"经期","subtitle":"周期第 13 天 · 卵泡期","value":"17 天后","icon":"drop",
          "rows":[{"label":"下次预计","value":"12/07"},{"label":"平均周期","value":"29 天"},{"label":"上次经期","value":"11/08 · 5 天"}],"note":"根据你自己的记录估算，不能用于避孕"}},
        {"id":"c3","name":"open_session","label":"开新对话","status":"ok","card":{"kind":"session","title":"最近的压力","subtitle":"新对话 · 陪你聊聊","session_id":"x","note":"连着一个月加班，回家还要吵架，这真的很耗人。想先从哪一件说起都可以。"}}]}]}}
    """#
    if let message = try? JSONDecoder().decode(Message.self, from: Data(health.utf8)) {
        let column = ScrollView { AssistantMessage(message: message, open: { _ in }).padding(24).frame(maxWidth: .infinity, alignment: .leading) }.background(Color.canvas).environmentObject(store)
        await capture("health-cards", size: CGSize(width: 640, height: 900), wait: 1.5, column)
        await capture("health-cards-dark", size: CGSize(width: 390, height: 900), dark: true, wait: 1.5, column)
    } else { print("FAIL health sample did not decode") }
    await store.loadCodeProjects()
    await store.newChat(project: store.codeProjects.first?.id)
    await capture("home-with-project", root)
    await store.newChat()
    await capture("home-narrow", size: CGSize(width: 820, height: 600), root)
    await capture("home-dark", dark: true, root)
    print("done \(output.path)")
    exit(0)
}

@main enum UISmoke {
    static func main() {
        try? FileManager.default.createDirectory(at: output, withIntermediateDirectories: true)
        let app = NSApplication.shared
        app.setActivationPolicy(.prohibited)
        Task { @MainActor in await run() }
        app.run()
    }
}
