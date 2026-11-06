// Renders the Jurvis app icon.
// iOS gets an opaque full-bleed square (the system applies the mask); macOS gets
// the standard squircle with margin and drop shadow.
// Usage:
//   swift scripts/make_icon.swift <project root> [design]            write the asset catalog
//   swift scripts/make_icon.swift <project root> <design> --preview <out.png>
// Designs: jot (default, the shipped icon), jotdark, swirl, loop, flow, ribbon, aurora, spark, neural, orbj, wave.
import AppKit
import Foundation

let args = CommandLine.arguments
let root = URL(fileURLWithPath: args[1])
let design = args.count > 2 && !args[2].hasPrefix("-") ? args[2] : "jot"
let previewPath = args.firstIndex(of: "--preview").map { args[$0 + 1] }

let space = CGColorSpace(name: CGColorSpace.sRGB)!
func rgb(_ r: CGFloat, _ g: CGFloat, _ b: CGFloat, _ a: CGFloat = 1) -> CGColor { CGColor(srgbRed: r, green: g, blue: b, alpha: a) }
func gradient(_ colors: [CGColor], _ locations: [CGFloat]? = nil) -> CGGradient {
    CGGradient(colorsSpace: space, colors: colors as CFArray, locations: locations)!
}
let cyan = rgb(0.25, 0.88, 1), blue = rgb(0.32, 0.45, 1), violet = rgb(0.62, 0.36, 1), magenta = rgb(1, 0.38, 0.78), peach = rgb(1, 0.66, 0.45)

struct Canvas {
    let ctx: CGContext, c: CGPoint, s: CGFloat, detailed: Bool
    func p(_ x: CGFloat, _ y: CGFloat) -> CGPoint { CGPoint(x: c.x + (x - 512) * s, y: c.y + (y - 512) * s) }
    func circle(_ center: CGPoint, _ r: CGFloat) -> CGRect { CGRect(x: center.x - r * s, y: center.y - r * s, width: 2 * r * s, height: 2 * r * s) }
    func glow(_ color: CGColor, _ blur: CGFloat, _ body: () -> Void) {
        ctx.saveGState(); ctx.setShadow(offset: .zero, blur: blur * s, color: color); body(); ctx.restoreGState()
    }
}

// MARK: - Designs

/// A living orb of swirling light: the assistant as a presence rather than a letter.
func aurora(_ k: Canvas) {
    let ctx = k.ctx, c = k.c, s = k.s
    ctx.drawRadialGradient(gradient([rgb(0.55, 0.35, 1, 0.55), rgb(0.2, 0.5, 1, 0.18), rgb(0.2, 0.5, 1, 0)], [0, 0.55, 1]),
                           startCenter: c, startRadius: 0, endCenter: c, endRadius: 470 * s, options: [])
    // Orbit: the far half is drawn before the orb so the sphere hides it.
    func orbit(_ from: CGFloat, _ to: CGFloat, alpha: CGFloat) {
        guard k.detailed else { return }
        ctx.saveGState()
        ctx.translateBy(x: c.x, y: c.y); ctx.rotate(by: -0.35); ctx.scaleBy(x: 1, y: 0.32)
        ctx.addArc(center: .zero, radius: 400 * s, startAngle: from, endAngle: to, clockwise: false)
        ctx.restoreGState()
        ctx.setStrokeColor(rgb(0.75, 0.8, 1, alpha)); ctx.setLineWidth(4 * s); ctx.strokePath()
    }
    orbit(0, .pi, alpha: 0.3)
    let orb = k.circle(c, 300)
    ctx.saveGState()
    ctx.addEllipse(in: orb); ctx.clip()
    conic(ctx, center: k.p(560, 560), radius: 520 * s, stops: [(0, cyan), (0.22, blue), (0.45, violet), (0.66, magenta), (0.82, peach), (1, cyan)], rotation: 0.6)
    // Soft swirls of light blended on top for a fluid, generative feel.
    ctx.setBlendMode(.screen)
    for (x, y, r, color) in [(400.0, 640.0, 210.0, cyan), (650.0, 380.0, 230.0, magenta), (560.0, 560.0, 200.0, rgb(1, 1, 1, 0.95))] as [(CGFloat, CGFloat, CGFloat, CGColor)] {
        ctx.drawRadialGradient(gradient([color.copy(alpha: 0.75)!, color.copy(alpha: 0)!]), startCenter: k.p(x, y), startRadius: 0, endCenter: k.p(x, y), endRadius: r * s, options: [])
    }
    ctx.setBlendMode(.normal)
    // Depth: darken the rim, then a glassy specular highlight.
    ctx.drawRadialGradient(gradient([rgb(0, 0, 0.05, 0), rgb(0.02, 0, 0.1, 0.55)], [0.55, 1]), startCenter: c, startRadius: 0, endCenter: c, endRadius: 300 * s, options: [])
    ctx.saveGState()
    ctx.translateBy(x: k.p(420, 690).x, y: k.p(420, 690).y); ctx.scaleBy(x: 1.6, y: 1)
    ctx.drawRadialGradient(gradient([rgb(1, 1, 1, 0.55), rgb(1, 1, 1, 0)]), startCenter: .zero, startRadius: 0, endCenter: .zero, endRadius: 95 * s, options: [])
    ctx.restoreGState()
    ctx.restoreGState()
    ctx.addEllipse(in: orb.insetBy(dx: 1.5 * s, dy: 1.5 * s)); ctx.setStrokeColor(rgb(1, 1, 1, 0.22)); ctx.setLineWidth(3 * s); ctx.strokePath()
    orbit(.pi, 2 * .pi, alpha: 0.45)
    if k.detailed {
        let a: CGFloat = 1.72 * .pi, local = CGPoint(x: cos(a) * 400 * s, y: sin(a) * 400 * s * 0.32)
        let satellite = CGPoint(x: c.x + local.x * cos(-0.35) - local.y * sin(-0.35), y: c.y + local.x * sin(-0.35) + local.y * cos(-0.35))
        k.glow(cyan, 20) { ctx.setFillColor(rgb(0.9, 0.98, 1)); ctx.fillEllipse(in: k.circle(satellite, 13)) }
    }
}

/// A four-point spark: generation, insight, the moment an idea appears.
func spark(_ k: Canvas) {
    let ctx = k.ctx, s = k.s
    func star(_ cx: CGFloat, _ cy: CGFloat, _ h: CGFloat, _ w: CGFloat, pinch: CGFloat) -> CGPath {
        let path = CGMutablePath(), o = k.p(cx, cy)
        let tips = [CGPoint(x: o.x, y: o.y + h * s), CGPoint(x: o.x + w * s, y: o.y), CGPoint(x: o.x, y: o.y - h * s), CGPoint(x: o.x - w * s, y: o.y)]
        path.move(to: tips[0])
        for i in 0..<4 {
            let a = tips[i], b = tips[(i + 1) % 4]
            let control = CGPoint(x: o.x + (a.x + b.x - 2 * o.x) * pinch, y: o.y + (a.y + b.y - 2 * o.y) * pinch)
            path.addQuadCurve(to: b, control: control)
        }
        path.closeSubpath(); return path
    }
    ctx.drawRadialGradient(gradient([rgb(0.5, 0.35, 1, 0.5), rgb(0.5, 0.35, 1, 0)]), startCenter: k.p(500, 500), startRadius: 0, endCenter: k.p(500, 500), endRadius: 430 * s, options: [])
    func fill(_ path: CGPath, glowColor: CGColor) {
        k.glow(glowColor, 50) { ctx.addPath(path); ctx.setFillColor(rgb(0.7, 0.6, 1)); ctx.fillPath() }
        ctx.saveGState(); ctx.addPath(path); ctx.clip()
        let box = path.boundingBox
        ctx.drawLinearGradient(gradient([cyan, violet, magenta], [0, 0.55, 1]), start: CGPoint(x: box.minX, y: box.maxY), end: CGPoint(x: box.maxX, y: box.minY), options: [])
        ctx.drawRadialGradient(gradient([rgb(1, 1, 1, 0.85), rgb(1, 1, 1, 0)]), startCenter: CGPoint(x: box.midX, y: box.midY), startRadius: 0,
                               endCenter: CGPoint(x: box.midX, y: box.midY), endRadius: box.width * 0.28, options: [])
        ctx.restoreGState()
    }
    fill(star(470, 470, 330, 290, pinch: 0.12), glowColor: violet)
    fill(star(735, 740, 120, 105, pinch: 0.12), glowColor: cyan)
    if k.detailed { fill(star(735, 260, 58, 50, pinch: 0.14), glowColor: magenta) }
}

/// A constellation of connected nodes that resolves into a "J": a network that knows you.
func neural(_ k: Canvas) {
    let ctx = k.ctx, s = k.s
    ctx.drawRadialGradient(gradient([rgb(0.3, 0.45, 1, 0.45), rgb(0.3, 0.45, 1, 0)]), startCenter: k.p(520, 540), startRadius: 0, endCenter: k.p(520, 540), endRadius: 460 * s, options: [])
    let spine: [(CGFloat, CGFloat)] = [(390, 790), (560, 790), (720, 790), (560, 640), (560, 490), (545, 370), (470, 290), (370, 310), (320, 400)]
    let edges = [(0, 1), (1, 2), (1, 3), (3, 4), (4, 5), (5, 6), (6, 7), (7, 8)]
    let outer: [(CGFloat, CGFloat, Int)] = [(250, 640, 3), (300, 860, 0), (820, 640, 2), (760, 470, 4), (800, 300, 5), (640, 200, 6), (230, 250, 8), (410, 520, 4)]
    let nodes = spine.map { k.p($0.0, $0.1) }
    if k.detailed {
        for (x, y, link) in outer {
            ctx.move(to: k.p(x, y)); ctx.addLine(to: nodes[link])
            ctx.setStrokeColor(rgb(0.6, 0.7, 1, 0.28)); ctx.setLineWidth(4 * s); ctx.strokePath()
            ctx.setFillColor(rgb(0.7, 0.78, 1, 0.55)); ctx.fillEllipse(in: k.circle(k.p(x, y), 11))
        }
    }
    // Bright J edges, coloured along the path from cyan to magenta.
    for (n, (a, b)) in edges.enumerated() {
        let t = CGFloat(n) / CGFloat(edges.count - 1)
        let color = t < 0.5 ? cyan.blend(violet, t * 2) : violet.blend(magenta, (t - 0.5) * 2)
        k.glow(color, 26) {
            ctx.move(to: nodes[a]); ctx.addLine(to: nodes[b])
            ctx.setStrokeColor(color); ctx.setLineWidth((k.detailed ? 22 : 34) * s); ctx.setLineCap(.round); ctx.strokePath()
        }
    }
    for (i, node) in nodes.enumerated() where k.detailed || [0, 2, 4, 8].contains(i) {
        let r: CGFloat = [1, 4, 6].contains(i) ? 34 : 26
        k.glow(rgb(0.8, 0.9, 1), 30) { ctx.setFillColor(rgb(0.96, 0.98, 1)); ctx.fillEllipse(in: k.circle(node, r)) }
    }
}

/// The aurora orb with a luminous "J" inside: the AI presence, signed with the brand.
func orbJ(_ k: Canvas) {
    let ctx = k.ctx, c = k.c, s = k.s
    ctx.drawRadialGradient(gradient([rgb(0.55, 0.35, 1, 0.5), rgb(0.2, 0.5, 1, 0.15), rgb(0.2, 0.5, 1, 0)], [0, 0.55, 1]),
                           startCenter: c, startRadius: 0, endCenter: c, endRadius: 480 * s, options: [])
    let orb = k.circle(c, 330)
    ctx.saveGState()
    ctx.addEllipse(in: orb); ctx.clip()
    conic(ctx, center: c, radius: 560 * s, stops: [(0, cyan), (0.25, blue), (0.5, violet), (0.72, magenta), (0.86, peach), (1, cyan)], rotation: 2.2)
    ctx.setBlendMode(.screen)
    for (x, y, r, color) in [(380.0, 660.0, 230.0, cyan), (680.0, 360.0, 250.0, magenta), (512.0, 512.0, 170.0, rgb(1, 1, 1, 0.55))] as [(CGFloat, CGFloat, CGFloat, CGColor)] {
        ctx.drawRadialGradient(gradient([color.copy(alpha: 0.7)!, color.copy(alpha: 0)!]), startCenter: k.p(x, y), startRadius: 0, endCenter: k.p(x, y), endRadius: r * s, options: [])
    }
    ctx.setBlendMode(.normal)
    ctx.drawRadialGradient(gradient([rgb(0, 0, 0.05, 0), rgb(0.02, 0, 0.1, 0.5)], [0.6, 1]), startCenter: c, startRadius: 0, endCenter: c, endRadius: 330 * s, options: [])
    ctx.restoreGState()
    ctx.addEllipse(in: orb.insetBy(dx: 1.5 * s, dy: 1.5 * s)); ctx.setStrokeColor(rgb(1, 1, 1, 0.25)); ctx.setLineWidth(3 * s); ctx.strokePath()
    glyphJ(k, size: k.detailed ? 400 : 470, glow: rgb(1, 1, 1, 0.9))
}

/// A single flowing ribbon that writes the "J".
func ribbon(_ k: Canvas) {
    let ctx = k.ctx, s = k.s
    ctx.drawRadialGradient(gradient([rgb(0.45, 0.35, 1, 0.45), rgb(0.45, 0.35, 1, 0)]), startCenter: k.p(520, 540), startRadius: 0, endCenter: k.p(520, 540), endRadius: 470 * s, options: [])
    let path = CGMutablePath()
    path.move(to: k.p(330, 742))
    path.addCurve(to: k.p(612, 770), control1: k.p(420, 800), control2: k.p(540, 800))
    path.addCurve(to: k.p(612, 440), control1: k.p(660, 720), control2: k.p(612, 560))
    path.addCurve(to: k.p(360, 330), control1: k.p(612, 250), control2: k.p(420, 230))
    let width: CGFloat = k.detailed ? 118 : 150
    let stroked = path.copy(strokingWithWidth: width * s, lineCap: .round, lineJoin: .round, miterLimit: 10)
    k.glow(violet.copy(alpha: 0.9)!, 60) { ctx.addPath(stroked); ctx.setFillColor(rgb(0.5, 0.45, 1)); ctx.fillPath() }
    ctx.saveGState()
    ctx.addPath(stroked); ctx.clip()
    ctx.drawLinearGradient(gradient([cyan, blue, violet, magenta, peach], [0, 0.3, 0.55, 0.82, 1]), start: k.p(640, 800), end: k.p(330, 300), options: [.drawsBeforeStartLocation, .drawsAfterEndLocation])
    // Gloss: one soft highlight riding the top bar and upper stem, stopping short of the ends.
    let gloss = CGMutablePath()
    gloss.move(to: k.p(372, 766))
    gloss.addCurve(to: k.p(586, 784), control1: k.p(440, 800), control2: k.p(530, 806))
    gloss.addCurve(to: k.p(590, 540), control1: k.p(624, 740), control2: k.p(592, 640))
    ctx.saveGState()
    ctx.translateBy(x: -width * 0.12 * s, y: width * 0.08 * s)
    ctx.addPath(gloss.copy(strokingWithWidth: width * 0.2 * s, lineCap: .round, lineJoin: .round, miterLimit: 10))
    ctx.setFillColor(rgb(1, 1, 1, k.detailed ? 0.34 : 0.22)); ctx.fillPath()
    ctx.restoreGState()
    ctx.restoreGState()
    if k.detailed {
        k.glow(cyan, 24) { ctx.setFillColor(rgb(0.92, 0.98, 1)); ctx.fillEllipse(in: k.circle(k.p(745, 760), 22)) }
    }
}

/// Abstract gradient ribbons in the style of `ribbon`, without a letter.
func ribbonShape(_ k: Canvas, kind: String) {
    let ctx = k.ctx, s = k.s
    ctx.drawRadialGradient(gradient([rgb(0.45, 0.35, 1, 0.45), rgb(0.45, 0.35, 1, 0)]), startCenter: k.p(512, 512), startRadius: 0, endCenter: k.p(512, 512), endRadius: 470 * s, options: [])
    var points: [CGPoint] = []
    let n = 240
    for i in 0...n {
        let t = CGFloat(i) / CGFloat(n)
        switch kind {
        case "loop":   // lemniscate, tilted
            let a = t * 2 * .pi + .pi / 2, d = 1 + sin(a) * sin(a)
            let x = 340 * cos(a) / d, y = 340 * sin(a) * cos(a) / d * 1.35
            points.append(k.p(512 + x, 512 + y))
        case "swirl":  // inward spiral
            let angle = (0.62 - t * 1.78) * .pi, radius = 330 - 205 * t
            points.append(k.p(512 + cos(angle) * radius, 500 + sin(angle) * radius))
        default:       // flowing S wave
            let x = 240 + 544 * t, y = 512 + 165 * sin(2 * .pi * t)
            points.append(k.p(x, y))
        }
    }
    func path(_ range: ClosedRange<Int>) -> CGPath {
        let path = CGMutablePath(); path.addLines(between: Array(points[range])); return path
    }
    let width: CGFloat = (kind == "swirl" ? 104 : 96) * (k.detailed ? 1 : 1.35)
    let stops = gradient([cyan, blue, violet, magenta, peach], [0, 0.3, 0.55, 0.8, 1])
    let box = path(0...n).boundingBox
    func paint(_ piece: CGPath, crossings: [CGPath] = []) {
        let stroked = piece.copy(strokingWithWidth: width * s, lineCap: .round, lineJoin: .round, miterLimit: 10)
        for crossing in crossings {  // a dark rim only where this piece passes over the other
            ctx.addPath(crossing.copy(strokingWithWidth: (width + 28) * s, lineCap: .butt, lineJoin: .round, miterLimit: 10))
            ctx.setFillColor(rgb(0.04, 0.03, 0.12, 0.9)); ctx.fillPath()
        }
        k.glow(violet.copy(alpha: 0.85)!, 54) { ctx.addPath(stroked); ctx.setFillColor(rgb(0.5, 0.45, 1)); ctx.fillPath() }
        ctx.saveGState(); ctx.addPath(stroked); ctx.clip()
        ctx.drawLinearGradient(stops, start: CGPoint(x: box.minX, y: box.maxY), end: CGPoint(x: box.maxX, y: box.minY), options: [.drawsBeforeStartLocation, .drawsAfterEndLocation])
        ctx.restoreGState()
    }
    if kind == "loop" {
        paint(path(0...n / 2))
        paint(path(n / 2...n), crossings: [path(n / 2...n / 2 + 22), path(n - 22...n)])
    } else {
        paint(path(0...n))
    }
    // Gloss along the first stretch of the ribbon, stopping short of the ends.
    let glossRange = kind == "loop" ? 20...100 : 16...(n * 11 / 20)
    ctx.saveGState()
    ctx.translateBy(x: -width * 0.12 * s, y: width * 0.12 * s)
    ctx.addPath(path(glossRange).copy(strokingWithWidth: width * 0.2 * s, lineCap: .round, lineJoin: .round, miterLimit: 10))
    ctx.setFillColor(rgb(1, 1, 1, k.detailed ? 0.32 : 0.2)); ctx.fillPath()
    ctx.restoreGState()
    if k.detailed {
        let spark = kind == "swirl" ? k.p(512, 500) : kind == "loop" ? k.p(512, 790) : k.p(790, 770)
        k.glow(cyan, 26) { ctx.setFillColor(rgb(0.92, 0.98, 1)); ctx.fillEllipse(in: k.circle(spark, kind == "swirl" ? 30 : 22)) }
    }
}


/// A voice waveform: the assistant you talk to.
func wave(_ k: Canvas) {
    let ctx = k.ctx, s = k.s
    ctx.drawRadialGradient(gradient([rgb(0.4, 0.4, 1, 0.45), rgb(0.4, 0.4, 1, 0)]), startCenter: k.p(512, 512), startRadius: 0, endCenter: k.p(512, 512), endRadius: 460 * s, options: [])
    let heights: [CGFloat] = k.detailed ? [120, 250, 430, 560, 430, 250, 120] : [220, 470, 620, 470, 220]
    let barWidth: CGFloat = k.detailed ? 62 : 104, gap: CGFloat = k.detailed ? 34 : 46
    let total = CGFloat(heights.count) * barWidth + CGFloat(heights.count - 1) * gap
    let bars = CGMutablePath()
    for (i, h) in heights.enumerated() {
        let x = 512 - total / 2 + CGFloat(i) * (barWidth + gap)
        let origin = k.p(x, 512 - h / 2)
        bars.addRoundedRect(in: CGRect(x: origin.x, y: origin.y, width: barWidth * s, height: h * s), cornerWidth: barWidth / 2 * s, cornerHeight: barWidth / 2 * s)
    }
    k.glow(violet.copy(alpha: 0.9)!, 50) { ctx.addPath(bars); ctx.setFillColor(rgb(0.5, 0.5, 1)); ctx.fillPath() }
    ctx.saveGState(); ctx.addPath(bars); ctx.clip()
    ctx.drawLinearGradient(gradient([cyan, blue, violet, magenta], [0, 0.35, 0.65, 1]), start: k.p(512 - total / 2, 512), end: k.p(512 + total / 2, 512), options: [])
    ctx.drawLinearGradient(gradient([rgb(1, 1, 1, 0.35), rgb(1, 1, 1, 0)]), start: k.p(512, 800), end: k.p(512, 512), options: [])
    ctx.restoreGState()
}

/// Rounded heavy "J" with an inner light, centred on the canvas.
func glyphJ(_ k: Canvas, size: CGFloat, glow: CGColor) {
    let ctx = k.ctx, s = k.s
    let base = NSFont.systemFont(ofSize: size * s, weight: .heavy)
    let font = NSFont(descriptor: base.fontDescriptor.withDesign(.rounded) ?? base.fontDescriptor, size: size * s) ?? base
    let line = CTLineCreateWithAttributedString(NSAttributedString(string: "J", attributes: [.font: font]))
    guard let run = (CTLineGetGlyphRuns(line) as! [CTRun]).first else { return }
    var glyph = CGGlyph(); CTRunGetGlyphs(run, CFRange(location: 0, length: 1), &glyph)
    guard let raw = CTFontCreatePathForGlyph(font as CTFont, glyph, nil) else { return }
    let box = raw.boundingBox
    var move = CGAffineTransform(translationX: k.c.x - box.midX, y: k.c.y - box.midY)
    guard let path = raw.copy(using: &move) else { return }
    k.glow(glow, 46) { ctx.addPath(path); ctx.setFillColor(rgb(1, 1, 1)); ctx.fillPath() }
    ctx.saveGState(); ctx.addPath(path); ctx.clip()
    ctx.drawLinearGradient(gradient([rgb(1, 1, 1), rgb(0.88, 0.9, 1)]), start: CGPoint(x: 0, y: path.boundingBox.maxY), end: CGPoint(x: 0, y: path.boundingBox.minY), options: [])
    ctx.restoreGState()
}

/// A geometric lowercase "j": one even stroke with a single accent dot. No glow, no rainbow.
func jot(_ k: Canvas, ink: CGColor, accent: CGColor) {
    let ctx = k.ctx, s = k.s
    let width: CGFloat = k.detailed ? 128 : 168, stem: CGFloat = 604, bend: CGFloat = 380, r: CGFloat = 136
    let path = CGMutablePath()
    path.move(to: k.p(stem, 600))
    path.addLine(to: k.p(stem, bend))
    path.addArc(center: k.p(stem - r, bend), radius: r * s, startAngle: 0, endAngle: -.pi * 0.95, clockwise: true)
    ctx.addPath(path.copy(strokingWithWidth: width * s, lineCap: .round, lineJoin: .round, miterLimit: 10))
    ctx.setFillColor(ink); ctx.fillPath()
    ctx.setFillColor(accent); ctx.fillEllipse(in: k.circle(k.p(stem, 772), width * 0.56))
}

/// Angular gradient drawn as thin wedges (CoreGraphics has no public conic gradient in Swift).
func conic(_ ctx: CGContext, center: CGPoint, radius: CGFloat, stops: [(CGFloat, CGColor)], rotation: CGFloat) {
    let steps = 360
    for i in 0..<steps {
        let t = CGFloat(i) / CGFloat(steps)
        let upper = stops.firstIndex { $0.0 >= t } ?? stops.count - 1
        let a = stops[max(upper - 1, 0)], b = stops[upper]
        let color = a.1.blend(b.1, b.0 == a.0 ? 0 : (t - a.0) / (b.0 - a.0))
        let start = rotation + t * 2 * .pi, end = start + 2 * .pi / CGFloat(steps) + 0.01
        ctx.move(to: center)
        ctx.addArc(center: center, radius: radius, startAngle: start, endAngle: end, clockwise: false)
        ctx.closePath(); ctx.setFillColor(color); ctx.fillPath()
    }
}

extension CGColor {
    func blend(_ other: CGColor, _ t: CGFloat) -> CGColor {
        let a = components!, b = other.components!
        return rgb(a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t, a[2] + (b[2] - a[2]) * t)
    }
}

// MARK: - Rendering

func artwork(_ ctx: CGContext, tile: CGRect, rounded: Bool, detailed: Bool) {
    let s = tile.width / 1024
    ctx.saveGState()
    if rounded {
        let shape = CGPath(roundedRect: tile, cornerWidth: 185 * s, cornerHeight: 185 * s, transform: nil)
        ctx.saveGState()
        ctx.setShadow(offset: CGSize(width: 0, height: -10 * s), blur: 28 * s, color: rgb(0, 0, 0, 0.4))
        ctx.addPath(shape); ctx.setFillColor(rgb(0.03, 0.03, 0.08)); ctx.fillPath()
        ctx.restoreGState()
        ctx.addPath(shape); ctx.clip()
    } else {
        ctx.clip(to: tile)
    }
    let paper = rgb(0.957, 0.945, 0.918), ink = rgb(0.075, 0.075, 0.082), ember = rgb(1, 0.36, 0.13)
    let background = design == "jot" ? [rgb(0.985, 0.975, 0.955), paper] : design == "jotdark" ? [rgb(0.13, 0.13, 0.14), ink] : [rgb(0.07, 0.07, 0.16), rgb(0.01, 0.01, 0.05)]
    ctx.drawLinearGradient(gradient(background), start: CGPoint(x: tile.midX, y: tile.maxY), end: CGPoint(x: tile.midX, y: tile.minY), options: [])
    let canvas = Canvas(ctx: ctx, c: CGPoint(x: tile.midX, y: tile.midY), s: s, detailed: detailed)
    switch design {
    case "jot": jot(canvas, ink: ink, accent: ember)
    case "jotdark": jot(canvas, ink: paper, accent: ember)
    case "spark": spark(canvas)
    case "neural": neural(canvas)
    case "orbj": orbJ(canvas)
    case "ribbon": ribbon(canvas)
    case "wave": wave(canvas)
    case "loop", "swirl", "flow": ribbonShape(canvas, kind: design)
    default: aurora(canvas)
    }
    ctx.restoreGState()
}

func render(pixels: Int, rounded: Bool) -> CGImage {
    // iOS icons must be opaque; macOS icons need alpha around the squircle.
    let info = rounded ? CGImageAlphaInfo.premultipliedLast : .noneSkipLast
    let ctx = CGContext(data: nil, width: pixels, height: pixels, bitsPerComponent: 8, bytesPerRow: 0, space: space, bitmapInfo: info.rawValue)!
    ctx.scaleBy(x: CGFloat(pixels) / 1024, y: CGFloat(pixels) / 1024)
    // macOS icon grid: 824-point tile centred in the 1024 canvas.
    let tile = rounded ? CGRect(x: 100, y: 100, width: 824, height: 824) : CGRect(x: 0, y: 0, width: 1024, height: 1024)
    artwork(ctx, tile: tile, rounded: rounded, detailed: pixels > 64)
    return ctx.makeImage()!
}

func write(_ image: CGImage, _ url: URL) throws {
    let destination = CGImageDestinationCreateWithURL(url as CFURL, "public.png" as CFString, 1, nil)!
    CGImageDestinationAddImage(destination, image, nil)
    guard CGImageDestinationFinalize(destination) else { throw CocoaError(.fileWriteUnknown) }
}

if let previewPath {
    try write(render(pixels: 1024, rounded: true), URL(fileURLWithPath: previewPath))
    exit(0)
}

let catalog = root.appendingPathComponent("native/Assets.xcassets")
let folder = catalog.appendingPathComponent("AppIcon.appiconset")
try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
try write(render(pixels: 1024, rounded: false), folder.appendingPathComponent("icon-1024.png"))
var images: [[String: String]] = [["idiom": "universal", "platform": "ios", "size": "1024x1024", "filename": "icon-1024.png"]]
for size in [16, 32, 128, 256, 512] {
    for scale in [1, 2] {
        let name = "mac-\(size)-\(scale).png"
        try write(render(pixels: size * scale, rounded: true), folder.appendingPathComponent(name))
        images.append(["idiom": "mac", "size": "\(size)x\(size)", "scale": "\(scale)x", "filename": name])
    }
}
try JSONSerialization.data(withJSONObject: ["images": images, "info": ["author": "xcode", "version": 1]], options: .prettyPrinted).write(to: folder.appendingPathComponent("Contents.json"))
// In-app brand mark (sidebar, pairing screen): the full-bleed artwork, rounded by SwiftUI.
let mark = catalog.appendingPathComponent("BrandMark.imageset")
try FileManager.default.createDirectory(at: mark, withIntermediateDirectories: true)
try write(render(pixels: 256, rounded: false), mark.appendingPathComponent("brandmark.png"))
try JSONSerialization.data(withJSONObject: ["images": [["idiom": "universal", "filename": "brandmark.png"]], "info": ["author": "xcode", "version": 1]], options: .prettyPrinted).write(to: mark.appendingPathComponent("Contents.json"))
try JSONSerialization.data(withJSONObject: ["info": ["author": "xcode", "version": 1]], options: .prettyPrinted).write(to: catalog.appendingPathComponent("Contents.json"))
