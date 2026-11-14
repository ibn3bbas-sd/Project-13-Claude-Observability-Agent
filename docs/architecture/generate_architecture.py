#!/usr/bin/env python3
# Copyright (c) 2026 Ibn Abbas
# SPDX-License-Identifier: MIT
"""Generate docs/architecture/architecture.svg — the high-level design (HLD).

Single self-contained SVG, FHD 1920x1080, theme-aware: a light and a dark copy
are drawn and swapped with `prefers-color-scheme`, so it reads well on GitHub in
either theme. No dependencies — run `python3 docs/architecture/generate_architecture.py`.
"""
from __future__ import annotations

from pathlib import Path
from xml.sax.saxutils import escape

W, H = 1920, 1080
FONT = "'Inter','Segoe UI','Noto Sans','DejaVu Sans',Helvetica,Arial,sans-serif"
MONO = "'JetBrains Mono','SFMono-Regular',Consolas,'DejaVu Sans Mono',monospace"

THEMES = {
    "lt": dict(bg="#ffffff", dot="#eef1f4", text="#1f2328", muted="#57606a", subtle="#8c959f",
               border="#d0d7de", panel="#f6f8fa", card="#ffffff", bar="#eaeef2",
               blue="#0969da", orange="#bc4c00", green="#1a7f37", purple="#8250df", red="#cf222e",
               gray="#6e7781", teal="#1b7c83", yellow="#9a6700", pink="#bf3989", glyph="#ffffff"),
    "dk": dict(bg="#0d1117", dot="#161b22", text="#e6edf3", muted="#9198a1", subtle="#6e7681",
               border="#30363d", panel="#161b22", card="#161b22", bar="#21262d",
               blue="#4493f8", orange="#f0883e", green="#3fb950", purple="#ab7df8", red="#f85149",
               gray="#8b949e", teal="#39c5cf", yellow="#d29922", pink="#db61a2", glyph="#ffffff"),
}


class Canvas:
    def __init__(self, theme: str):
        self.t = theme
        self.P = THEMES[theme]
        self.out: list[str] = []
        self.late: list[str] = []   # drawn last (boundary pills) so lines never cross them

    # ── primitives ──────────────────────────────────────────────────────────
    def add(self, s: str) -> None:
        self.out.append(s)

    def text(self, x, y, s, size=13, weight=400, color="text", anchor="start", font=FONT, italic=False):
        style = ' font-style="italic"' if italic else ""
        self.add(f'<text x="{x}" y="{y}" font-family="{font}" font-size="{size}" font-weight="{weight}" '
                 f'fill="{self.P.get(color, color)}" text-anchor="{anchor}"{style}>{escape(s)}</text>')

    def rect(self, x, y, w, h, fill="card", stroke="border", sw=1.4, rx=12, dash=None, fill_op=None):
        d = f' stroke-dasharray="{dash}"' if dash else ""
        fo = f' fill-opacity="{fill_op}"' if fill_op is not None else ""
        f = "none" if fill == "none" else self.P.get(fill, fill)
        self.add(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{rx}" ry="{rx}" fill="{f}"{fo} '
                 f'stroke="{self.P.get(stroke, stroke)}" stroke-width="{sw}"{d}/>')

    def line(self, x1, y1, x2, y2, color="border", sw=1.5, dash=None, op=1):
        d = f' stroke-dasharray="{dash}"' if dash else ""
        self.add(f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" stroke="{self.P[color]}" stroke-width="{sw}" '
                 f'stroke-linecap="round"{d} opacity="{op}"/>')

    # ── arrows ──────────────────────────────────────────────────────────────
    def markers(self):
        defs = []
        for c in ("blue", "orange", "green", "gray", "purple", "teal"):
            defs.append(f'<marker id="ah-{self.t}-{c}" viewBox="0 0 10 10" refX="8.5" refY="5" markerWidth="7" '
                        f'markerHeight="7" orient="auto-start-reverse"><path d="M0,0 L10,5 L0,10 z" '
                        f'fill="{self.P[c]}"/></marker>')
        self.add("<defs>" + "".join(defs) + "</defs>")

    def arrow(self, pts, color="gray", dashed=False, both=False, sw=1.8):
        d = "M" + " L".join(f"{x},{y}" for x, y in pts)
        dash = ' stroke-dasharray="7 6"' if dashed else ""
        m = f"url(#ah-{self.t}-{color})"
        start = f' marker-start="{m}"' if both else ""
        self.add(f'<path d="{d}" fill="none" stroke="{self.P[color]}" stroke-width="{sw}" stroke-linejoin="round" '
                 f'stroke-linecap="round"{dash}{start} marker-end="{m}"/>')

    def label(self, x, y, s, color="gray", anchor="middle", size=12.5, weight=600, rot=0):
        # text with a background halo so it stays legible over lines
        tr = f' transform="rotate({rot} {x} {y})"' if rot else ""
        self.add(f'<text{tr} x="{x}" y="{y}" font-family="{FONT}" font-size="{size}" font-weight="{weight}" '
                 f'fill="{self.P[color]}" text-anchor="{anchor}" stroke="{self.P["bg"]}" stroke-width="5" '
                 f'paint-order="stroke" stroke-linejoin="round">{escape(s)}</text>')

    # ── composite shapes ────────────────────────────────────────────────────
    def boundary(self, x, y, w, h, color, title="", note="", off=14, right=False):
        self.add(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="14" ry="14" fill="{self.P[color]}" '
                 f'fill-opacity="0.025" stroke="{self.P[color]}" stroke-width="1.6" stroke-dasharray="8 6"/>')
        if not title:
            return
        main, self.out = self.out, []
        pw = 30 + len(title) * 7.6
        px = x + w - off - pw if right else x + off
        self.rect(px, y - 15, pw, 30, fill="bg", stroke=color, sw=1.6, rx=8)
        self.add(f'<circle cx="{px + 16}" cy="{y}" r="5" fill="{self.P[color]}"/>')
        self.text(px + 28, y + 5, title, 14.5, 600, color)
        if note:
            self.label(px + pw + 12, y + 5, note, "muted", "start", 12, 400)
        self.late += self.out
        self.out = main

    def icon(self, x, y, color, kind, s=34):
        self.add(f'<rect x="{x}" y="{y}" width="{s}" height="{s}" rx="8" fill="{self.P[color]}"/>')
        self.add(f'<g transform="translate({x + s / 2},{y + s / 2}) scale({s / 34})" fill="none" '
                 f'stroke="{self.P["glyph"]}" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round">'
                 f'{GLYPHS[kind]}</g>')

    def card(self, x, y, w, h, color, kind, title, sub="", foot="", foot2=""):
        self.rect(x, y, w, h, fill="card", stroke=color, sw=1.4, rx=10)
        self.icon(x + 12, y + 12, color, kind)
        self.text(x + 56, y + 27, title, 14.5, 700)
        if sub:
            self.text(x + 56, y + 44, sub, 11.8, 400, "muted")
        if foot:
            self.text(x + 14, y + h - (28 if foot2 else 12), foot, 11.2, 400, "subtle")
        if foot2:
            self.text(x + 14, y + h - 12, foot2, 11.2, 400, "subtle")

    def actor(self, cx, y, color, kind, title, sub):
        self.icon(cx - 26, y, color, kind, s=52)
        self.text(cx, y + 72, title, 14.5, 700, anchor="middle")
        self.text(cx, y + 90, sub, 12, 400, "muted", anchor="middle")

    def panel(self, x, y, w, h, title, sub=""):
        self.rect(x, y, w, h, fill="panel", stroke="border", sw=1.4, rx=12)
        self.text(x + 18, y + 32, title, 16.5, 700)
        if sub:
            self.text(x + 18, y + 50, sub, 12, 400, "muted")

    def bullets(self, x, y, items, color="blue", lh=14, gap=6):
        for first, *rest in items:
            self.add(f'<circle cx="{x}" cy="{y - 4}" r="2.8" fill="{self.P[color]}"/>')
            self.text(x + 12, y, first, 11.8, 400)
            for r in rest:
                y += lh
                self.text(x + 12, y, r, 11.8, 400, "muted")
            y += lh + gap
        return y


# Glyphs are drawn centred on (0,0) inside a 34x34 tile.
GLYPHS = {
    "users": '<circle cx="-3" cy="-4" r="4.5"/><path d="M-11,9 C-11,2 5,2 5,9"/><circle cx="7" cy="-2" r="3.2"/>'
             '<path d="M6,4 C10,4 12,6 12,9"/>',
    "chat": '<path d="M-10,-8 h20 a2,2 0 0 1 2,2 v11 a2,2 0 0 1 -2,2 h-11 l-6,5 v-5 h-3 a2,2 0 0 1 -2,-2 v-11 '
            'a2,2 0 0 1 2,-2 z"/><path d="M-5,-1 h10"/>',
    "spark": '<path d="M0,-11 C1,-3 3,-1 11,0 C3,1 1,3 0,11 C-1,3 -3,1 -11,0 C-3,-1 -1,-3 0,-11 z"/>',
    "branch": '<circle cx="-6" cy="-8" r="2.8"/><circle cx="-6" cy="8" r="2.8"/><circle cx="7" cy="-3" r="2.8"/>'
              '<path d="M-6,-5 v10"/><path d="M7,0 C7,5 -6,3 -6,5"/>',
    "box": '<path d="M0,-10 L10,-5 L10,6 L0,11 L-10,6 L-10,-5 z"/><path d="M-10,-5 L0,0 L10,-5"/><path d="M0,0 v11"/>',
    "lb": '<circle cx="0" cy="-7" r="3.5"/><circle cx="-8" cy="8" r="3"/><circle cx="8" cy="8" r="3"/>'
          '<circle cx="0" cy="8" r="3"/><path d="M0,-3.5 V5 M-2,-4 L-7,5 M2,-4 L7,5"/>',
    "lock": '<rect x="-8" y="-2" width="16" height="12" rx="2"/><path d="M-5,-2 v-4 a5,5 0 0 1 10,0 v4"/>',
    "pulse": '<path d="M-12,1 h6 l3,-8 l5,15 l3,-7 h7"/>',
    "layers": '<path d="M0,-10 L11,-4 L0,2 L-11,-4 z"/><path d="M-11,1 L0,7 L11,1"/><path d="M-11,6 L0,12 L11,6"/>',
    "check": '<path d="M-10,-7 h4 M-10,0 h4 M-10,7 h4"/><path d="M-2,-7 h12 M-2,0 h12 M-2,7 h12"/>',
    "terminal": '<rect x="-11" y="-9" width="22" height="18" rx="2"/><path d="M-6,-3 l4,3 l-4,3 M1,4 h5"/>',
    "chart": '<path d="M-10,10 h20"/><path d="M-7,8 v-6 M-2,8 v-12 M3,8 v-8 M8,8 v-15"/>',
    "logs": '<path d="M-8,-11 h11 l6,6 v16 h-17 z"/><path d="M-4,-2 h9 M-4,3 h9 M-4,8 h6"/>',
    "trace": '<path d="M-11,-7 h12 M-6,-1 h12 M-2,5 h12 M-11,-10 v20"/>',
    "hub": '<circle cx="0" cy="0" r="3.5"/><circle cx="-9" cy="-7" r="2.2"/><circle cx="9" cy="-7" r="2.2"/>'
           '<circle cx="0" cy="10" r="2.2"/><path d="M-3,-2 L-7,-5 M3,-2 L7,-5 M0,3.5 V8"/>',
    "gauge": '<path d="M-11,5 a11,11 0 0 1 22,0"/><path d="M0,5 L6,-4"/><circle cx="0" cy="5" r="1.6"/>',
    "route": '<path d="M-11,0 h8 M-3,0 C2,0 2,-7 8,-7 M-3,0 C2,0 2,7 8,7"/><path d="M5,-10 l4,3 l-4,3 M5,4 l4,3 l-4,3"/>',
    "disk": '<ellipse cx="0" cy="-7" rx="9" ry="3.5"/><path d="M-9,-7 v14 a9,3.5 0 0 0 18,0 v-14"/>'
            '<path d="M-9,0 a9,3.5 0 0 0 18,0"/>',
    "plug": '<circle cx="0" cy="0" r="9"/><path d="M-9,0 h18 M0,-9 C-5,-4 -5,4 0,9 M0,-9 C5,-4 5,4 0,9"/>',
    "db": '<rect x="-10" y="-10" width="20" height="8" rx="2"/><rect x="-10" y="2" width="20" height="8" rx="2"/>'
          '<path d="M-6,-6 h1 M-6,6 h1"/>',
}


def draw(c: Canvas) -> None:
    P = c.P
    c.markers()
    c.add(f'<rect width="{W}" height="{H}" fill="{P["bg"]}"/>')
    c.add(f'<rect width="{W}" height="{H}" fill="url(#dots-{c.t})"/>')

    # ── header ──────────────────────────────────────────────────────────────
    c.text(48, 52, "Claude Observability Agent — High-Level Design (HLD)", 30, 700)
    c.text(48, 78, "AI-assisted SRE for Oracle Kubernetes Engine · CronJob every 5 min · detect → collect → "
                   "pre-pass → Claude tool-use analysis → Teams approval → remediate · a human approves every mutation",
           15.5, 400, "muted")
    c.text(1880, 52, "FHD 1920 × 1080", 14, 600, "subtle", "end")
    c.text(1880, 74, "v1 · theme-aware · © 2026 Ibn Abbas · MIT", 13.5, 400, "subtle", "end")
    c.line(48, 96, 1880, 96, "border", 1.5, op=0.9)

    # ── boundaries (drawn first so arrows and cards sit on top) ──────────────
    c.boundary(320, 340, 1120, 680, "red", "OCI region · us-ashburn-1")
    c.boundary(342, 458, 1076, 548, "orange", "OKE cluster · staging", off=30)
    c.boundary(392, 504, 1006, 176, "blue", "namespace: monitoring · claude-agent",
               "CronJob */5 · concurrencyPolicy Forbid · ServiceAccount claude-agent")
    c.boundary(392, 712, 1006, 136, "green", "namespace: observability · Helm releases")
    c.boundary(392, 880, 232, 112, "gray")
    c.boundary(642, 880, 756, 112, "purple", "monitored namespaces · apps-stg", right=True)

    # ── geometry shared by cards and arrows ─────────────────────────────────
    ax = [412 + i * 164 for i in range(6)]          # agent pipeline cards (w=148)
    ox = [412 + i * 197 for i in range(5)]          # observability cards  (w=180)
    CLAUDE_X, TEAMS_X, GH_X = ax[3] + 74, ax[4] + 74, ax[5] + 74
    ENG_X, REG_X, LB_X = 510, 720, 510

    # ── arrows: external ────────────────────────────────────────────────────
    c.arrow([(ENG_X, 258), (LB_X, 372)], "blue")
    c.label(ENG_X + 10, 300, "HTTPS 443 · Grafana", "blue", "start")
    c.arrow([(REG_X, 258), (REG_X, 456)], "green", dashed=True)
    c.label(REG_X + 10, 300, "image pull", "green", "start")
    c.arrow([(CLAUDE_X, 258), (CLAUDE_X, 552)], "orange", dashed=True, both=True)
    c.label(CLAUDE_X - 10, 300, "Messages API · tool use", "orange", "end")
    c.arrow([(TEAMS_X, 258), (TEAMS_X, 552)], "orange", dashed=True, both=True)
    c.label(TEAMS_X - 10, 300, "Graph · cards + replies", "orange", "end")
    c.arrow([(GH_X, 552), (GH_X, 258)], "orange", dashed=True)
    c.label(GH_X - 10, 300, "GitOps PR", "orange", "end")

    # ── arrows: internal ────────────────────────────────────────────────────
    for i in range(5):                                  # pipeline chain
        c.arrow([(ax[i] + 148, 597), (ax[i + 1] - 2, 597)], "gray", sw=1.6)
    c.arrow([(LB_X - 110, 404), (358, 404), (358, 936), (406, 936)], "blue")      # LB -> ingress-nginx
    c.label(353, 610, "HTTP :80 → ingress", "blue", "middle", size=11.5, rot=-90)
    c.arrow([(508, 904), (508, 834)], "blue")                                     # ingress -> stack
    c.label(500, 862, "host routing", "blue", "end", size=11.5)
    c.arrow([(ax[3] + 40, 645), (ax[3] + 40, 760)], "gray", both=False)             # analyzer -> stack
    c.label(ax[3] + 48, 700, "PromQL · LogQL · TraceQL (live tools)", "gray", "start", size=11.5)
    c.arrow([(ax[5] + 74, 647), (ax[5] + 74, 690), (1390, 690), (1390, 948), (1365, 948)], "purple")
    c.label(1408, 790, "approved kubectl", "purple", "middle", size=11.5, rot=-90)
    c.arrow([(ax[0] + 74, 647), (ax[0] + 74, 690), (600, 690), (600, 866), (633, 866), (633, 925), (662, 925)], "gray", both=True)
    c.label(592, 862, "K8s API", "gray", "end", size=11.5)
    c.arrow([(ox[3] + 90, 904), (ox[3] + 90, 834)], "teal")                       # apps -> otel
    c.label(ox[3] + 82, 864, "OTLP :4318", "teal", "end", size=11.5)
    c.arrow([(740, 904), (740, 834)], "gray")                                     # apps -> loki/prom
    c.label(748, 864, "logs · metrics", "gray", "start", size=11.5)

    # ── external actors ─────────────────────────────────────────────────────
    c.actor(ENG_X, 150, "blue", "users", "Engineers / on-call", "browser · Teams client")
    c.actor(REG_X, 150, "green", "box", "Container registry", "registry.example.com/platform")
    c.actor(CLAUDE_X, 150, "orange", "spark", "Anthropic Claude", "RCA + remediation plan")
    c.actor(TEAMS_X, 150, "purple", "chat", "Microsoft Teams", "alerts · @mention approve")
    c.actor(GH_X, 150, "gray", "branch", "GitHub", "Actions CI · chart PRs")

    # ── OCI row (outside the cluster) ───────────────────────────────────────
    c.card(400, 372, 222, 64, "blue", "lb", "Public load balancer", "stg-public-lb · TLS ends here")
    c.card(780, 372, 168, 64, "red", "lock", "OCI Bastion", "port-forward :6443")

    # ── agent pipeline ──────────────────────────────────────────────────────
    agent = [
        ("blue", "pulse", "Detect", "health · storage", "pods · PVC · svc", "ingress · node runtime"),
        ("blue", "layers", "Collect", "context + logs", "events · logs · timeline", "kubectl-logs fallback"),
        ("blue", "check", "Pre-pass", "deterministic", "OOM · rollout · PVC full", "known signatures"),
        ("orange", "spark", "Analyze", "Claude + tools", "agentic tool loop", "5 live tools · JSON out"),
        ("purple", "chat", "Notify", "Teams cards", "Adaptive Card thread", "polls @mention replies"),
        ("purple", "terminal", "Remediate", "policy executor", "allow-list · dry-run", "post-check verifies"),
    ]
    for x, (col, kind, title, sub, f1, f2) in zip(ax, agent):
        c.card(x, 548, 148, 98, col, kind, title, sub, f1, f2)
    c.label(ax[1] + 60, 670, "state → ConfigMaps claude-agent-cooldown · claude-agent-outcomes (last 50)", "muted",
            "start", 11.5, 400)

    # ── observability stack ─────────────────────────────────────────────────
    obs = [
        ("red", "chart", "Prometheus", "kube-prom-stack", "50 Gi PV · remote-write rx"),
        ("yellow", "logs", "Loki 3.x", "with Promtail", "TSDB v13 · compactor"),
        ("blue", "trace", "Tempo", "+ metrics-gen", "TraceQL · rate()"),
        ("teal", "hub", "OTel Collector", "OTLP :4317/:4318", "pipeline, not a datasource"),
        ("orange", "gauge", "Grafana", "dashboards", "Drilldown · Prom/Loki/Tempo"),
    ]
    for x, (col, kind, title, sub, f1) in zip(ox, obs):
        c.card(x, 756, 180, 76, col, kind, title, sub, f1)

    # ── edge + monitored workloads ──────────────────────────────────────────
    c.card(408, 906, 200, 70, "blue", "route", "ingress-nginx", "ns ingress-nginx", "*-stg.example.com hosts")
    apps = [("purple", "box", "Workloads", "Deploy · STS · Pods"), ("purple", "plug", "Services", "endpoints · ports"),
            ("purple", "route", "Ingress", "class · TLS · hosts"), ("purple", "disk", "PVCs", "oci-bv · usage")]
    for i, (col, kind, title, sub) in enumerate(apps):
        c.card(664 + i * 178, 906, 166, 70, col, kind, title, sub)

    # ── left column ─────────────────────────────────────────────────────────
    c.panel(48, 140, 250, 452, "At a glance")
    c.bullets(68, 190, [
        ("Runs as a CronJob every 5 min,", "no overlap, backoffLimit 0"),
        ("Cheap deterministic checks first;", "Claude only for unhealthy services"),
        ("Claude investigates live with", "PromQL, LogQL, Tempo, describe,", "events (agentic tool loop)"),
        ("Every mutating command needs a", "human @mention reply in Teams"),
        ("Tier-2 patches are server-side", "dry-run validated before applying"),
        ("Outcomes are stored; next prompt", "prefers fixes that worked"),
        ("Helm-owned drift → a PR to the", "chart values, not a live patch"),
        ("Per-service cooldown stops", "alert storms"),
        ("The agent has no ingress at all —", "outbound calls only"),
        ("Degrades gracefully: Loki down →", "kubectl logs fallback"),
    ])

    c.panel(48, 612, 250, 196, "Detection coverage", "checks per area")
    cov = [("Pods", 11, "blue"), ("Ingress", 10, "purple"), ("Services", 6, "green"), ("Storage", 6, "yellow"),
           ("Node runtime", 1, "red")]
    for i, (name, n, col) in enumerate(cov):
        y = 684 + i * 24
        c.text(66, y + 4, name, 11.8, 600)
        c.rect(158, y - 6, 110, 10, fill="bar", stroke="bar", sw=0, rx=5)
        c.rect(158, y - 6, 110 * n / 11, 10, fill=col, stroke=col, sw=0, rx=5)
        c.text(280, y + 4, str(n), 11.8, 600, "muted", "end")

    c.panel(48, 828, 250, 192, "Safety model", "read wide, write narrow")
    c.text(66, 900, "Tier 1 · runs once approved", 12.5, 700, "green")
    c.text(66, 917, "rollout restart · scale · delete pod", 11.5, 400, "muted")
    c.text(66, 942, "Tier 2 · dry-run, then apply", 12.5, 700, "orange")
    c.text(66, 959, "set image · patch deploy/svc/ingress", 11.5, 400, "muted")
    c.text(66, 984, "Anything else → not executed", 12.5, 700, "red")
    c.text(66, 1001, "read-only verbs only (get · logs · exec)", 11.5, 400, "muted")

    # ── right column: abbreviations + flow key ──────────────────────────────
    c.panel(1466, 120, 414, 900, "Abbreviations", "")
    c.text(1484, 168, "every acronym used in this diagram", 12, 400, "muted")
    groups = [
        ("PLATFORM", [("OCI", "Oracle Cloud Infrastructure"), ("OKE", "Oracle Kubernetes Engine"),
                      ("LB", "Load Balancer"), ("STS", "StatefulSet"), ("PVC / PV", "Persistent Volume (Claim)"),
                      ("oci-bv", "OCI Block Volume CSI StorageClass")]),
        ("OBSERVABILITY", [("OTLP", "OpenTelemetry Protocol"), ("PromQL", "Prometheus query language"),
                           ("LogQL", "Loki query language"), ("TraceQL", "Tempo query language"),
                           ("TSDB", "Time-Series Database (Loki index)"), ("DS", "Grafana data source")]),
        ("AGENT", [("RCA", "Root-Cause Analysis"), ("SA", "ServiceAccount (RBAC identity)"),
                   ("GitOps", "Fix lands as a PR to chart values"), ("Tier 1/2", "Remediation blast-radius policy")]),
        ("DOCUMENT", [("HLD", "High-Level Design"), ("FHD", "Full HD — 1920 × 1080 px")]),
    ]
    y = 200
    for g, rows in groups:
        c.text(1490, y, g, 12.5, 700, "muted")
        y += 24
        for k, v in rows:
            c.add(f'<rect x="1490" y="{y - 11}" width="3" height="14" rx="1.5" fill="{P["orange"]}"/>')
            c.text(1504, y, k, 13, 700)
            c.text(1594, y, v, 12.5, 400, "muted")
            y += 23
        y += 12
    c.text(1490, y + 4, "FLOW KEY", 12.5, 700, "muted")
    keys = [("blue", False, "Ingress — users into the cluster"), ("orange", True, "Egress — Claude, Teams, GitHub"),
            ("green", True, "Supply — image pulls"), ("teal", False, "Telemetry — OTLP traces"),
            ("purple", False, "Remediation — approved commands only"), ("gray", False, "Internal — reads / queries")]
    for i, (col, dashed, txt) in enumerate(keys):
        yy = y + 30 + i * 24
        c.arrow([(1500, yy - 4), (1556, yy - 4)], col, dashed=dashed)
        c.text(1580, yy, txt, 12.5, 400, "muted")

    # ── footer ──────────────────────────────────────────────────────────────
    c.line(48, 1032, 1880, 1032, "border", 1.2, op=0.9)
    c.text(48, 1052, "Loop: K8s API → detect → collect (events, Loki / kubectl logs, Tempo-correlated timeline) → "
                     "pre-pass hints → Claude (live PromQL / LogQL / TraceQL) → Teams card → @mention approval → "
                     "policy-checked kubectl → post-check → outcome stored", 12.5, 400, "muted")
    c.text(48, 1070, "Read cluster-wide, write narrow · nothing mutates without a human · secrets stay in the "
                     "cluster (claude-agent-secrets) · Helm-owned drift is fixed in Git, not in place", 12.5, 400,
           "subtle")


def build() -> str:
    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}" role="img" '
             f'aria-label="Claude Observability Agent high-level design on OCI OKE">',
             "<style>.theme-dark{display:none}@media (prefers-color-scheme: dark)"
             "{.theme-light{display:none}.theme-dark{display:inline}}</style>"]
    for t, cls in (("lt", "theme-light"), ("dk", "theme-dark")):
        p = THEMES[t]
        parts.append(f'<defs><pattern id="dots-{t}" width="32" height="32" patternUnits="userSpaceOnUse">'
                     f'<circle cx="1.2" cy="1.2" r="1.2" fill="{p["dot"]}"/></pattern></defs>')
        c = Canvas(t)
        draw(c)
        parts.append(f'<g class="{cls}">' + "\n".join(c.out + c.late) + "</g>")
    parts.append("</svg>")
    return "\n".join(parts)


if __name__ == "__main__":
    out = Path(__file__).with_name("architecture.svg")
    out.write_text(build(), encoding="utf-8")
    print(f"wrote {out}")
