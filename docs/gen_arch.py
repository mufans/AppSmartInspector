"""Generate architecture diagram SVG with precise layout control."""

SVG_HEAD = """<svg xmlns="http://www.w3.org/2000/svg" width="1200" height="540" viewBox="0 0 1200 540">
<style>
  text { font-family: -apple-system, "PingFang SC", "Microsoft YaHei", system-ui, sans-serif; }
  .title { font-size: 20px; font-weight: bold; fill: #1a1a2e; }
  .layer-label { font-size: 12px; font-weight: bold; fill: #555; }
  .mod-name { font-size: 13px; font-weight: bold; }
  .mod-desc { font-size: 11px; fill: #666; }
  .arrow { fill: none; stroke: #888; stroke-width: 1.5; marker-end: url(#ah); }
  .dash { fill: none; stroke: #aaa; stroke-width: 1.2; stroke-dasharray: 5,3; marker-end: url(#ah2); }
</style>
<defs>
  <marker id="ah" markerWidth="8" markerHeight="6" refX="8" refY="3" orient="auto">
    <polygon points="0 0, 8 3, 0 6" fill="#888"/>
  </marker>
  <marker id="ah2" markerWidth="7" markerHeight="5" refX="7" refY="2.5" orient="auto">
    <polygon points="0 0, 7 2.5, 0 5" fill="#aaa"/>
  </marker>
</defs>
"""

C = {
    "outer": "#f8f9fa",
    "entry": ("#E8F0FE", "#A8C7FA", "#3B82F6"),
    "pipe":  ("#E6F4EA", "#A8DAB5", "#0D9488"),
    "agent": ("#FEF7E0", "#F9E088", "#D97706"),
    "coll":  ("#FFF3E6", "#FFCC80", "#E65100"),
    "sdk":   ("#FCE8E6", "#F5A8A2", "#EF4444"),
    "out":   ("#F3E8FD", "#D6B4F8", "#7C3AED"),
    "orc":   "#6366F1",
}

LAYER_H = 72
CARD_H = 44
PAD = 20
MAIN_W = 930
SIDE_X = 960
SIDE_W = 220


def rrect(x, y, w, h, fill, stroke, rx=8, sw=1.5):
    return f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{rx}" fill="{fill}" stroke="{stroke}" stroke-width="{sw}"/>'


def card(x, y, w, accent, name, desc=""):
    p = [
        rrect(x, y, w, CARD_H, "#fff", "#ddd", rx=6, sw=1),
        f'<rect x="{x}" y="{y}" width="4" height="{CARD_H}" rx="2" fill="{accent}"/>',
        f'<text x="{x+14}" y="{y+17}" class="mod-name" fill="#333">{name}</text>',
    ]
    if desc:
        p.append(f'<text x="{x+14}" y="{y+34}" class="mod-desc">{desc}</text>')
    return "\n".join(p)


def layer_row(y, label, bg, border, modules, gap=10):
    p = [
        rrect(PAD, y, MAIN_W, LAYER_H, bg, border, rx=10),
        f'<text x="{PAD+12}" y="{y+15}" class="layer-label">{label}</text>',
    ]
    n = len(modules)
    mw = (MAIN_W - 20 - (n - 1) * gap) / n
    mx = PAD + 10
    my = y + 20
    cs = []
    for name, desc, accent in modules:
        p.append(card(mx, my, mw, accent, name, desc))
        cs.append({"cx": mx + mw/2, "cy": my + CARD_H/2,
                    "r": mx + mw, "l": mx, "t": my, "b": my + CARD_H})
        mx += mw + gap
    return "\n".join(p), cs, y + LAYER_H


def pipeline_row(y, bg, border, modules, agap=32):
    p = [
        rrect(PAD, y, MAIN_W, LAYER_H, bg, border, rx=10),
        f'<text x="{PAD+12}" y="{y+15}" class="layer-label">PIPELINE · LangGraph 全量分析</text>',
    ]
    n = len(modules)
    mw = (MAIN_W - 20 - (n - 1) * agap) / n
    mx = PAD + 10
    my = y + 20
    cs = []
    for i, (name, desc, accent) in enumerate(modules):
        p.append(card(mx, my, mw, accent, name, desc))
        cs.append({"cx": mx + mw/2, "cy": my + CARD_H/2,
                    "r": mx + mw, "l": mx, "t": my, "b": my + CARD_H})
        if i < n - 1:
            a1 = mx + mw + 3
            a2 = mx + mw + agap - 3
            ay = my + CARD_H / 2
            p.append(f'<line x1="{a1:.0f}" y1="{ay:.0f}" x2="{a2:.0f}" y2="{ay:.0f}" class="arrow"/>')
        mx += mw + agap
    return "\n".join(p), cs, y + LAYER_H


def build():
    p = [SVG_HEAD]
    # outer + title
    p.append(rrect(2, 2, 1196, 536, C["outer"], "#bbb", rx=12, sw=2))
    p.append(f'<text x="490" y="34" text-anchor="middle" class="title">SmartInspector Architecture</text>')

    y = 50

    # ── Entry ──
    l1, c1, y = layer_row(y, "ENTRY · 入口层", *C["entry"][:2], [
        ("CLI 交互",       "自然语言 + Slash 命令", C["entry"][2]),
        ("CI / Headless",  "非交互式全量流水线",    C["entry"][2]),
        ("MCP Server",     "23 个 MCP Tools",       C["entry"][2]),
        ("Perfetto UI",    "框选帧交互分析",        C["entry"][2]),
    ])
    p.append(l1)

    # ── Orchestrator pill ──
    y += 10
    orc_cy = y + 18
    p.append(rrect(420, y, 140, 36, C["orc"], "#4338CA", rx=18, sw=2))
    p.append(f'<text x="490" y="{y+15}" text-anchor="middle" fill="#fff" font-size="12" font-weight="bold">Orchestrator</text>')
    p.append(f'<text x="490" y="{y+29}" text-anchor="middle" fill="#ddd6fe" font-size="10">意图路由</text>')
    p.append(f'<line x1="490" y1="{50+LAYER_H}" x2="490" y2="{y}" class="arrow"/>')
    y = orc_cy + 22

    # ── Pipeline ──
    y += 8
    pt = y
    l2, c2, y = pipeline_row(y, *C["pipe"][:2], [
        ("Collector",  "Trace采集 + SQL",  C["pipe"][2]),
        ("Analyzer",   "LLM 性能解读",     C["pipe"][2]),
        ("Attributor", "SI$ → 源码归因",   C["pipe"][2]),
        ("Reporter",   "Markdown / JSON",  C["pipe"][2]),
    ])
    p.append(l2)
    p.append(f'<line x1="490" y1="{orc_cy+18}" x2="490" y2="{pt}" class="arrow"/>')

    # ── Agents ──
    y += 12
    l3, c3, y = layer_row(y, "AGENTS · Agent 层", *C["agent"][:2], [
        ("perf_analyzer",   "流式 LLM + 验证重试",  C["agent"][2]),
        ("deterministic",   "18 模块 · 0 token",    C["agent"][2]),
        ("verifier",        "L1+L2 质量验证",        C["agent"][2]),
        ("frame_analyzer",  "帧级切片归因",          C["agent"][2]),
    ])
    p.append(l3)
    p.append(f'<line x1="{c2[1]["r"]:.0f}" y1="{c2[1]["cy"]:.0f}" x2="{c3[0]["cx"]:.0f}" y2="{c3[0]["t"]:.0f}" class="dash"/>')

    # ── Collector ──
    y += 12
    l4, c4, y = layer_row(y, "COLLECTOR · 采集 + 维度注册", *C["coll"][:2], [
        ("PerfettoCollector", "28+ SQL 方法",      C["coll"][2]),
        ("StartupAnalyzer",   "冷启动阶段切分",     C["coll"][2]),
        ("MemoryAnalyzer",    "Heap 泄漏检测",      C["coll"][2]),
        ("DimensionRegistry", "7 维度插件注册",     C["coll"][2]),
    ])
    p.append(l4)

    # ── SDK ──
    y += 12
    l5, c5, y = layer_row(y, "ANDROID SDK · Pine AOP", *C["sdk"][:2], [
        ("TraceHook",     "SI$ tag 注入",        C["sdk"][2]),
        ("BlockMonitor",  "主线程卡顿 ≥100ms",   C["sdk"][2]),
        ("WebSocket",     "CLI ↔ App 通信",      C["sdk"][2]),
    ])
    p.append(l5)
    p.append(f'<line x1="{c5[0]["cx"]:.0f}" y1="{c5[0]["t"]:.0f}" x2="{c4[0]["cx"]:.0f}" y2="{c4[0]["b"]:.0f}" class="dash"/>')

    # Perfetto UI → frame_analyzer
    p.append(f'<line x1="{c1[3]["cx"]:.0f}" y1="{c1[3]["b"]:.0f}" x2="{c3[3]["cx"]:.0f}" y2="{c3[3]["t"]:.0f}" class="dash"/>')

    # ── Output sidebar ──
    sb_top = 50
    sb_bot = y
    sb_h = sb_bot - sb_top
    p.append(rrect(SIDE_X, sb_top, SIDE_W, sb_h, *C["out"][:2], rx=10))
    p.append(f'<text x="{SIDE_X+12}" y="{sb_top+16}" class="layer-label">OUTPUT · 输出</text>')

    # 3 cards vertically centered, compact
    card_h = 44
    card_gap = 10
    total_cards_h = 3 * card_h + 2 * card_gap
    start_y = sb_top + (sb_h - total_cards_h) / 2
    cy = start_y
    items = [
        ("Markdown",  "可读性强的分析报告",  C["out"][2]),
        ("JSON",      "CI/CD 自动化解析",   C["out"][2]),
        ("reports/",  "文件持久化保存",      C["out"][2]),
    ]
    mid_card_cy = None
    for i, (name, desc, accent) in enumerate(items):
        p.append(card(SIDE_X + 10, cy, SIDE_W - 20, accent, name, desc))
        if i == 1:
            mid_card_cy = cy + card_h / 2
        cy += card_h + card_gap

    # Reporter → Output sidebar (horizontal arrow)
    p.append(f'<line x1="{c2[3]["r"]:.0f}" y1="{c2[3]["cy"]:.0f}" x2="{SIDE_X}" y2="{mid_card_cy:.0f}" class="arrow"/>')

    p.append("</svg>")
    return "\n".join(p)


if __name__ == "__main__":
    svg = build()
    out = "/Users/liujun/langchainProjects/AppSmartInspector/docs/architecture.svg"
    with open(out, "w", encoding="utf-8") as f:
        f.write(svg)
    print(f"Saved to {out}")
