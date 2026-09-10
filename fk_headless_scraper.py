#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
FK 无头浏览器定时抓取脚本
====================================
原理：
  1. 用 Playwright 启动无头 Chromium，逐个打开VT页面。
  2. 候选人数据（name / vote_percentage / rank）由 Next.js 服务端渲染，
     以 JSON 形式嵌在全局 self.__next_f 里。脚本在「浏览器内部」用 JS
     读取并解析（此时转义已还原，比在 Python 里正则原始 HTML 更稳）。
  3. 每条快照追加写入 CSV，并合并进 JSON 历史文件。
  4. 每次运行后重绘一个本地时间线 HTML（多线折线图，自带 Chart.js CDN）。
  5. 用 Windows 任务计划 / cron 每小时调用本脚本，即可「关电脑也记录」。

依赖：
  pip install playwright && playwright install chromium

用法：
  python fk_headless_scraper.py            # 抓一次并刷新图表
  python fk_headless_scraper.py --once     # 同上（等价于默认）
  python fk_headless_scraper.py --no-chart # 只抓取，不重绘 HTML
"""

import argparse
import csv
import json
import os
import sys
from datetime import datetime, timezone, timedelta

# ───────────────────────── 配置区 ─────────────────────────
CATEGORIES = {
    "best-actress": "https://feedxkhaosodawards.feedforfuture.co/categories/best-actress",
    "best-series": "https://feedxkhaosodawards.feedforfuture.co/categories/best-series",
}
# 页面标题里显示的友好名字（用于图表）
CATEGORY_LABELS = {
    "best-actress": "Best Actress (นักแสดงนำหญิง)",
    "best-series": "Best Series (ละครดราม่าทีวี)",
}

# 输出文件（与脚本同目录）
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CSV_PATH = os.path.join(BASE_DIR, "fk_vote_history.csv")
JSON_PATH = os.path.join(BASE_DIR, "fk_snapshots.json")
TIMELINE_HTML = os.path.join(BASE_DIR, "fk_timeline.html")
TABLE_HTML = os.path.join(BASE_DIR, "fk_data_table.html")

PAGE_TIMEOUT = 60000      # 页面加载超时(ms)
WAIT_TIMEOUT = 30000      # 等待数据出现超时(ms)

# 疑似付费VT自动检测开关（默认关闭）。
# 开启时：相邻快照「百分点差」≥ SUSPECT_PAID_THRESHOLD 的候选人，
# 在图表用红色菱形标记并列出事件。判定用相邻快照的实涨差（百分点），
# 不除以时间间隔，避免 cron 间隔偏差把 0.1% 的波动放大成疑似付费。
SUSPECT_PAID_ENABLED = True
SUSPECT_PAID_THRESHOLD = 0.2

# 在浏览器内部执行的提取逻辑：读取 self.__next_f 的 JSON
EXTRACT_JS = r"""
() => {
  const out = [];
  const seen = {};
  try {
    const chunks = [];
    if (self.__next_f) {
      for (const item of self.__next_f) {
        // 每个 push 是 [id, "string"] 或 [id, obj]
        if (item && typeof item[1] === 'string') chunks.push(item[1]);
      }
    }
    const big = chunks.join('');
    const re = /"rank":(\d+),"id":\d+,"code":(?:null|"[^"]*"),"name":"([^"]+)","image_url":"[^"]+","order":\d+,"vote_percentage":([\d.]+)/g;
    let m;
    while ((m = re.exec(big)) !== null) {
      const name = m[2];
      if (!seen[name]) {
        seen[name] = true;
        out.push({ rank: parseInt(m[1], 10), name: name, pct: parseFloat(m[3]) });
      }
    }
  } catch (e) { /* ignore */ }

  // DOM 兜底：series 等页面 self.__next_f 可能为空（纯客户端渲染），
  // 此时从每个「百分比」向上找最近的候选人名字，精准配对。
  if (out.length === 0) {
    const pctEls = [...document.querySelectorAll('span')]
      .filter(el => /^[\d.]+%$/.test(el.textContent.trim()));
    for (const el of pctEls) {
      const pct = parseFloat(el.textContent.trim().replace('%', ''));
      let node = el.parentElement;
      let name = null;
      for (let d = 0; d < 6 && node; d++) {
        const nm = node.querySelector && node.querySelector('span.font-bold, [class*="font-bold"]');
        if (nm && nm.textContent.trim()) { name = nm.textContent.trim(); break; }
        node = node.parentElement;
      }
      if (name && !seen[name]) { seen[name] = true; out.push({ rank: 0, name, pct }); }
    }
  }
  // 统一按百分比降序排定 rank（与页面展示顺序一致）
  out.sort((a, b) => b.pct - a.pct);
  out.forEach((o, i) => { o.rank = i + 1; });
  return out;
}
"""


def extract_category(page, url):
    """打开页面并提取候选人数据。"""
    from playwright.sync_api import sync_playwright  # 延迟导入，便于 --help 不报错
    page.goto(url, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT)
    # 等待数据出现：优先 self.__next_f 含 vote_percentage；
    # 否则接受「DOM 中已渲染出百分比」（series 等纯客户端渲染页）。
    page.wait_for_function(
        r"""() => {
          try {
            if (self.__next_f && self.__next_f.some(i => i && typeof i[1] === 'string' && i[1].includes('vote_percentage'))) return true;
          } catch(e) {}
          return Array.from(document.querySelectorAll('span')).some(el => /^\d+(\.\d+)?%$/.test(el.textContent.trim()));
        }""",
        timeout=WAIT_TIMEOUT,
    )
    return page.evaluate(EXTRACT_JS)


def load_history():
    if os.path.exists(JSON_PATH):
        try:
            with open(JSON_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {cat: [] for cat in CATEGORIES}


def save_history(history):
    with open(JSON_PATH, "w", encoding="utf-8") as f:
        json.dump(history, f, ensure_ascii=False, indent=2)


def append_csv(rows):
    write_header = not os.path.exists(CSV_PATH)
    with open(CSV_PATH, "a", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        if write_header:
            w.writerow(["timestamp_iso", "timestamp_bkk", "category", "candidate", "rank", "vote_percentage"])
        for r in rows:
            w.writerow(r)


def detect_events(history, threshold=0.2):
    """逐奖项、逐候选人比较相邻快照：相邻百分点差 ≥ threshold 视为疑似付费VT。"""
    events = {cat: [] for cat in history}
    for cat, snaps in history.items():
        ordered = sorted(snaps, key=lambda s: s["t"])
        for i in range(1, len(ordered)):
            prev_map = {c["name"]: c["pct"] for c in ordered[i - 1]["candidates"]}
            t_prev, t_cur = ordered[i - 1]["t"], ordered[i]["t"]
            try:
                dt = (datetime.fromisoformat(t_cur) - datetime.fromisoformat(t_prev)).total_seconds() / 3600.0
            except Exception:
                dt = 0.0
            if dt <= 0:
                continue
            for c in ordered[i]["candidates"]:
                name = c["name"]
                if name in prev_map:
                    delta = c["pct"] - prev_map[name]
                    rate = delta / dt
                    # 判定用相邻快照的实涨差（百分点），不依赖 dt，避免间隔偏差放大误判
                    if delta >= threshold:
                        events[cat].append({
                            "t": t_cur, "name": name,
                            "delta": round(delta, 2), "rate": round(rate, 3),
                            "pct": c["pct"],
                        })
    return events


def _fmt_bkk(iso_str):
    """Convert ISO timestamp to 'YYYY-MM-DD HH:MM:SS' in Asia/Bangkok (UTC+7)."""
    try:
        dt = datetime.fromisoformat(iso_str)
    except Exception:
        return iso_str or ''
    # If ISO has no tzinfo, assume it's already UTC+7 (the source feed is in BKK)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone(timedelta(hours=7)))
    bkk = dt.astimezone(timezone(timedelta(hours=7)))
    return bkk.strftime("%Y-%m-%d %H:%M:%S")


def _with_bkk_labels(history):
    """Add a `t_bkk` field to every snapshot, so JS can display BKK time without TZ math."""
    out = {}
    for cat, snaps in history.items():
        out[cat] = [{"t": s.get("t"), "t_bkk": _fmt_bkk(s.get("t")), "candidates": s.get("candidates", [])} for s in snaps]
    return out


def generate_timeline(history, threshold=0.2):
    """根据 JSON 历史生成多线时间线 HTML（Chart.js CDN）。"""
    data_js = json.dumps(_with_bkk_labels(history), ensure_ascii=False)
    if SUSPECT_PAID_ENABLED:
        events = detect_events(history, threshold)
        # Decorate each event with a BKK-formatted label so the chart displays BKK time
        for cat, evs in events.items():
            for e in evs:
                e["t_bkk"] = _fmt_bkk(e["t"])
    else:
        events = {cat: [] for cat in history}
    events_js = json.dumps(events, ensure_ascii=False)
    subtitle = '  ·  red diamond = suspected paid VT' if SUSPECT_PAID_ENABLED else ''
    labels_meta = {cat: CATEGORY_LABELS.get(cat, cat) for cat in CATEGORIES}
    html = (TEMPLATE
            .replace("/*__DATA__*/", data_js)
            .replace("/*__META__*/", json.dumps(labels_meta, ensure_ascii=False))
            .replace("/*__EVENTS__*/", events_js)
            .replace("/*__SUBTITLE__*/", subtitle))
    with open(TIMELINE_HTML, "w", encoding="utf-8") as f:
        f.write(html)
    if SUSPECT_PAID_ENABLED:
        total = sum(len(v) for v in events.values())
        if total:
            print(f"\n⚠ 检测到 {total} 起疑似付费VT事件（单小时涨幅≥{threshold}%）：")
            for cat, evs in events.items():
                for e in evs:
                    print(f"   [{cat}] {e['t']}  {e['name']}  +{e['delta']}%  (≈{e['rate']}%/h)")
        else:
            print(f"\n未检测到疑似付费VT事件（单小时涨幅≥{threshold}%）。")
    else:
        print("\n疑似付费VT自动检测已关闭（仅记录与绘图）。")
    return TIMELINE_HTML


def generate_data_table():
    """读取 CSV 完整数据，生成可排序/筛选/复制为 TSV 的表格页 HTML。"""
    if not os.path.exists(CSV_PATH):
        csv_text = ""
    else:
        # utf-8-sig 会自动剥离 BOM；newline 保持原样（csv 模块会写 \r\n）
        with open(CSV_PATH, "r", encoding="utf-8-sig") as f:
            csv_text = f.read()
    labels_meta = {cat: CATEGORY_LABELS.get(cat, cat) for cat in CATEGORIES}
    html = (TABLE_TEMPLATE
            .replace("/*__CSV__*/", json.dumps(csv_text))
            .replace("/*__META__*/", json.dumps(labels_meta, ensure_ascii=False)))
    with open(TABLE_HTML, "w", encoding="utf-8") as f:
        f.write(html)
    return TABLE_HTML


# 时间线 HTML 模板
TEMPLATE = r"""<!DOCTYPE html>
<html lang="th">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>FK · VT Timeline</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>
<style>
  body { font-family: system-ui, "Segoe UI", sans-serif; margin: 0; background:#0f1115; color:#e8e8e8; }
  .nav { display:flex; gap:4px; padding:10px 16px; border-bottom:1px solid #2a2d34; background:#0a0c10; }
  .nav a { color:#9aa0a6; text-decoration:none; padding:5px 12px; border-radius:6px; font-size:12px; transition:all .15s; }
  .nav a:hover { background:#1f242d; color:#e8e8e8; }
  .nav a.active { background:#171a21; color:#e8e8e8; border:1px solid #2a2d34; }
  header { padding: 16px 20px; border-bottom: 1px solid #2a2d34; }
  header h1 { margin:0; font-size:18px; }
  header p { margin:4px 0 0; color:#9aa0a6; font-size:12px; }
  .grid { display:grid; grid-template-columns: 1fr; gap: 18px; padding: 18px 20px; }
  .card { background:#171a21; border:1px solid #2a2d34; border-radius:12px; padding:14px; }
  .card h2 { margin:0 0 10px; font-size:15px; }
  .chart-wrap { position:relative; height:360px; }
  table { width:100%; border-collapse:collapse; margin-top:10px; font-size:12px; }
  th,td { text-align:left; padding:4px 6px; border-bottom:1px solid #23262d; }
  th { color:#9aa0a6; font-weight:600; }
  .up { color:#4ade80; } .down { color:#f87171; } .flat { color:#9aa0a6; }
</style>
</head>
<body>
<nav class="nav">
  <a href="./index.html" class="active">📈 Chart</a>
  <a href="./data.html">📋 Data Table</a>
  <a href="./fk_vote_history.csv" download>⬇ Download CSV</a>
</nav>
<header>
  <h1>FK · VT Vote % Timeline <span style="font-size:13px;color:#9aa0a6;font-weight:400;">(BKK, UTC+7)</span></h1>
  <p id="updated"></p>
</header>
<div class="grid" id="grid"></div>
<script>
const DATA = /*__DATA__*/;
const META = /*__META__*/;
const EVENTS = /*__EVENTS__*/;
const COLORS = ['#FFCE38','#3b82f6','#ef4444','#22c55e','#a855f7','#ec4899','#14b8a6','#f97316'];
const grid = document.getElementById('grid');

function fmtTime(iso){ return iso || ''; }   // BKK label is pre-formatted server-side; pass through

// Event lookup table: cat -> (t + '|' + name) -> event
const evMap = {};
for (const cat of Object.keys(EVENTS)){
  evMap[cat] = {};
  for (const e of EVENTS[cat]) evMap[cat][e.t + '|' + e.name] = e;
}

let latest = '';
for (const cat of Object.keys(DATA)) {
  const snaps = DATA[cat];
  if (!snaps.length) continue;
  // labels = BKK-formatted strings (consistent with data table); keys = ISO (for lookup)
  latest = snaps[snaps.length-1].t_bkk;
  const times   = snaps.map(s=>s.t_bkk);
  const timesIso = snaps.map(s=>s.t);
  const candSet = {};
  snaps.forEach(s=>s.candidates.forEach(c=>candSet[c.name]=true));
  const cands = Object.keys(candSet);
  const series = cands.map((name, i)=>{
    const vals=[], radii=[], styles=[], pcolors=[];
    timesIso.forEach(t=>{
      const snap = snaps.find(s=>s.t===t);
      const c = snap ? snap.candidates.find(x=>x.name===name) : null;
      const ev = evMap[cat][t + '|' + name];
      if (c){
        vals.push(c.pct);
        if (ev){ radii.push(7); styles.push('rectRot'); pcolors.push('#FF3B30'); }
        else  { radii.push(3); styles.push('circle');  pcolors.push(COLORS[i%COLORS.length]); }
      } else {
        vals.push(null); radii.push(0); styles.push('circle'); pcolors.push(COLORS[i%COLORS.length]);
      }
    });
    return {
      label: name, data: vals,
      borderColor: COLORS[i%COLORS.length],
      backgroundColor: COLORS[i%COLORS.length],
      tension: 0.25, borderWidth: 2, spanGaps: true,
      pointRadius: radii, pointStyle: styles,
      pointBackgroundColor: pcolors, pointBorderColor: pcolors
    };
  });
  const card = document.createElement('div'); card.className='card';
  const h2 = document.createElement('h2');
  h2.textContent = (META[cat] || cat) + '/*__SUBTITLE__*/';
  card.appendChild(h2);
  const wrap = document.createElement('div'); wrap.className='chart-wrap';
  const cv = document.createElement('canvas'); wrap.appendChild(cv); card.appendChild(wrap);
  grid.appendChild(card);
  new Chart(cv, {
    type:'line',
    data:{ labels: times.map(fmtTime), datasets: series },
    options:{
      responsive:true, maintainAspectRatio:false,
      interaction:{ mode:'index', intersect:false },
      plugins:{
        legend:{ labels:{ color:'#cbd5e1', boxWidth:12, font:{size:11} } },
        tooltip:{ callbacks:{ label:(c)=>{
          let s = `${c.dataset.label}: ${c.parsed.y}%`;
          const ev = evMap[cat][ timesIso[c.dataIndex] + '|' + c.dataset.label ];
          if (ev) s += '  ▲ Suspected paid VT +' + ev.delta + '%';
          return s;
        }}}
      },
      scales:{
        x:{ ticks:{ color:'#9aa0a6', maxRotation:45, minRotation:45, font:{size:10}, title:{display:true,text:'time (BKK, UTC+7)',color:'#9aa0a6',font:{size:10}} }, grid:{color:'#23262d'} },
        y:{ ticks:{ color:'#9aa0a6', callback:v=>v+'%' }, grid:{color:'#23262d'}, title:{display:true,text:'vote %',color:'#9aa0a6'} }
      }
    }
  });
  // Suspected paid VT event list
  if (EVENTS[cat] && EVENTS[cat].length){
    const ul = document.createElement('ul');
    ul.style.cssText='margin:10px 0 0;padding-left:18px;font-size:12px;color:#fca5a5;line-height:1.7;';
    EVENTS[cat].forEach(e=>{
      const li=document.createElement('li');
      li.textContent = `${fmtTime(e.t)} · ${e.name}  ▲ +${e.delta}%  (≈${e.rate}%/h)  · Suspected paid VT participation`;
      ul.appendChild(li);
    });
    card.appendChild(ul);
  }
}
const evTotal = Object.values(EVENTS).reduce((a,b)=>a+b.length,0);
document.getElementById('updated').textContent = 'Last updated: ' + (latest ? fmtTime(latest) : '—') + '  |  ' + Object.keys(DATA).length + ' categories  |  ' + evTotal + ' suspected paid VT events';
</script>
</body>
</html>
"""


# 数据表 HTML 模板（内嵌 CSV，页面打开即看；带筛选 / 排序 / 复制为 TSV / 下载）
TABLE_TEMPLATE = r"""<!DOCTYPE html>
<html lang="th">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>FK · VT Data Table</title>
<style>
  :root { --bg:#0f1115; --card:#171a21; --border:#2a2d34; --text:#e8e8e8; --muted:#9aa0a6; --accent:#3b82f6; --hover:#1f242d; }
  * { box-sizing: border-box; }
  body { font-family: system-ui, "Segoe UI", sans-serif; margin: 0; background: var(--bg); color: var(--text); }
  .nav { display:flex; gap:4px; padding:10px 16px; border-bottom:1px solid var(--border); background:#0a0c10; }
  .nav a { color:var(--muted); text-decoration:none; padding:5px 12px; border-radius:6px; font-size:12px; transition:all .15s; }
  .nav a:hover { background:var(--hover); color:var(--text); }
  .nav a.active { background:var(--card); color:var(--text); border:1px solid var(--border); }
  header { padding: 16px 20px; border-bottom: 1px solid var(--border); }
  header h1 { margin:0; font-size:18px; }
  header p { margin:4px 0 0; color:var(--muted); font-size:12px; }
  .container { padding: 16px 20px 24px; }
  .stats { display:flex; gap:18px; flex-wrap:wrap; padding:12px 16px; background:var(--card); border:1px solid var(--border); border-radius:10px; font-size:12px; margin-bottom:12px; }
  .stats div { color:var(--muted); }
  .stats span { color:var(--text); font-weight:600; margin-left:4px; }
  .toolbar { display:flex; gap:8px; flex-wrap:wrap; align-items:center; margin-bottom:12px; }
  .toolbar input, .toolbar select, .toolbar button {
    background:var(--card); color:var(--text); border:1px solid var(--border);
    border-radius:6px; padding:7px 11px; font-size:13px; font-family:inherit;
  }
  .toolbar input:focus, .toolbar select:focus { outline:1px solid var(--accent); }
  .toolbar input { flex:1; min-width:180px; }
  .toolbar button { cursor:pointer; transition:all .15s; }
  .toolbar button:hover { background:var(--hover); border-color:var(--accent); }
  .table-wrap { background:var(--card); border:1px solid var(--border); border-radius:10px; overflow:auto; max-height:70vh; }
  table { width:100%; border-collapse:collapse; font-size:12px; }
  th, td { text-align:left; padding:8px 12px; border-bottom:1px solid #23262d; white-space:nowrap; }
  th { background:#1a1d24; color:var(--muted); font-weight:600; position:sticky; top:0; cursor:pointer; user-select:none; z-index:1; }
  th:hover { color:var(--text); }
  th .arrow { color:var(--accent); margin-left:4px; font-size:11px; }
  tbody tr:hover { background:var(--hover); }
  .pct { font-variant-numeric: tabular-nums; text-align:right; }
  .rank { text-align:right; font-variant-numeric: tabular-nums; }
  .badge { display:inline-block; padding:2px 9px; border-radius:999px; font-size:11px; font-weight:500; }
  .badge-actress { background:#4a1d3e; color:#f9a8d4; }
  .badge-series { background:#1e3a5f; color:#93c5fd; }
  .delta-up { color:#4ade80; font-weight:600; }
  .delta-down { color:#f87171; font-weight:600; }
  .delta-flat { color:#9aa0a6; }
  .delta-empty { color:#5a5e66; }
  .toast { position:fixed; bottom:24px; right:24px; background:var(--accent); color:#fff; padding:10px 18px; border-radius:6px; font-size:13px; opacity:0; transition:opacity .2s; pointer-events:none; z-index:100; }
  .toast.show { opacity:1; }
  .empty { padding:40px; text-align:center; color:var(--muted); }
  footer { padding:14px 20px; color:var(--muted); font-size:11px; border-top:1px solid var(--border); margin-top:8px; }
</style>
</head>
<body>

<nav class="nav">
  <a href="./index.html">📈 Chart</a>
  <a href="./data.html" class="active">📋 Data Table</a>
  <a href="./fk_vote_history.csv" download>⬇ Download CSV</a>
</nav>

<header>
  <h1>FK · VT Full Data Table</h1>
  <p id="updated"></p>
</header>

<div class="container">
  <div class="stats" id="stats"></div>

  <div class="toolbar">
    <input type="text" id="search" placeholder="🔍 Search candidate or category..." autocomplete="off"/>
    <select id="category">
      <option value="">All Categories</option>
      <option value="best-actress">Best Actress</option>
      <option value="best-series">Best Series</option>
    </select>
    <button id="copyBtn" title="Copy as Tab-separated, paste directly into Excel">📋 Copy as TSV</button>
    <button id="csvBtn" title="Download the current CSV file">⬇ Download CSV</button>
  </div>

  <div class="table-wrap">
    <table id="dataTable">
      <thead>
        <tr>
          <th data-key="timestamp_bkk" data-type="str">Time (BKK)<span class="arrow"></span></th>
          <th data-key="category" data-type="str">Category<span class="arrow"></span></th>
          <th data-key="candidate" data-type="str">Candidate<span class="arrow"></span></th>
          <th data-key="rank" data-type="num" style="text-align:right;">Rank<span class="arrow"></span></th>
          <th data-key="vote_percentage" data-type="num" style="text-align:right;">Vote %<span class="arrow"></span></th>
          <th data-key="delta" data-type="num" style="text-align:right;" title="Difference vs same candidate/category's previous snapshot (percentage points)">Change %<span class="arrow"></span></th>
        </tr>
      </thead>
      <tbody></tbody>
    </table>
  </div>
</div>

<div class="toast" id="toast">Copied</div>

<footer>
  Tip: Click a column header to sort; use the category dropdown + search box to filter; "Copy as TSV" copies the currently visible rows, paste into Excel to auto-split.
</footer>

<script>
const CSV = /*__CSV__*/;
const META = /*__META__*/;

// Simple CSV parser that supports quoted fields
function parseCSV(text) {
  const rows = [];
  let row = [], field = '', inQuote = false;
  for (let i = 0; i < text.length; i++) {
    const c = text[i], next = text[i+1];
    if (inQuote) {
      if (c === '"' && next === '"') { field += '"'; i++; }
      else if (c === '"') { inQuote = false; }
      else { field += c; }
    } else {
      if (c === '"') { inQuote = true; }
      else if (c === ',') { row.push(field); field = ''; }
      else if (c === '\n' || c === '\r') {
        if (field !== '' || row.length) { row.push(field); rows.push(row); }
        row = []; field = '';
        if (c === '\r' && next === '\n') i++;
      } else { field += c; }
    }
  }
  if (field !== '' || row.length) { row.push(field); rows.push(row); }
  return rows;
}

const allRows = (() => {
  if (!CSV || !CSV.trim()) return [];
  const rows = parseCSV(CSV);
  if (rows.length < 2) return [];
  const headers = rows[0].map(h => h.trim());
  return rows.slice(1).map(cells => {
    const o = {};
    headers.forEach((h, i) => { o[h] = cells[i] !== undefined ? cells[i].trim() : ''; });
    o.rank = parseInt(o.rank, 10);
    o.vote_percentage = parseFloat(o.vote_percentage);
    o._t = Date.parse(o.timestamp_bkk ? o.timestamp_bkk.replace(' ', 'T') : '') || 0;
    return o;
  });
})();

// Compute "Change %": each candidate/category's diff vs its previous snapshot (percentage points).
// First snapshot has no "previous" — delta is null (shown as — on the page).
(function computeDeltas() {
  const byKey = {};
  for (const r of allRows) {
    const k = r.category + '|' + r.candidate;
    (byKey[k] = byKey[k] || []).push(r);
  }
  for (const k of Object.keys(byKey)) {
    byKey[k].sort((a, b) => a._t - b._t);
    for (let i = 0; i < byKey[k].length; i++) {
      byKey[k][i].delta = (i === 0) ? null : (byKey[k][i].vote_percentage - byKey[k][i-1].vote_percentage);
    }
  }
})();

function deltaCell(d) {
  if (d == null || isNaN(d)) return '<span class="delta-empty">—</span>';
  const v = d.toFixed(1);
  if (d > 0.05)  return '<span class="delta-up">+' + v + '</span>';
  if (d < -0.05) return '<span class="delta-down">' + v + '</span>';
  return '<span class="delta-flat">' + v + '</span>';
}

let filtered = [...allRows];
let sortKey = 'timestamp_bkk';
let sortAsc = false;
let sortType = 'str';
let isDefaultSort = true;   // Whether we're in "default time descending" state (flips to false when user clicks a header)

const $ = id => document.getElementById(id);
const tbody = document.querySelector('#dataTable tbody');

function badgeFor(cat) {
  if (cat === 'best-actress') return '<span class="badge badge-actress">Best Actress</span>';
  if (cat === 'best-series') return '<span class="badge badge-series">Best Series</span>';
  return cat;
}

function fmtTime(s) { return s || ''; }

function render() {
  const rows = filtered.map(r =>
    `<tr>
      <td>${fmtTime(r.timestamp_bkk)}</td>
      <td>${badgeFor(r.category)}</td>
      <td>${r.candidate}</td>
      <td class="rank">#${r.rank}</td>
      <td class="pct">${r.vote_percentage.toFixed(1)}</td>
      <td class="pct">${deltaCell(r.delta)}</td>
    </tr>`
  ).join('');
  tbody.innerHTML = rows || '<tr><td colspan="6" class="empty">No data yet — waiting for the next scheduled scrape</td></tr>';

  const candidates = new Set(allRows.map(d => d.candidate));
  const times = allRows.map(d => d._t).filter(t => t > 0);
  const minT = times.length ? new Date(Math.min(...times)).toLocaleString('en-GB', {hour12:false}) : '—';
  const maxT = times.length ? new Date(Math.max(...times)).toLocaleString('en-GB', {hour12:false}) : '—';
  $('stats').innerHTML = `
    <div>Total records<span>${allRows.length}</span></div>
    <div>Candidates<span>${candidates.size}</span></div>
    <div>Showing<span>${filtered.length}</span></div>
    <div>From<span>${minT}</span></div>
    <div>To<span>${maxT}</span></div>
  `;
  $('updated').textContent = 'Last updated: ' + (maxT !== '—' ? maxT : '—') + '  |  ' + allRows.length + ' records  |  refreshes hourly';
}

// Default sort: time descending (newest on top); within the same time slot, sort by category then rank ascending
function defaultSort() {
  isDefaultSort = true;
  sortKey = null;
  filtered.sort((a, b) => {
    if (a._t !== b._t) return b._t - a._t;                 // 1) newest time first
    if (a.category !== b.category) return a.category.localeCompare(b.category); // 2) category grouping
    return (a.rank || 0) - (b.rank || 0);                  // 3) rank ascending (rank #1 on top)
  });
  document.querySelectorAll('th').forEach(th => {
    th.querySelector('.arrow').textContent = '';
  });
  render();
}

function sortBy(key, type) {
  isDefaultSort = false;
  sortType = type;
  if (sortKey === key) sortAsc = !sortAsc;
  else { sortKey = key; sortAsc = (type === 'str'); }
  filtered.sort((a, b) => {
    const va = a[key], vb = b[key];
    // null/undefined always goes to the end, regardless of asc/desc
    if (va == null && vb == null) return 0;
    if (va == null) return 1;
    if (vb == null) return -1;
    let cmp;
    if (type === 'num') cmp = va - vb;
    else cmp = String(va).localeCompare(String(vb), 'zh-Hans-CN');
    return sortAsc ? cmp : -cmp;
  });
  document.querySelectorAll('th').forEach(th => {
    th.querySelector('.arrow').textContent = th.dataset.key === sortKey ? (sortAsc ? '↑' : '↓') : '';
  });
  render();
}

document.querySelectorAll('th').forEach(th => {
  th.addEventListener('click', () => sortBy(th.dataset.key, th.dataset.type));
});

function applyFilter() {
  const q = $('search').value.toLowerCase().trim();
  const cat = $('category').value;
  filtered = allRows.filter(r => {
    if (cat && r.category !== cat) return false;
    if (q) {
      const hay = (r.candidate + ' ' + r.category + ' ' + (META[r.category] || '')).toLowerCase();
      if (!hay.includes(q)) return false;
    }
    return true;
  });
  if (isDefaultSort) defaultSort();
  else sortBy(sortKey, sortType);
}

$('search').addEventListener('input', applyFilter);
$('category').addEventListener('change', applyFilter);

function toTSV(rows) {
  const headers = ['Time (BKK)', 'Category', 'Category friendly name', 'Candidate', 'Rank', 'Vote %', 'Change % (vs prev)'];
  const lines = [headers.join('\t')];
  for (const r of rows) {
    const dStr = (r.delta == null || isNaN(r.delta)) ? '' : ((r.delta > 0 ? '+' : '') + r.delta.toFixed(1));
    lines.push([
      r.timestamp_bkk,
      r.category,
      META[r.category] || r.category,
      r.candidate,
      r.rank,
      r.vote_percentage.toFixed(1),
      dStr
    ].join('\t'));
  }
  return lines.join('\n');
}

$('copyBtn').addEventListener('click', async () => {
  if (!filtered.length) { showToast('No data to copy'); return; }
  const tsv = toTSV(filtered);
  try {
    await navigator.clipboard.writeText(tsv);
    showToast(`✓ Copied ${filtered.length} rows, paste into Excel`);
  } catch (e) {
    const ta = document.createElement('textarea');
    ta.value = tsv;
    ta.style.position = 'fixed'; ta.style.opacity = '0';
    document.body.appendChild(ta);
    ta.select();
    try { document.execCommand('copy'); showToast(`✓ Copied ${filtered.length} rows (compatibility mode)`); }
    catch (_) { showToast('Copy failed, please use "Download CSV"'); }
    ta.remove();
  }
});

$('csvBtn').addEventListener('click', () => {
  const blob = new Blob([CSV], { type: 'text/csv;charset=utf-8' });
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = 'fk_vote_history.csv';
  document.body.appendChild(a); a.click();
  setTimeout(() => { URL.revokeObjectURL(a.href); a.remove(); }, 100);
});

function showToast(msg) {
  const t = $('toast');
  t.textContent = msg;
  t.classList.add('show');
  clearTimeout(showToast._timer);
  showToast._timer = setTimeout(() => t.classList.remove('show'), 2400);
}

// Init: time descending (newest on top; within same time slot sort by category+rank)
defaultSort();
</script>
</body>
</html>
"""


def main():
    ap = argparse.ArgumentParser(description="FK 无头定时抓取")
    ap.add_argument("--no-chart", action="store_true", help="只抓取，不重绘图表 HTML")
    ap.add_argument("--no-table", action="store_true", help="只抓取，不重绘数据表 HTML")
    args = ap.parse_args()

    from playwright.sync_api import sync_playwright

    history = load_history()
    for cat in CATEGORIES:
        history.setdefault(cat, [])

    bkk = timezone(timedelta(hours=7))
    now = datetime.now(bkk)
    now_iso = now.isoformat()
    now_bkk = now.strftime("%Y-%m-%d %H:%M:%S")

    csv_rows = []
    print(f"[{now_bkk} BKK] 开始抓取 {len(CATEGORIES)} 个奖项...")

    with sync_playwright() as p:
        # --no-sandbox: 云端多以 root 运行；--disable-dev-shm-usage: 容器/dev/shm 太小防崩溃
        browser = p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
        page = browser.new_page()
        for cat, url in CATEGORIES.items():
            try:
                cands = extract_category(page, url)
                if not cands:
                    print(f"  [!] {cat}: 未解析到候选人，跳过")
                    continue
                cands.sort(key=lambda c: c["rank"])
                history[cat].append({
                    "t": now_iso,
                    "candidates": [{"name": c["name"], "rank": c["rank"], "pct": c["pct"]} for c in cands],
                })
                print(f"  [✓] {cat}: {len(cands)} 名候选人")
                for c in cands:
                    print(f"        #{c['rank']}  {c['pct']:>5.1f}%  {c['name']}")
                    csv_rows.append([now_iso, now_bkk, cat, c["name"], c["rank"], f"{c['pct']:.1f}"])
            except Exception as e:
                print(f"  [X] {cat}: 抓取失败 - {e}")
        browser.close()

    if csv_rows:
        append_csv(csv_rows)
        save_history(history)
        print(f"\n已写入 CSV: {CSV_PATH}")
        print(f"已写入 JSON 历史: {JSON_PATH}")

    if not args.no_chart:
        path = generate_timeline(history, SUSPECT_PAID_THRESHOLD)
        print(f"已生成时间线图表: {path}")

    if not args.no_table:
        path = generate_data_table()
        print(f"已生成数据表页面: {path}")

    print("完成。")


if __name__ == "__main__":
    main()
