// Citizenship — iPhone widget for the Scriptable app.
//
// Reads citizenship.json from Scriptable's iCloud folder, which flpt_watch.py
// writes hourly (burndown.write_widget) once that folder exists: the 2025-07
// cohort's --summarize estimates (sweep arrives, sweep done, done by
// then, if left half by) at the last 3 months' rates: each as a month, with
// how long from now that is in small text under it.
//
// Small, medium and lock-screen rectangular sizes.

const FILE = "citizenship.json";
const TEXT = Color.dynamic(new Color("#1C1C1E"), new Color("#F2F2F7"));
const DIM = Color.dynamic(new Color("#6C6C70"), new Color("#AEAEB2"));

async function load() {
  const fm = FileManager.iCloud();
  const path = fm.joinPath(fm.documentsDirectory(), FILE);
  if (!fm.fileExists(path)) return null;
  await fm.downloadFileFromiCloud(path);
  return JSON.parse(fm.readString(path));
}

function addText(stack, text, size, color, bold = false) {
  const t = stack.addText(text);
  t.font = bold ? Font.semiboldSystemFont(size) : Font.systemFont(size);
  t.textColor = color;
  t.lineLimit = 1;
  t.minimumScaleFactor = 0.6;
  return t;
}

// A one-line row of fixed height, so each kind of line (date, duration,
// label) sits at the same height in every column even when its text shrinks.
function line(stack, text, size, color, bold = false) {
  const cell = stack.addStack();
  cell.size = new Size(0, Math.ceil(size * 1.25));
  cell.centerAlignContent();
  addText(cell, text, size, color, bold);
}

function homeWidget(feed, family) {
  const w = new ListWidget();
  w.setPadding(12, 14, 12, 14);
  addText(w, feed.title, 20, TEXT, true);
  addText(w, `as of ${feed.period}`, 14, DIM);
  w.addSpacer();
  const row = w.addStack();
  row.topAlignContent();
  const big = family === "small" ? 22 : 36;
  feed.rows.forEach((r, i) => {
    if (i > 0) row.addSpacer();
    const s = row.addStack();
    s.layoutVertically();
    line(s, r.value, big, TEXT, true);
    line(s, r.detail || " ", 10, DIM);
    line(s, r.label, family === "small" ? 11 : 15, DIM);
  });
  w.refreshAfterDate = new Date(Date.now() + 6 * 3600e3);
  return w;
}

function lockWidget(feed) {
  const w = new ListWidget();
  addText(w, feed.title, 13, Color.white(), true);
  for (const r of feed.rows) {
    const extra = r.detail ? ` (${r.detail})` : "";
    addText(w, `${r.label}: ${r.value}${extra}`, 13, Color.white());
  }
  return w;
}

function message(text) {
  const w = new ListWidget();
  addText(w, text, 12, DIM);
  return w;
}

const family = config.widgetFamily || "medium";
let widget;
try {
  const feed = await load();
  if (!feed) widget = message(`No ${FILE} yet. flpt_watch writes it once Scriptable's iCloud folder exists.`);
  else if (family.startsWith("accessory")) widget = lockWidget(feed);
  else widget = homeWidget(feed, family);
} catch (e) {
  widget = message(`Citizenship: ${e}`);
}

if (config.runsInWidget) Script.setWidget(widget);
else await widget.presentMedium();
Script.complete();
