/**
 * HTML/CSS → PPTX with editable shapes.
 *
 * The fixed-layout writer (pptxgen.js) can only place content into ten
 * hard-coded arrangements, so anything the enum does not cover comes out as a
 * bullet list. This path instead lets the author use a real layout engine —
 * flex, grid, web fonts, gradients, SVG — renders it in headless Chromium,
 * reads the *computed* geometry back out, and maps it onto native PowerPoint
 * objects. Text stays text, boxes stay shapes, tables stay tables; only what
 * PowerPoint genuinely cannot express (SVG icons, gradients, canvas) is
 * rasterised, and only for the element that needs it.
 *
 * Input (stdin JSON):
 *   { path, html | html_path, title, width_in, height_in, raster_selectors }
 * Output (stdout JSON):
 *   { path, renderer, slides, shapes, texts, tables, images, warnings }
 *
 * Authoring contract: each slide is one `.slide` element sized exactly
 * width_in x height_in at 96 CSS px/inch. Mark anything that must be
 * rasterised with `data-raster` (or list a selector in raster_selectors).
 */

const fs = require("fs");
const path = require("path");
const pptxgen = require("pptxgenjs");

const PX_PER_IN = 96;
const PX_PER_PT = 96 / 72;

const payload = JSON.parse(fs.readFileSync(0, "utf8"));
const WIDTH_IN = Number(payload.width_in) || 13.333;
const HEIGHT_IN = Number(payload.height_in) || 7.5;
const RASTER_SELECTORS = Array.isArray(payload.raster_selectors) ? payload.raster_selectors : [];
const warnings = [];

const px2in = (v) => Math.round((v / PX_PER_IN) * 1000) / 1000;
const px2pt = (v) => Math.round((v / PX_PER_PT) * 10) / 10;

/** "rgb(31, 78, 121)" / "rgba(...)" → "1F4E79"; returns null when transparent. */
function cssColorToHex(value) {
  if (!value) return null;
  const m = String(value).match(/rgba?\(([^)]+)\)/);
  if (!m) return null;
  const parts = m[1].split(",").map((s) => parseFloat(s.trim()));
  if (parts.length >= 4 && parts[3] === 0) return null;
  const [r, g, b] = parts;
  if ([r, g, b].some((n) => Number.isNaN(n))) return null;
  return [r, g, b].map((n) => Math.max(0, Math.min(255, Math.round(n))).toString(16).padStart(2, "0")).join("").toUpperCase();
}

function cssAlphaOf(value) {
  const m = String(value || "").match(/rgba?\(([^)]+)\)/);
  if (!m) return 1;
  const parts = m[1].split(",").map((s) => parseFloat(s.trim()));
  return parts.length >= 4 ? parts[3] : 1;
}

/** First concrete family from a CSS font stack. */
function primaryFont(fontFamily) {
  const first = String(fontFamily || "").split(",")[0] || "";
  return first.replace(/["']/g, "").trim() || "Arial";
}

/**
 * Extract every renderable box from the page, in document order, in CSS px
 * relative to each slide's own origin. Runs inside the browser.
 */
async function extractSlides(page) {
  return page.evaluate((rasterSelectors) => {
    const out = [];
    const slides = Array.from(document.querySelectorAll(".slide"));

    const isHidden = (el, cs) =>
      cs.display === "none" || cs.visibility === "hidden" || parseFloat(cs.opacity || "1") === 0;

    const hasInlineOnlyChildren = (el) =>
      Array.from(el.childNodes).every((n) => {
        if (n.nodeType === Node.TEXT_NODE) return true;
        if (n.nodeType !== Node.ELEMENT_NODE) return true;
        const d = getComputedStyle(n).display;
        return d === "inline" || d === "inline-block" || d === "contents";
      });

    const directText = (el) =>
      Array.from(el.childNodes)
        .filter((n) => n.nodeType === Node.TEXT_NODE)
        .map((n) => n.textContent)
        .join("")
        .trim();

    // Split a text-leaf element into styled runs so bold/italic/colored spans
    // survive as real PowerPoint runs rather than one flat string.
    const runsOf = (el) => {
      const runs = [];
      const walk = (node, inherited) => {
        if (node.nodeType === Node.TEXT_NODE) {
          const t = node.textContent.replace(/\s+/g, " ");
          if (t.trim()) runs.push({ text: t, ...inherited });
          return;
        }
        if (node.nodeType !== Node.ELEMENT_NODE) return;
        const cs = getComputedStyle(node);
        if (isHidden(node, cs)) return;
        if (node.tagName === "BR") {
          runs.push({ text: "", breakLine: true, ...inherited });
          return;
        }
        const style = {
          bold: parseInt(cs.fontWeight, 10) >= 600,
          italic: cs.fontStyle === "italic",
          underline: (cs.textDecorationLine || "").includes("underline"),
          color: cs.color,
          fontSize: parseFloat(cs.fontSize),
          fontFamily: cs.fontFamily,
        };
        Array.from(node.childNodes).forEach((c) => walk(c, style));
      };
      const cs = getComputedStyle(el);
      walk(el, {
        bold: parseInt(cs.fontWeight, 10) >= 600,
        italic: cs.fontStyle === "italic",
        underline: (cs.textDecorationLine || "").includes("underline"),
        color: cs.color,
        fontSize: parseFloat(cs.fontSize),
        fontFamily: cs.fontFamily,
      });
      return runs;
    };

    slides.forEach((slide, slideIdx) => {
      const base = slide.getBoundingClientRect();
      const items = [];
      const rel = (r) => ({ x: r.left - base.left, y: r.top - base.top, w: r.width, h: r.height });
      let rasterSeq = 0;

      const visit = (el) => {
        const cs = getComputedStyle(el);
        if (isHidden(el, cs)) return;
        const r = el.getBoundingClientRect();
        if (r.width <= 0 || r.height <= 0) return;

        const forcedRaster =
          el.hasAttribute("data-raster") ||
          rasterSelectors.some((sel) => { try { return el.matches(sel); } catch (e) { return false; } });
        const tag = el.tagName;

        // Anything PowerPoint cannot express natively is captured as a picture
        // of exactly that element — never the whole slide.
        if (forcedRaster || tag === "SVG" || tag === "svg" || tag === "CANVAS" ||
            (cs.backgroundImage && cs.backgroundImage !== "none")) {
          el.setAttribute("data-raster-id", `s${slideIdx}-r${rasterSeq++}`);
          items.push({ kind: "raster", rasterId: el.getAttribute("data-raster-id"), ...rel(r) });
          return;
        }

        if (tag === "IMG") {
          items.push({ kind: "image", src: el.getAttribute("src") || "", ...rel(r) });
          return;
        }

        if (tag === "TABLE") {
          const rows = Array.from(el.rows).map((tr) =>
            Array.from(tr.cells).map((td) => {
              const tcs = getComputedStyle(td);
              return {
                text: td.innerText.trim(),
                bold: parseInt(tcs.fontWeight, 10) >= 600,
                color: tcs.color,
                fill: tcs.backgroundColor,
                align: tcs.textAlign,
                fontSize: parseFloat(tcs.fontSize),
                colspan: td.colSpan > 1 ? td.colSpan : undefined,
                rowspan: td.rowSpan > 1 ? td.rowSpan : undefined,
              };
            })
          );
          const firstCell = el.rows[0] && el.rows[0].cells[0];
          const bcs = getComputedStyle(firstCell || el);
          items.push({
            kind: "table", rows, ...rel(r),
            borderColor: bcs.borderTopColor,
            borderWidth: parseFloat(bcs.borderTopWidth) || 0,
            fontFamily: getComputedStyle(el).fontFamily,
          });
          return;
        }

        // A visible box (fill and/or border) becomes a real shape, so the user
        // can restyle it in PowerPoint instead of getting a flat picture.
        const bg = cs.backgroundColor;
        const bw = parseFloat(cs.borderTopWidth) || 0;
        const hasFill = bg && bg !== "rgba(0, 0, 0, 0)" && bg !== "transparent";
        const hasBorder = bw > 0 && cs.borderTopStyle !== "none";
        if ((hasFill || hasBorder) && el !== slide) {
          const radius = parseFloat(cs.borderTopLeftRadius) || 0;
          const round = radius >= Math.min(r.width, r.height) / 2 - 1;
          items.push({
            kind: "shape",
            shape: round && Math.abs(r.width - r.height) < 2 ? "ellipse" : (radius > 0 ? "roundRect" : "rect"),
            radius,
            fill: hasFill ? bg : null,
            line: hasBorder ? cs.borderTopColor : null,
            lineWidth: bw,
            dash: cs.borderTopStyle,
            ...rel(r),
          });
        }

        // Lists become bulleted text blocks rather than one string per <li>.
        if ((tag === "UL" || tag === "OL") && el.querySelector("li")) {
          const lis = Array.from(el.children).filter((c) => c.tagName === "LI");
          if (lis.length) {
            const lcs = getComputedStyle(lis[0]);
            items.push({
              kind: "bullets",
              ordered: tag === "OL",
              items: lis.map((li) => ({ runs: runsOf(li) })),
              fontSize: parseFloat(lcs.fontSize),
              fontFamily: lcs.fontFamily,
              color: lcs.color,
              align: lcs.textAlign,
              lineHeight: parseFloat(lcs.lineHeight) || parseFloat(lcs.fontSize) * 1.2,
              padLeft: parseFloat(cs.paddingLeft) || 0,
              ...rel(r),
            });
            return;
          }
        }

        if (directText(el) && hasInlineOnlyChildren(el)) {
          const flexMid = cs.display.includes("flex") && (cs.alignItems === "center");
          items.push({
            kind: "text",
            runs: runsOf(el),
            fontSize: parseFloat(cs.fontSize),
            fontFamily: cs.fontFamily,
            color: cs.color,
            align: cs.textAlign,
            valign: flexMid ? "middle" : "top",
            lineHeight: parseFloat(cs.lineHeight) || parseFloat(cs.fontSize) * 1.2,
            padding: {
              l: parseFloat(cs.paddingLeft) || 0,
              t: parseFloat(cs.paddingTop) || 0,
              r: parseFloat(cs.paddingRight) || 0,
              b: parseFloat(cs.paddingBottom) || 0,
            },
            ...rel(r),
          });
          return;
        }

        Array.from(el.children).forEach(visit);
      };

      Array.from(slide.children).forEach(visit);
      const scs = getComputedStyle(slide);
      out.push({
        background: scs.backgroundColor,
        notes: slide.getAttribute("data-notes") || "",
        items,
      });
    });
    return out;
  }, RASTER_SELECTORS);
}

/**
 * Prefer a system Chrome over Playwright's bundled build. The bundled
 * revision is pinned per Playwright version, so a routine `npm update` leaves
 * the package expecting a browser the cache does not have and every
 * conversion dies. The repo already auto-detects /usr/bin/google-chrome for
 * mermaid rendering; reuse it.
 */
function chromeExecutable() {
  const candidates = [
    process.env.LEGAL_HELPER_CHROME,
    "/usr/bin/google-chrome",
    "/usr/bin/google-chrome-stable",
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
  ].filter(Boolean);
  for (const c of candidates) {
    try { if (fs.existsSync(c)) return c; } catch (e) { /* keep looking */ }
  }
  return undefined;  // fall back to Playwright's own download
}

async function main() {
  const { chromium } = require("playwright");
  const executablePath = chromeExecutable();
  const launchOpts = { args: ["--no-sandbox", "--font-render-hinting=none"] };
  if (executablePath) launchOpts.executablePath = executablePath;
  let browser;
  try {
    browser = await chromium.launch(launchOpts);
  } catch (e) {
    if (!executablePath) throw e;
    warnings.push(`system chrome at ${executablePath} failed (${e.message}); using bundled build`);
    browser = await chromium.launch({ args: launchOpts.args });
  }
  const page = await browser.newPage({
    viewport: { width: Math.round(WIDTH_IN * PX_PER_IN), height: Math.round(HEIGHT_IN * PX_PER_IN) },
    deviceScaleFactor: 2,
  });

  if (payload.html_path) {
    await page.goto("file://" + path.resolve(payload.html_path), { waitUntil: "networkidle" });
  } else {
    await page.setContent(String(payload.html || ""), { waitUntil: "networkidle" });
  }
  try {
    await page.evaluate(() => document.fonts && document.fonts.ready);
  } catch (e) { /* fonts API unavailable — computed metrics are still valid */ }

  const slides = await extractSlides(page);
  if (!slides.length) {
    await browser.close();
    throw new Error("no .slide elements found — each slide must be an element with class=\"slide\"");
  }

  // Capture every rasterised element after extraction, so the ids assigned in
  // the page are still attached.
  const rasters = {};
  for (const slide of slides) {
    for (const item of slide.items) {
      if (item.kind !== "raster") continue;
      try {
        const handle = await page.$(`[data-raster-id="${item.rasterId}"]`);
        if (!handle) { warnings.push(`raster element ${item.rasterId} vanished`); continue; }
        const buf = await handle.screenshot({ omitBackground: true, type: "png" });
        rasters[item.rasterId] = "image/png;base64," + buf.toString("base64");
      } catch (e) {
        warnings.push(`raster ${item.rasterId}: ${e.message}`);
      }
    }
  }
  await browser.close();

  const pptx = new pptxgen();
  pptx.defineLayout({ name: "CUSTOM", width: WIDTH_IN, height: HEIGHT_IN });
  pptx.layout = "CUSTOM";
  pptx.title = payload.title || "Presentation";
  pptx.author = "Legal Helper";
  if (payload.lang) pptx.lang = payload.lang;

  const counts = { shapes: 0, texts: 0, tables: 0, images: 0 };

  for (const s of slides) {
    const slide = pptx.addSlide();
    const bgHex = cssColorToHex(s.background);
    if (bgHex) slide.background = { color: bgHex };

    for (const it of s.items) {
      const geo = { x: px2in(it.x), y: px2in(it.y), w: px2in(it.w), h: px2in(it.h) };

      if (it.kind === "shape") {
        const fillHex = cssColorToHex(it.fill);
        const lineHex = cssColorToHex(it.line);
        const opts = { ...geo };
        if (fillHex) {
          const a = cssAlphaOf(it.fill);
          opts.fill = { color: fillHex, transparency: Math.round((1 - a) * 100) };
        } else {
          opts.fill = { type: "none" };
        }
        opts.line = lineHex
          ? { color: lineHex, width: Math.max(0.25, it.lineWidth * 0.75),
              dashType: it.dash === "dashed" ? "dash" : it.dash === "dotted" ? "sysDot" : "solid" }
          : { type: "none" };
        if (it.shape === "roundRect") opts.rectRadius = Math.min(px2in(it.radius), Math.min(geo.w, geo.h) / 2);
        const shapeType = it.shape === "ellipse" ? pptx.ShapeType.ellipse
          : it.shape === "roundRect" ? pptx.ShapeType.roundRect : pptx.ShapeType.rect;
        slide.addShape(shapeType, opts);
        counts.shapes++;
        continue;
      }

      if (it.kind === "raster") {
        const data = rasters[it.rasterId];
        if (data) { slide.addImage({ ...geo, data }); counts.images++; }
        continue;
      }

      if (it.kind === "image") {
        const src = it.src || "";
        const opts = { ...geo };
        if (src.startsWith("data:")) opts.data = src.slice(5);
        else if (/^https?:/.test(src)) opts.path = src;
        else opts.path = payload.html_path ? path.resolve(path.dirname(payload.html_path), src) : src;
        try { slide.addImage(opts); counts.images++; }
        catch (e) { warnings.push(`image ${src}: ${e.message}`); }
        continue;
      }

      if (it.kind === "table") {
        const rows = it.rows.map((row) =>
          row.map((c) => ({
            text: c.text,
            options: {
              bold: c.bold,
              color: cssColorToHex(c.color) || "000000",
              fill: cssColorToHex(c.fill) || undefined,
              align: c.align === "center" ? "center" : c.align === "right" ? "right" : "left",
              fontSize: px2pt(c.fontSize),
              colspan: c.colspan,
              rowspan: c.rowspan,
              valign: "middle",
            },
          }))
        );
        if (rows.length) {
          slide.addTable(rows, {
            ...geo,
            fontFace: primaryFont(it.fontFamily),
            border: it.borderWidth > 0
              ? { type: "solid", color: cssColorToHex(it.borderColor) || "D6DBDF", pt: Math.max(0.25, it.borderWidth * 0.75) }
              : { type: "none" },
            autoPage: false,
          });
          counts.tables++;
        }
        continue;
      }

      const toRun = (r) => ({
        text: r.text,
        options: {
          bold: !!r.bold,
          italic: !!r.italic,
          underline: r.underline ? { style: "sng" } : undefined,
          color: cssColorToHex(r.color) || "000000",
          fontSize: px2pt(r.fontSize),
          fontFace: primaryFont(r.fontFamily),
          breakLine: !!r.breakLine,
        },
      });

      if (it.kind === "bullets") {
        const items = [];
        it.items.forEach((li, i) => {
          const runs = li.runs.length ? li.runs : [{ text: "", fontSize: it.fontSize, color: it.color, fontFamily: it.fontFamily }];
          runs.forEach((r, j) => {
            const run = toRun(r);
            if (j === 0) run.options.bullet = it.ordered ? { type: "number" } : true;
            run.options.breakLine = j === runs.length - 1 && i < it.items.length - 1;
            items.push(run);
          });
        });
        if (items.length) {
          slide.addText(items, {
            ...geo,
            align: it.align === "center" ? "center" : it.align === "right" ? "right" : "left",
            valign: "top",
            color: cssColorToHex(it.color) || "000000",
            fontSize: px2pt(it.fontSize),
            fontFace: primaryFont(it.fontFamily),
            lineSpacing: px2pt(it.lineHeight),
            margin: 0,
          });
          counts.texts++;
        }
        continue;
      }

      if (it.kind === "text") {
        const runs = it.runs.filter((r) => r.text !== "" || r.breakLine).map(toRun);
        if (!runs.length) continue;
        const pad = it.padding || { l: 0, t: 0, r: 0, b: 0 };
        const align = it.align === "center" ? "center" : it.align === "right" ? "right" : "left";

        // PowerPoint and LibreOffice lay text out with slightly different font
        // metrics than Chromium, so a box measured to the pixel re-wraps its
        // last word onto a second line. For text Chromium fit on ONE line,
        // forbid wrapping and add a little slack on the side the text grows
        // toward — the layout is unchanged, the spurious wrap disappears.
        const oneLine = it.h <= it.lineHeight * 1.6;
        const slack = oneLine ? Math.max(12, it.w * 0.08) : 0;
        const growLeft = align === "right" ? slack : align === "center" ? slack / 2 : 0;

        slide.addText(runs, {
          x: px2in(it.x + pad.l - growLeft),
          y: px2in(it.y + pad.t),
          w: Math.max(0.05, px2in(it.w - pad.l - pad.r + slack)),
          h: Math.max(0.05, px2in(it.h - pad.t - pad.b)),
          wrap: !oneLine,
          align,
          valign: it.valign || "top",
          color: cssColorToHex(it.color) || "000000",
          fontSize: px2pt(it.fontSize),
          fontFace: primaryFont(it.fontFamily),
          lineSpacing: px2pt(it.lineHeight),
          margin: 0,
        });
        counts.texts++;
      }
    }

    if (s.notes) slide.addNotes(s.notes);
  }

  await pptx.writeFile({ fileName: payload.path });
  process.stdout.write(JSON.stringify({
    path: payload.path, renderer: "html2pptx", slides: slides.length, ...counts, warnings,
  }));
}

main().catch((e) => {
  process.stderr.write(String((e && e.stack) || e));
  process.exit(1);
});
