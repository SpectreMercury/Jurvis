import SwiftUI

struct GenerationSettings {
    var model = "gguf_q5"
    var sampling = "balanced"
    var steps = 6
    var motion = 0.0
    var payload: [String:Any] { ["model_profile":model,"sampling_profile":sampling,"turbo_steps":steps,"motion_strength":motion] }
}

struct GenerationOptions: View {
    @EnvironmentObject var store: AppStore
    @Binding var settings: GenerationSettings
    func available(_ id:String)->Bool { store.components.contains{$0.id==id && $0.installed} }
    var body: some View {
        DisclosureGroup("生成配置") {
            VStack(alignment:.leading,spacing:16) {
                Picker("主模型",selection:$settings.model) {
                    Text("GGUF Q5 · 较省内存").tag("gguf_q5")
                    if available("h3_convrot_int8") { Text("INT8 ConvRot · 21GB 权重").tag("convrot_int8") }
                }
                Picker("采样方案",selection:$settings.sampling) {
                    Text("标准加速 · 8 步").tag("balanced")
                    if available("h3_turbo_v2") { Text("Turbo multistep v2").tag("turbo_v2") }
                    if available("dasiwa_v33") { Text("DaSiWa v3.3 · Mac 核心流程 · 25 步").tag("dasiwa_v33") }
                }
                if settings.sampling == "turbo_v2" {
                    Picker("采样步数",selection:$settings.steps) { Text("4 · 草稿").tag(4);Text("6 · 均衡").tag(6);Text("8 · 精修").tag(8) }.pickerStyle(.segmented)
                }
                if available("h3_motion_fos") {
                    HStack { Text("运动强度"); Slider(value:$settings.motion,in:-8...8,step:0.5); Text(settings.motion,format:.number.precision(.fractionLength(1))).monospacedDigit().frame(width:40); Button("复位"){settings.motion=0}.buttonStyle(.borderless) }
                    Text("负值偏慢动作，正值增强运动；0 为关闭。它影响生成内容，不是播放倍速。").font(.caption).foregroundStyle(.secondary)
                }
                if settings.sampling == "dasiwa_v33" { Text("采用 v3.3 的核心采样设置。当前支持文本、首帧、尾帧和首尾帧输入；RTX 增强、多模态 Ref2VA 和潜空间续拍尚未启用。").font(.caption).foregroundStyle(.secondary) }
                Text("未安装的扩展可在设置中查看。新组合需要审片确认画面和声音效果。").font(.caption).foregroundStyle(.secondary)
            }.padding(.top,12)
        }.task { await store.reloadSettings() }
    }
}
