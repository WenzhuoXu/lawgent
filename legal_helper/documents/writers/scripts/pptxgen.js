const fs = require("fs");
const pptxgen = require("pptxgenjs");

const input = fs.readFileSync(0, "utf8");
const payload = JSON.parse(input);

const pptx = new pptxgen();
// Language + East-Asian font are threaded from write_pptx_deck; a CJK deck
// gets a native face (e.g. 微软雅黑) instead of the Latin Aptos defaults.
const LANG = String(payload.lang || "en-US");
const EAST_ASIA_FONT = String(payload.font_face_east_asia || "");
const FONT_HEAD = EAST_ASIA_FONT || "Aptos Display";
const FONT_BODY = EAST_ASIA_FONT || "Aptos";
pptx.layout = "LAYOUT_WIDE";
pptx.author = "Legal Helper";
pptx.subject = payload.title || "Presentation";
pptx.title = payload.title || "Presentation";
pptx.company = "Legal Helper";
pptx.lang = LANG;
pptx.theme = {
  headFontFace: FONT_HEAD,
  bodyFontFace: FONT_BODY,
  lang: LANG,
};

const C = {
  ink: "17202A",
  muted: "5D6D7E",
  paper: "FFFFFF",
  soft: "F4F6F7",
  line: "D6DBDF",
};

const CHART_TYPE_MAP = {
  bar: pptx.ChartType.bar,
  line: pptx.ChartType.line,
  pie: pptx.ChartType.pie,
  doughnut: pptx.ChartType.doughnut,
  scatter: pptx.ChartType.scatter,
  bubble: pptx.ChartType.bubble,
  radar: pptx.ChartType.radar,
};

function cleanHex(value, fallback) {
  const raw = String(value || "").replace("#", "").trim();
  return /^[0-9a-fA-F]{6}$/.test(raw) ? raw.toUpperCase() : fallback;
}

function addTitle(slide, text, accent, opts = {}) {
  slide.addText(text || "", {
    x: opts.x ?? 0.55,
    y: opts.y ?? 0.38,
    w: opts.w ?? 12.2,
    h: opts.h ?? 0.55,
    fontFace: FONT_HEAD,
    fontSize: opts.size ?? 27,
    bold: true,
    color: opts.color ?? C.ink,
    margin: 0,
    fit: "shrink",
  });
  if (opts.rule !== false) {
    slide.addShape(pptx.ShapeType.rect, {
      x: opts.x ?? 0.55,
      y: (opts.y ?? 0.38) + 0.68,
      w: 0.9,
      h: 0.06,
      fill: { color: accent },
      line: { color: accent },
    });
  }
}

function addFooter(slide, text) {
  if (!text) return;
  slide.addText(text, {
    x: 0.55,
    y: 7.05,
    w: 12.2,
    h: 0.2,
    fontSize: 7,
    color: C.muted,
    margin: 0,
    fit: "shrink",
  });
}

function addBullets(slide, bullets, x, y, w, h, color = C.ink) {
  const items = (bullets || []).filter(Boolean).map((text) => ({
    text: String(text),
    options: { bullet: { type: "ul" }, breakLine: true },
  }));
  slide.addText(items.length ? items : "", {
    x,
    y,
    w,
    h,
    fontSize: 15,
    color,
    breakLine: false,
    margin: 0.08,
    paraSpaceAfterPt: 8,
    fit: "shrink",
  });
}

function addBodyText(slide, text, x, y, w, h, opts = {}) {
  slide.addText(text || "", {
    x,
    y,
    w,
    h,
    fontSize: opts.fontSize ?? 15,
    bold: opts.bold ?? false,
    italic: opts.italic ?? false,
    color: opts.color ?? C.ink,
    margin: 0.08,
    valign: opts.valign ?? "top",
    fit: "shrink",
  });
}

function addTable(slide, headers, rows, x, y, w, h, accent) {
  const data = [];
  if (headers && headers.length) {
    data.push(headers.map((v) => ({
      text: String(v ?? ""),
      options: { bold: true, color: "FFFFFF", fill: accent },
    })));
  }
  for (const row of rows || []) {
    data.push(row.map((v) => String(v ?? "")));
  }
  if (!data.length) return;
  slide.addTable(data, {
    x,
    y,
    w,
    h,
    border: { type: "solid", color: C.line, pt: 0.6 },
    fontFace: FONT_BODY,
    fontSize: 10,
    color: C.ink,
    margin: 0.06,
    valign: "mid",
  });
}

function addChart(slide, chart, accent, x, y, w, h) {
  if (!chart || !chart.type) return;
  const kind = CHART_TYPE_MAP[String(chart.type).toLowerCase()];
  if (!kind) return;
  const categories = (chart.categories || []).map(String);
  const data = (chart.series || []).map((s) => ({
    name: String(s.name || ""),
    labels: categories,
    values: (s.values || []).map((v) => (typeof v === "number" ? v : Number(v) || 0)),
  }));
  if (!data.length) return;
  slide.addChart(kind, data, {
    x: chart.x ?? x,
    y: chart.y ?? y,
    w: chart.w ?? w,
    h: chart.h ?? h,
    title: chart.title || "",
    showTitle: !!chart.title,
    chartColors: [accent, "70AD47", "FFC000", "5B9BD5", "C00000", "7030A0"],
    showLegend: data.length > 1,
    legendPos: "b",
    catAxisLabelColor: C.muted,
    valAxisLabelColor: C.muted,
    catAxisLabelFontSize: 9,
    valAxisLabelFontSize: 9,
    titleFontSize: 13,
    titleColor: C.ink,
  });
}

const FLOWCHART_SHAPE_MAP = {
  process: "flowChartProcess",
  decision: "flowChartDecision",
  terminator: "flowChartTerminator",
  io: "flowChartInputOutput",
  data: "flowChartManualInput",
  subprocess: "flowChartPredefinedProcess",
};

function addFlowchart(slide, fc, accent) {
  if (!fc || !Array.isArray(fc.nodes) || !fc.nodes.length) return;
  const nodes = fc.nodes;
  const edges = Array.isArray(fc.edges) ? fc.edges : [];

  const nodeFontSize = fc.font_size || 12;
  const nodeIndex = {};

  // First pass: draw the node shapes.
  for (const node of nodes) {
    if (!node || node.x === null || node.y === null || node.w == null || node.h == null) continue;
    const kind = String(node.kind || "process");
    const shapeName = FLOWCHART_SHAPE_MAP[kind] || FLOWCHART_SHAPE_MAP.process;
    const shape = pptx.ShapeType[shapeName] || pptx.ShapeType.flowChartProcess;

    const fill = cleanHex(node.fill, "");
    const stroke = cleanHex(node.stroke, "");

    // addText with `shape` option emits a single <p:sp> carrying both the
    // flowChart* prstGeom and the text frame. That way inspect_pptx_deck
    // can read node text directly off the shape (no orphan text box).
    slide.addText(String(node.text || node.id || ""), {
      shape,
      x: Number(node.x),
      y: Number(node.y),
      w: Number(node.w),
      h: Number(node.h),
      fill: { color: fill || accent, transparency: fill ? 0 : 70 },
      line: { color: stroke || accent, width: 1.5 },
      fontSize: nodeFontSize,
      color: C.ink,
      bold: kind === "terminator",
      align: "center",
      valign: "middle",
      margin: 0,
      fit: "shrink",
      wrap: true,
    });
    nodeIndex[node.id] = node;
  }

  // Second pass: draw edges as line shapes between node centers. Connector
  // endpoints don't snap to nodes when the user drags shapes in PowerPoint;
  // this is a known pptxgenjs limitation (see references/flowchart_authoring.md).
  for (const edge of edges) {
    if (!edge) continue;
    const from = nodeIndex[edge.from_id];
    const to = nodeIndex[edge.to_id];
    if (!from || !to) continue;
    const x1 = Number(from.x) + Number(from.w) / 2;
    const y1 = Number(from.y) + Number(from.h) / 2;
    const x2 = Number(to.x) + Number(to.w) / 2;
    const y2 = Number(to.y) + Number(to.h) / 2;
    const left = Math.min(x1, x2);
    const top = Math.min(y1, y2);
    const w = Math.max(Math.abs(x2 - x1), 0.01);
    const h = Math.max(Math.abs(y2 - y1), 0.01);
    // We can't draw the *direction* of a 1D line shape; pptxgenjs assigns the
    // arrow endpoint to (x+w, y+h). For TB / LR layouts that matches the
    // source->target direction; for backward edges the user sees the arrow at
    // the wrong end. Acceptable for v1 — the deck still reads as a flowchart.
    const arrow = String(edge.arrow || "end");
    const style = String(edge.style || "solid");
    slide.addShape(pptx.ShapeType.line, {
      x: left,
      y: top,
      w,
      h,
      line: {
        color: accent,
        width: 1.25,
        dashType: style === "dashed" ? "dash" : "solid",
        beginArrowType: arrow === "both" ? "triangle" : "none",
        endArrowType: arrow === "none" ? "none" : "triangle",
      },
      flipH: x2 < x1,
      flipV: y2 < y1,
    });
    if (edge.label) {
      slide.addText(String(edge.label), {
        x: left + w / 2 - 0.5,
        y: top + h / 2 - 0.18,
        w: 1.0,
        h: 0.32,
        fontSize: 9,
        color: C.muted,
        align: "center",
        valign: "middle",
        margin: 0,
        fit: "shrink",
      });
    }
  }
}

function addImages(slide, images) {
  for (const img of images || []) {
    if (!img || (!img.path && !img.data)) continue;
    const opts = {
      x: img.x ?? 0.85,
      y: img.y ?? 1.55,
      w: img.w ?? 5.0,
      h: img.h ?? 3.0,
    };
    if (img.sizing && img.sizing !== "stretch") {
      opts.sizing = { type: img.sizing, w: opts.w, h: opts.h };
    }
    if (img.data) opts.data = img.data;
    else opts.path = img.path;
    slide.addImage(opts);
  }
}

for (const master of payload.masters || []) {
  if (!master || !master.name) continue;
  const mAccent = cleanHex(master.accent_color, "1F4E79");
  const mBg = cleanHex(master.background_color, C.paper);
  const objects = [];
  if (master.footer) {
    objects.push({
      text: {
        text: String(master.footer),
        options: { x: 0.55, y: 7.05, w: 8.0, h: 0.2, fontSize: 7, color: C.muted, margin: 0, fit: "shrink" },
      },
    });
  }
  if (master.show_slide_number !== false) {
    objects.push({
      placeholder: {
        options: { name: "slideNum", type: "sldNum", x: 12.4, y: 7.05, w: 0.5, h: 0.2, fontSize: 7, color: C.muted, align: "right" },
        text: "",
      },
    });
  }
  pptx.defineSlideMaster({
    title: String(master.name),
    background: { color: mBg },
    objects: objects,
    slideNumber: { x: 12.4, y: 7.05, fontSize: 7, color: C.muted },
  });
}

function addStats(slide, stats, x, y, w, accent) {
  const clean = (stats || []).filter(Boolean).slice(0, 4);
  const gap = 0.18;
  const itemW = (w - gap * Math.max(0, clean.length - 1)) / Math.max(1, clean.length);
  clean.forEach((item, idx) => {
    const left = x + idx * (itemW + gap);
    slide.addShape(pptx.ShapeType.roundRect, {
      x: left,
      y,
      w: itemW,
      h: 1.4,
      rectRadius: 0.06,
      fill: { color: C.soft },
      line: { color: C.line, transparency: 30 },
    });
    slide.addText(String(item.value ?? item.label ?? ""), {
      x: left + 0.15,
      y: y + 0.18,
      w: itemW - 0.3,
      h: 0.45,
      fontSize: 25,
      bold: true,
      color: accent,
      margin: 0,
      fit: "shrink",
    });
    slide.addText(String(item.label ?? ""), {
      x: left + 0.15,
      y: y + 0.72,
      w: itemW - 0.3,
      h: 0.38,
      fontSize: 10,
      color: C.muted,
      margin: 0,
      fit: "shrink",
    });
  });
}

for (const [idx, item] of (payload.slides || []).entries()) {
  const slideOpts = item.master ? { masterName: String(item.master) } : undefined;
  const slide = slideOpts ? pptx.addSlide(slideOpts) : pptx.addSlide();
  const accent = cleanHex(item.accent_color || payload.accent_color, "1F4E79");
  const bg = cleanHex(item.background_color || payload.background_color, C.paper);
  const layout = String(item.layout || "bullets");
  if (!item.master) slide.background = { color: bg };

  if (layout === "title" || layout === "section") {
    slide.addShape(pptx.ShapeType.rect, { x: 0, y: 0, w: 13.333, h: 7.5, fill: { color: accent }, line: { color: accent } });
    addBodyText(slide, item.title || payload.title || `Slide ${idx + 1}`, 0.8, 2.35, 11.7, 0.7, { fontSize: 34, bold: true, color: "FFFFFF" });
    addBodyText(slide, item.subtitle || (item.bullets || []).join("\n"), 0.83, 3.2, 10.8, 1.4, { fontSize: 17, color: "FFFFFF" });
  } else if (layout === "chart") {
    addTitle(slide, item.title || `Slide ${idx + 1}`, accent);
    addChart(slide, item.chart, accent, 0.85, 1.55, 11.55, 4.95);
  } else {
    addTitle(slide, item.title || `Slide ${idx + 1}`, accent);
    if (layout === "two_column" || layout === "comparison") {
      slide.addShape(pptx.ShapeType.roundRect, { x: 0.65, y: 1.45, w: 5.8, h: 4.9, fill: { color: C.soft }, line: { color: C.line } });
      slide.addShape(pptx.ShapeType.roundRect, { x: 6.9, y: 1.45, w: 5.8, h: 4.9, fill: { color: C.soft }, line: { color: C.line } });
      addBodyText(slide, item.left_title || "A", 0.95, 1.72, 5.2, 0.35, { fontSize: 16, bold: true, color: accent });
      addBodyText(slide, item.right_title || "B", 7.2, 1.72, 5.2, 0.35, { fontSize: 16, bold: true, color: accent });
      addBullets(slide, item.left || [], 0.95, 2.25, 5.1, 3.65);
      addBullets(slide, item.right || [], 7.2, 2.25, 5.1, 3.65);
    } else if (layout === "table") {
      addTable(slide, item.table_headers || [], item.table_rows || [], 0.65, 1.42, 12.0, 4.9, accent);
    } else if (layout === "quote") {
      slide.addShape(pptx.ShapeType.rect, { x: 0.72, y: 1.55, w: 0.12, h: 4.4, fill: { color: accent }, line: { color: accent } });
      addBodyText(slide, item.subtitle || (item.bullets || []).join(" "), 1.05, 1.8, 10.8, 2.5, { fontSize: 25, italic: true });
      addBodyText(slide, item.footer || "", 1.05, 4.7, 8.0, 0.35, { fontSize: 12, color: C.muted });
    } else if (layout === "stat") {
      addStats(slide, item.stats || [], 0.7, 1.65, 11.95, accent);
      addBullets(slide, item.bullets || [], 0.95, 3.55, 11.2, 2.35);
    } else if (layout === "timeline") {
      const steps = (item.bullets || []).slice(0, 5);
      slide.addShape(pptx.ShapeType.line, { x: 1.0, y: 3.45, w: 11.0, h: 0, line: { color: accent, pt: 2 } });
      steps.forEach((step, stepIdx) => {
        const x = 1.0 + stepIdx * (11.0 / Math.max(1, steps.length - 1 || 1));
        slide.addShape(pptx.ShapeType.ellipse, { x: x - 0.18, y: 3.27, w: 0.36, h: 0.36, fill: { color: accent }, line: { color: accent } });
        addBodyText(slide, String(step), x - 0.85, 3.85, 1.7, 1.25, { fontSize: 10, color: C.ink });
      });
    } else {
      addBullets(slide, item.bullets || [], 0.85, 1.55, 11.55, 4.95);
      if (item.stats && item.stats.length) addStats(slide, item.stats, 0.85, 5.85, 11.55, accent);
    }
  }

  // Overlay: charts on non-"chart" layouts (e.g. bullets + chart on the right)
  // and images on every layout, regardless of base content.
  if (layout !== "chart" && item.chart) {
    addChart(slide, item.chart, accent, 6.9, 1.55, 5.8, 4.9);
  }
  if (item.flowchart) {
    addFlowchart(slide, item.flowchart, accent);
  }
  addImages(slide, item.images);

  addFooter(slide, item.footer || payload.footer || "");
  if (item.speaker_notes) slide.addNotes(String(item.speaker_notes));
}

pptx.writeFile({ fileName: payload.path });
