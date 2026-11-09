// Small helpers shared by the xdssat documentation pages.
//
//   import {withDownload, cellFeatures} from "./assets/dl.js"
//   withDownload(Plot.plot({...}), "file-name")
//
// withDownload wraps an Observable Plot chart (an <svg>, or a <figure> holding
// a legend and an <svg>) and adds "Download SVG" and "Download PNG" buttons.
// The legend, title and caption are folded into one standalone SVG, so the
// downloaded file looks like the chart on the page.

const NS = "http://www.w3.org/2000/svg";
const FONT = "system-ui, -apple-system, 'Segoe UI', Roboto, sans-serif";
const INK = "#222222";

function el(tag, attrs = {}, parent) {
  const e = document.createElementNS(NS, tag);
  for (const [k, v] of Object.entries(attrs)) e.setAttribute(k, v);
  if (parent) parent.appendChild(e);
  return e;
}

function svgSize(svg) {
  const w = parseFloat(svg.getAttribute("width"));
  const h = parseFloat(svg.getAttribute("height"));
  if (isFinite(w) && isFinite(h)) return [w, h];
  const r = svg.getBoundingClientRect();
  return [r.width || 640, r.height || 400];
}

// Plot's categorical legend is HTML: <span class="...-swatch"><svg fill=...>..</svg>label</span>
function swatchesBlock(node, maxWidth) {
  const items = [...node.querySelectorAll("[class$='-swatch']")];
  if (!items.length) return null;
  const g = el("g");
  let x = 0, y = 0;
  const lineH = 18;
  for (const s of items) {
    const sv = s.querySelector("svg");
    const shape = sv && sv.querySelector("rect, circle, path, line");
    let fill = (sv && sv.getAttribute("fill")) || "";
    if (!fill || fill === "currentColor") {
      const cs = shape ? getComputedStyle(shape) : null;
      fill = cs ? (cs.fill && cs.fill !== "none" ? cs.fill : cs.stroke) : "#888";
    }
    const label = s.textContent.trim();
    const w = 15 + 6 + label.length * 6.6 + 14;
    if (x + w > maxWidth && x > 0) { x = 0; y += lineH; }
    el("rect", {x, y, width: 15, height: 15, fill}, g);
    const t = el("text", {x: x + 21, y: y + 12, fill: INK, "font-size": 12, "font-family": FONT}, g);
    t.textContent = label;
    x += w;
  }
  return {g, h: y + lineH + 4};
}

function textBlock(text, size, weight) {
  const g = el("g");
  const t = el("text", {x: 0, y: size, fill: INK, "font-size": size, "font-weight": weight, "font-family": FONT}, g);
  t.textContent = text;
  return {g, h: size + 8};
}

// Fold a chart node into one standalone <svg> element.
export function toSVG(node) {
  if (node.tagName.toLowerCase() === "svg") {
    const c = node.cloneNode(true);
    const [w, h] = svgSize(node);
    const root = el("svg", {xmlns: NS, width: w, height: h, viewBox: `0 0 ${w} ${h}`});
    root.setAttribute("style", `color:${INK};font-family:${FONT};background:#fff`);
    el("rect", {width: w, height: h, fill: "#ffffff"}, root);
    c.setAttribute("x", 0); c.setAttribute("y", 0);
    root.appendChild(c);
    return {svg: root, w, h};
  }
  // a <figure> or <div>: stack its parts top to bottom
  const svgs = [...node.querySelectorAll("svg")].filter(s => !s.closest("[class$='-swatch']"));
  let width = Math.max(320, ...svgs.map(s => svgSize(s)[0]));
  const blocks = [];
  const walk = (n) => {
    for (const ch of n.children) {
      const tag = ch.tagName.toLowerCase();
      if (tag === "svg") {
        const [w, h] = svgSize(ch);
        const c = ch.cloneNode(true);
        c.setAttribute("width", w); c.setAttribute("height", h);
        blocks.push({node: c, h, svg: true});
      } else if (ch.querySelector && ch.querySelector("[class$='-swatch']") && /swatches/.test(ch.className)) {
        const b = swatchesBlock(ch, width);
        if (b) blocks.push({node: b.g, h: b.h});
      } else if (/^h[1-6]$/.test(tag)) {
        const b = textBlock(ch.textContent.trim(), 15, 600); blocks.push({node: b.g, h: b.h});
      } else if (tag === "figcaption") {
        const b = textBlock(ch.textContent.trim(), 11, 400); blocks.push({node: b.g, h: b.h});
      } else if (ch.children.length) {
        walk(ch);
      } else if (ch.textContent.trim()) {
        const b = textBlock(ch.textContent.trim(), 12, 400); blocks.push({node: b.g, h: b.h});
      }
    }
  };
  walk(node);
  const total = blocks.reduce((a, b) => a + b.h + 4, 0);
  const root = el("svg", {xmlns: NS, width, height: total, viewBox: `0 0 ${width} ${total}`});
  root.setAttribute("style", `color:${INK};font-family:${FONT};background:#fff`);
  el("rect", {width, height: total, fill: "#ffffff"}, root);
  let y = 0;
  for (const b of blocks) {
    if (b.svg) { b.node.setAttribute("x", 0); b.node.setAttribute("y", y); root.appendChild(b.node); }
    else { const g = el("g", {transform: `translate(0,${y})`}, root); g.appendChild(b.node); }
    y += b.h + 4;
  }
  return {svg: root, w: width, h: total};
}

function save(blob, filename) {
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  setTimeout(() => { URL.revokeObjectURL(a.href); a.remove(); }, 1000);
}

export function svgString(node) {
  const {svg} = toSVG(node);
  return '<?xml version="1.0" encoding="UTF-8"?>\n' + new XMLSerializer().serializeToString(svg);
}

export function downloadSVG(node, name) {
  save(new Blob([svgString(node)], {type: "image/svg+xml;charset=utf-8"}), `${name}.svg`);
}

export function downloadPNG(node, name, scale = 2) {
  const {svg, w, h} = toSVG(node);
  const str = new XMLSerializer().serializeToString(svg);
  const img = new Image();
  img.onload = () => {
    const c = document.createElement("canvas");
    c.width = Math.round(w * scale); c.height = Math.round(h * scale);
    const ctx = c.getContext("2d");
    ctx.fillStyle = "#ffffff"; ctx.fillRect(0, 0, c.width, c.height);
    ctx.drawImage(img, 0, 0, c.width, c.height);
    c.toBlob(b => save(b, `${name}.png`), "image/png");
  };
  img.onerror = (e) => console.error("PNG export failed", e);
  img.src = "data:image/svg+xml;charset=utf-8," + encodeURIComponent(str);
}

// Wrap a chart with the two download buttons. Returns a <div>.
export function withDownload(chart, name, note = "") {
  const wrap = document.createElement("div");
  wrap.className = "dl-wrap";
  wrap.dataset.name = name;           // read by chartImages() for batch export
  const card = document.createElement("div");
  card.className = "dl-chart";
  card.appendChild(chart);
  wrap.appendChild(card);
  const bar = document.createElement("div");
  bar.className = "dl-bar";
  for (const fmt of ["SVG", "PNG"]) {
    const b = document.createElement("button");
    b.type = "button";
    b.className = "btn btn-sm btn-outline-secondary";
    b.textContent = `Download ${fmt}`;
    b.dataset.format = fmt.toLowerCase();
    b.onclick = () => (fmt === "SVG" ? downloadSVG(chart, name) : downloadPNG(chart, name));
    bar.appendChild(b);
  }
  if (note) {
    const s = document.createElement("span");
    s.className = "dl-note";
    s.textContent = note;
    bar.appendChild(s);
  }
  wrap.appendChild(bar);
  return wrap;
}

// 0.1 degree AgERA5 cells as GeoJSON squares (clockwise rings, as d3-geo wants),
// carrying each row as properties. rows need lat and lon (cell centres).
export function cellFeatures(rows, half = 0.05) {
  return {
    type: "FeatureCollection",
    features: rows.map(r => ({
      type: "Feature",
      properties: r,
      geometry: {
        type: "Polygon",
        coordinates: [[
          [r.lon - half, r.lat + half], [r.lon + half, r.lat + half],
          [r.lon + half, r.lat - half], [r.lon - half, r.lat - half],
          [r.lon - half, r.lat + half]
        ]]
      }
    }))
  };
}

// Administrative boundaries drawn on every map, for location: thin admin2
// (districts) under bold admin1 (provinces), clipped to the map frame. Both come
// from java/data/{districts,provinces}.geojson (export_data.py). Spread into marks:
//   marks: [..., ...adminMarks(Plot, provinces, districtsGeo)]
export function adminMarks(Plot, provinces, districts, {labels = false} = {}) {
  const marks = [
    Plot.geo(districts, {stroke: "#555", strokeWidth: 0.3, strokeOpacity: 0.8, fill: "none", clip: true}),
    Plot.geo(provinces, {stroke: "#111", strokeWidth: 0.9, fill: "none", clip: true})
  ];
  if (labels) {
    marks.push(Plot.text(provinces.features, Plot.centroid({text: d => d.properties.name, fontSize: 9, fill: "#111",
      stroke: "white", strokeWidth: 3, clip: true})));
  }
  return marks;
}

// ---- Location maps of the case-study pages ------------------------------------------------
// Boundaries come from docs/scripts/export_boundaries.py: countries.geojson (country and
// neighbours), adm1.geojson / adm2.geojson (name, lon, lat label point), boundaries.json (box).

// A lon/lat rectangle as a GeoJSON polygon (clockwise ring, as d3-geo wants).
export function boxPolygon([w, s, e, n]) {
  return {type: "Feature", properties: {},
          geometry: {type: "Polygon", coordinates: [[[w, n], [e, n], [e, s], [w, s], [w, n]]]}};
}

// Country with its neighbours, the study box in red. info = boundaries.json.
export function locatorMap(Plot, countries, info, {width = 300, height = 300} = {}) {
  const [cw, cs, ce, cn] = info.country_bounds;
  const pad = 0.04 * Math.max(ce - cw, cn - cs);
  // Neighbour labels sit at the most interior point of the part shown in the frame
  // (export_boundaries.py); a neighbour that barely enters the frame has no label point.
  const own = countries.features.filter(d => d.properties.iso3 === info.iso3);
  const others = countries.features.filter(d => d.properties.iso3 !== info.iso3 && d.properties.lon != null);
  // the country's own name moves clear of the study box when its label point falls next to it
  const W = ce - cw + 2 * pad, H = cn - cs + 2 * pad;
  const bx = (info.box[0] + info.box[2]) / 2, by = (info.box[1] + info.box[3]) / 2;
  const near = own.some(d => Math.abs(d.properties.lon - bx) < 0.15 * W && Math.abs(d.properties.lat - by) < 0.1 * H);
  const ownDy = near ? (by > cs - pad + H / 2 ? 28 : -28) : 0;
  return Plot.plot({
    width, height, margin: 2,
    projection: {type: "equirectangular", domain: boxPolygon([cw - pad, cs - pad, ce + pad, cn + pad])},
    marks: [
      Plot.geo(countries, {fill: d => (d.properties.iso3 === info.iso3 ? "#efe3c8" : "#f4f4f4"),
                           stroke: "#8a8a8a", strokeWidth: 0.6, clip: true}),
      Plot.geo(boxPolygon(info.box), {fill: "#d0021b", fillOpacity: 0.3, stroke: "#d0021b", strokeWidth: 1.5}),
      Plot.dot([[(info.box[0] + info.box[2]) / 2, (info.box[1] + info.box[3]) / 2]],
               {x: d => d[0], y: d => d[1], r: 13, stroke: "#d0021b", strokeWidth: 1.2, fill: "none"}),
      Plot.text(others, {x: d => d.properties.lon, y: d => d.properties.lat, text: d => d.properties.name,
                         fontSize: 9, fill: "#888", clip: true, lineWidth: 8}),
      Plot.text(own, {x: d => d.properties.lon, y: d => d.properties.lat, text: d => d.properties.name,
                      fontSize: 13, fontWeight: 700, fill: "#222", dy: ownDy}),
      Plot.frame({stroke: "#bbb"})
    ]
  });
}

// Admin lines (level 2 thin, level 1 bold) and name labels, clipped to the map frame.
// labels: "adm1", "adm2" or null.
export function placeMarks(Plot, adm1, adm2, {labels = "adm1", fontSize = 10} = {}) {
  const marks = [
    Plot.geo(adm2, {stroke: "#666", strokeWidth: 0.4, strokeOpacity: 0.8, fill: "none", clip: true}),
    Plot.geo(adm1, {stroke: "#111", strokeWidth: 1.2, fill: "none", clip: true})
  ];
  const lab = labels === "adm1" ? adm1 : labels === "adm2" ? adm2 : null;
  if (lab) {
    marks.push(Plot.text(lab.features, {x: d => d.properties.lon, y: d => d.properties.lat,
      text: d => d.properties.name, fontSize, fill: "#111", stroke: "white", strokeWidth: 3, clip: true}));
  }
  return marks;
}

// Number formatting used in the prose.
export const fmt = {
  int: (v) => (v == null || !isFinite(v) ? "n/a" : Math.round(v).toLocaleString("en-US")),
  d1: (v) => (v == null || !isFinite(v) ? "n/a" : (+v).toFixed(1)),
  d2: (v) => (v == null || !isFinite(v) ? "n/a" : (+v).toFixed(2)),
  pct: (v) => (v == null || !isFinite(v) ? "n/a" : `${Math.round(v * 100)}%`),
  sign1: (v) => (v == null || !isFinite(v) ? "n/a" : `${v >= 0 ? "+" : ""}${(+v).toFixed(1)}`)
};

// Simple Pearson correlation
export function pearson(x, y) {
  const p = x.map((v, i) => [v, y[i]]).filter(([a, b]) => isFinite(a) && isFinite(b));
  const n = p.length;
  if (n < 3) return NaN;
  const mx = p.reduce((s, d) => s + d[0], 0) / n, my = p.reduce((s, d) => s + d[1], 0) / n;
  let sxy = 0, sxx = 0, syy = 0;
  for (const [a, b] of p) { sxy += (a - mx) * (b - my); sxx += (a - mx) ** 2; syy += (b - my) ** 2; }
  return sxy / Math.sqrt(sxx * syy);
}


// Every chart on the page as {name, svg, png}: the same SVG and 2x PNG as the download buttons.
// Used by docs/scripts/save_charts.py to keep backup copies in docs/assets/training/.
export async function chartImages(scale = 2) {
  const out = [];
  for (const wrap of document.querySelectorAll(".dl-wrap")) {
    const node = wrap.querySelector(".dl-chart").firstElementChild;
    if (!node) continue;
    const {svg, w, h} = toSVG(node);
    const str = new XMLSerializer().serializeToString(svg);
    const png = await new Promise((resolve) => {
      const img = new Image();
      img.onload = () => {
        const c = document.createElement("canvas");
        c.width = Math.round(w * scale); c.height = Math.round(h * scale);
        const ctx = c.getContext("2d");
        ctx.fillStyle = "#ffffff"; ctx.fillRect(0, 0, c.width, c.height);
        ctx.drawImage(img, 0, 0, c.width, c.height);
        resolve(c.toDataURL("image/png"));
      };
      img.onerror = () => resolve(null);
      img.src = "data:image/svg+xml;charset=utf-8," + encodeURIComponent(str);
    });
    out.push({name: wrap.dataset.name || `chart-${out.length + 1}`, svg: str, png});
  }
  return out;
}
