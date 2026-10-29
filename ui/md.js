// Markdown-ish rendering that keeps raw character offsets, so review items
// anchored by text quote can be highlighted inside the rendered document.
//
// parseBlocks(text, isMarkdown) -> blocks; every piece of visible text is a
// "run" {start, end} into the raw text. renderRun() splits a run at style,
// hidden-marker and highlight boundaries.

const RE = {
  fence: /^\s*(```|~~~)/,
  heading: /^(#{1,6})\s+/,
  hr: /^\s*([-*_])(\s*\1){2,}\s*$/,
  list: /^(\s*)([-*+]|\d+[.)])\s+/,
  quote: /^\s*>\s?/,
  tableRow: /^\s*\|/,
  tableSep: /^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$/,
};

function splitLines(text) {
  const lines = [];
  let i = 0;
  while (i <= text.length) {
    const j = text.indexOf("\n", i);
    const end = j === -1 ? text.length : j;
    lines.push({ start: i, end, s: text.slice(i, end) });
    if (j === -1) break;
    i = j + 1;
  }
  return lines;
}

function trimRun(text, start, end) {
  while (start < end && /\s/.test(text[start])) start++;
  while (end > start && /\s/.test(text[end - 1])) end--;
  return { start, end };
}

function tableCells(text, line) {
  const cells = [];
  const s = line.s;
  const off = line.start;
  const a = s.indexOf("|");
  let b = s.lastIndexOf("|");
  if (a === -1) return cells;
  if (b === a) b = s.length;
  let cur = a + 1;
  for (let k = a + 1; k <= b; k++) {
    if (k === b || (s[k] === "|" && s[k - 1] !== "\\")) {
      cells.push(trimRun(text, off + cur, off + k));
      cur = k + 1;
    }
  }
  return cells;
}

function startsBlock(lines, i) {
  const s = lines[i].s;
  return RE.fence.test(s) || RE.heading.test(s) || RE.list.test(s) || RE.quote.test(s) ||
    (RE.tableRow.test(s) && i + 1 < lines.length && RE.tableSep.test(lines[i + 1].s));
}

export function parseBlocks(text, isMarkdown = true) {
  const lines = splitLines(text);
  const blocks = [];
  if (!isMarkdown) {
    let i = 0;
    while (i < lines.length) {
      if (!lines[i].s.trim()) { i++; continue; }
      const a = i;
      while (i < lines.length && lines[i].s.trim()) i++;
      blocks.push({ type: "pre", start: lines[a].start, end: lines[i - 1].end,
        runs: [{ start: lines[a].start, end: lines[i - 1].end }] });
    }
    return blocks;
  }
  let i = 0;
  while (i < lines.length) {
    const L = lines[i];
    const s = L.s;
    if (!s.trim()) { i++; continue; }
    let m;
    if (RE.fence.test(s)) {
      const fence = s.trim().slice(0, 3);
      const a = i + 1;
      i++;
      while (i < lines.length && !lines[i].s.trim().startsWith(fence)) i++;
      const lastContent = Math.max(a, i) - 1;
      const run = a <= lastContent ? { start: lines[a].start, end: lines[lastContent].end } : { start: L.end, end: L.end };
      blocks.push({ type: "code", start: L.start, end: (lines[i] || lines[lines.length - 1]).end, runs: [run] });
      i++;
      continue;
    }
    if ((m = s.match(RE.heading))) {
      blocks.push({ type: "h", level: m[1].length, start: L.start, end: L.end,
        runs: [trimRun(text, L.start + m[0].length, L.end)] });
      i++;
      continue;
    }
    if (RE.hr.test(s)) { blocks.push({ type: "hr", start: L.start, end: L.end, runs: [] }); i++; continue; }
    if (RE.tableRow.test(s) && i + 1 < lines.length && RE.tableSep.test(lines[i + 1].s)) {
      const rows = [tableCells(text, L)];
      const a = i;
      i += 2;
      while (i < lines.length && RE.tableRow.test(lines[i].s)) { rows.push(tableCells(text, lines[i])); i++; }
      blocks.push({ type: "table", start: lines[a].start, end: lines[i - 1].end, rows, runs: rows.flat() });
      continue;
    }
    if (RE.list.test(s)) {
      const items = [];
      const a = i;
      while (i < lines.length && lines[i].s.trim()) {
        const lm = lines[i].s.match(RE.list);
        if (lm) {
          items.push({ indent: lm[1].length, ordered: /\d/.test(lm[2]),
            runs: [{ start: lines[i].start + lm[0].length, end: lines[i].end }] });
        } else if (items.length && /^\s/.test(lines[i].s)) {
          items[items.length - 1].runs.push(trimRun(text, lines[i].start, lines[i].end));
        } else break;
        i++;
      }
      blocks.push({ type: "list", ordered: items[0].ordered, start: lines[a].start, end: lines[i - 1].end,
        items, runs: items.flatMap((x) => x.runs) });
      continue;
    }
    if (RE.quote.test(s)) {
      const runs = [];
      const a = i;
      while (i < lines.length && RE.quote.test(lines[i].s)) {
        const qm = lines[i].s.match(RE.quote);
        runs.push({ start: lines[i].start + qm[0].length, end: lines[i].end });
        i++;
      }
      blocks.push({ type: "quote", start: lines[a].start, end: lines[i - 1].end, runs });
      continue;
    }
    const a = i;
    i++;
    while (i < lines.length && lines[i].s.trim() && !startsBlock(lines, i)) i++;
    blocks.push({ type: "p", start: lines[a].start, end: lines[i - 1].end,
      runs: [{ start: lines[a].start, end: lines[i - 1].end }] });
  }
  return blocks;
}

// ---------------------------------------------------------------- inline

// Returns {hidden: [[a,b]], styles: [{a,b,type}]} in offsets relative to s.
export function parseInline(s) {
  const hidden = [];
  const styles = [];
  const masked = s.split("");
  const mask = (a, b) => { for (let k = a; k < b; k++) masked[k] = "\u0000"; };
  let m;
  const code = /`([^`\n]+)`/g;
  while ((m = code.exec(s))) {
    hidden.push([m.index, m.index + 1], [m.index + m[0].length - 1, m.index + m[0].length]);
    styles.push({ a: m.index + 1, b: m.index + m[0].length - 1, type: "code" });
    mask(m.index, m.index + m[0].length);
  }
  let t = masked.join("");
  const link = /\[([^\]\n]+)\]\(([^)\s]+)\)/g;
  while ((m = link.exec(t))) {
    const close = m.index + 1 + m[1].length;
    hidden.push([m.index, m.index + 1], [close, m.index + m[0].length]);
    styles.push({ a: m.index + 1, b: close, type: "link" });
  }
  const bold = /\*\*(?=\S)([^\u0000]+?)(?<=\S)\*\*/g;
  while ((m = bold.exec(t))) {
    hidden.push([m.index, m.index + 2], [m.index + m[0].length - 2, m.index + m[0].length]);
    styles.push({ a: m.index + 2, b: m.index + m[0].length - 2, type: "strong" });
  }
  t = t.replace(/\*\*/g, "\u0000\u0000");
  const em = /(^|[^\w*])\*(?=\S)([^*\u0000\n]+?)(?<=\S)\*(?![\w*])/g;
  while ((m = em.exec(t))) {
    const a = m.index + m[1].length;
    hidden.push([a, a + 1], [m.index + m[0].length - 1, m.index + m[0].length]);
    styles.push({ a: a + 1, b: m.index + m[0].length - 1, type: "em" });
  }
  return { hidden, styles };
}

// Split a run into pieces: [{a, b, text, styles:Set, marks:[ids]}], absolute offsets.
// marks: [{id, start, end}] with absolute offsets.
export function runPieces(text, run, marks, inline = true) {
  const s = text.slice(run.start, run.end);
  const { hidden, styles } = inline ? parseInline(s) : { hidden: [], styles: [] };
  const cuts = new Set([0, s.length]);
  for (const [a, b] of hidden) { cuts.add(a); cuts.add(b); }
  for (const st of styles) { cuts.add(st.a); cuts.add(st.b); }
  const local = [];
  for (const mk of marks) {
    const a = Math.max(mk.start - run.start, 0);
    const b = Math.min(mk.end - run.start, s.length);
    if (a < b || (mk.start === mk.end && a === b && a >= 0 && a <= s.length)) {
      local.push({ ...mk, a, b });
      cuts.add(a);
      cuts.add(b);
    }
  }
  const sorted = [...cuts].filter((x) => x >= 0 && x <= s.length).sort((x, y) => x - y);
  const pieces = [];
  for (let k = 0; k + 1 < sorted.length; k++) {
    const a = sorted[k];
    const b = sorted[k + 1];
    if (hidden.some(([h1, h2]) => a >= h1 && b <= h2)) continue;
    pieces.push({
      a: a + run.start, b: b + run.start, text: s.slice(a, b),
      styles: styles.filter((st) => a >= st.a && b <= st.b).map((st) => st.type),
      marks: local.filter((mk) => a >= mk.a && b <= mk.b).map((mk) => mk.id),
      ends: local.filter((mk) => mk.b === b).map((mk) => mk.id),
    });
  }
  return pieces;
}

// ---------------------------------------------------------------- anchors

function normalizeWithMap(text) {
  let out = "";
  const map = [];
  let inWs = false;
  for (let i = 0; i < text.length; i++) {
    if (/\s/.test(text[i])) {
      if (!inWs) { out += " "; map.push(i); }
      inWs = true;
    } else {
      out += text[i];
      map.push(i);
      inWs = false;
    }
  }
  map.push(text.length);
  return { out, map };
}

function commonSuffixLen(a, b) {
  let n = 0;
  while (n < a.length && n < b.length && a[a.length - 1 - n] === b[b.length - 1 - n]) n++;
  return n;
}

function commonPrefixLen(a, b) {
  let n = 0;
  while (n < a.length && n < b.length && a[n] === b[n]) n++;
  return n;
}

// W3C TextQuoteSelector-style resolution: exact match, disambiguated by
// prefix/suffix, then by line_hint; falls back to whitespace-normalized search.
export function resolveAnchor(text, anchor) {
  if (!anchor || !anchor.exact) return null;
  const find = (hay, needle) => {
    const out = [];
    let i = hay.indexOf(needle);
    while (i !== -1) { out.push(i); i = hay.indexOf(needle, i + 1); }
    return out;
  };
  let cands = find(text, anchor.exact).map((i) => ({ start: i, end: i + anchor.exact.length }));
  let fuzzy = false;
  if (!cands.length) {
    const { out, map } = normalizeWithMap(text);
    const ex = anchor.exact.replace(/\s+/g, " ").trim();
    if (!ex) return null;
    cands = find(out, ex).map((k) => ({ start: map[k], end: map[k + ex.length - 1] + 1 }));
    fuzzy = true;
  }
  if (!cands.length) return null;
  let best = null;
  for (const c of cands) {
    const pre = commonSuffixLen(text.slice(Math.max(0, c.start - 64), c.start), anchor.prefix || "");
    const suf = commonPrefixLen(text.slice(c.end, c.end + 64), anchor.suffix || "");
    const dist = anchor.line_hint ? Math.abs(lineOf(text, c.start) - anchor.line_hint) : 0;
    const score = pre + suf - dist * 0.01;
    if (!best || score > best.score) best = { ...c, score };
  }
  return { start: best.start, end: best.end, ambiguous: cands.length > 1, fuzzy };
}

export function lineOf(text, offset) {
  return text.slice(0, offset).split("\n").length;
}
