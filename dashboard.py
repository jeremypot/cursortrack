# -*- coding: utf-8 -*-
"""
dashboard.py — browser-based GUI for cursortrack.

Usage:
    python dashboard.py              # refresh data, open browser
    python dashboard.py --no-refresh # skip tracker.py, show last cursor-usage.json
    python dashboard.py --port 9000  # custom port (default 8765)

Stdlib only — no third-party packages required.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import threading
import time
import webbrowser
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

BASE_DIR = Path(__file__).parent
TRACKER = BASE_DIR / "tracker.py"
DATA_FILE = BASE_DIR / "cursor-usage.json"

DATA_SYNC_INTERVAL  = 15 * 60   # seconds between automatic data syncs
PRICE_SYNC_INTERVAL = 24 * 3600  # seconds between automatic price refreshes

# Shared scheduler state — written by background threads, read by /api/status
_sched: dict = {
    "last_data_sync":   None,   # ISO timestamp
    "next_data_sync":   None,   # ISO timestamp
    "last_price_sync":  None,   # ISO timestamp
    "next_price_sync":  None,   # ISO timestamp
    "data_sync_running":  False,
    "price_sync_running": False,
}

# ── HTML template ─────────────────────────────────────────────────────────────
# Stored as a constant so the server requires no filesystem reads per request.

HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Cursor Usage Dashboard</title>
<style>
  :root {
    --bg: #0f1117;
    --surface: #1a1d27;
    --surface2: #222536;
    --border: #2e3148;
    --accent: #6c63ff;
    --accent2: #3ecfcf;
    --green: #22c55e;
    --yellow: #f59e0b;
    --red: #ef4444;
    --muted: #6b7280;
    --text: #e2e8f0;
    --text2: #94a3b8;
    --radius: 10px;
    --shadow: 0 4px 24px rgba(0,0,0,.45);
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { background: var(--bg); color: var(--text); font-family: 'Segoe UI', system-ui, sans-serif; font-size: 14px; min-height: 100vh; }

  /* ── header ── */
  header {
    background: var(--surface);
    border-bottom: 1px solid var(--border);
    padding: 14px 28px;
    display: flex;
    align-items: center;
    gap: 16px;
    position: sticky;
    top: 0;
    z-index: 10;
  }
  header h1 { font-size: 18px; font-weight: 700; letter-spacing: -.3px; flex: 1; }
  header h1 span { color: var(--accent); }
  .badge {
    display: inline-flex;
    align-items: center;
    gap: 5px;
    padding: 3px 10px;
    border-radius: 20px;
    font-size: 12px;
    font-weight: 600;
    background: var(--surface2);
    border: 1px solid var(--border);
  }
  .badge.plan  { color: var(--accent2); border-color: var(--accent2); }
  .badge.mode  { color: var(--yellow); border-color: var(--yellow); cursor: pointer; user-select: none; }
  .badge.mode:hover { background: var(--surface); }

  /* ── rates dropdown ── */
  .rates-wrap { position: relative; }
  .rates-dropdown {
    display: none;
    flex-direction: column;
    position: absolute;
    top: calc(100% + 8px);
    right: 0;
    min-width: 340px;
    max-height: 480px;
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    box-shadow: var(--shadow);
    z-index: 200;
    overflow: hidden;
  }
  .rates-dropdown.open { display: flex; }
  .rates-table-scroll {
    overflow-y: auto;
    flex: 1;
    min-height: 0;
  }
  .rates-dropdown-header {
    padding: 10px 16px;
    font-size: 11px;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: .07em;
    color: var(--muted);
    border-bottom: 1px solid var(--border);
  }
  .rates-table {
    width: 100%;
    border-collapse: collapse;
    font-size: 12px;
  }
  .rates-table th {
    padding: 6px 14px;
    text-align: left;
    color: var(--muted);
    font-weight: 600;
    font-size: 11px;
    border-bottom: 1px solid var(--border);
    background: var(--surface2);
  }
  .rates-table th:not(:first-child) { text-align: right; }
  .rates-table td {
    padding: 6px 14px;
    color: var(--text2);
    border-bottom: 1px solid var(--border);
  }
  .rates-table td:first-child { color: var(--text); font-weight: 500; }
  .rates-table td:not(:first-child) { text-align: right; font-family: monospace; }
  .rates-table tr:last-child td { border-bottom: none; }
  .rates-table tr:hover td { background: var(--surface2); }
  .rates-footer {
    padding: 10px 14px;
    font-size: 11px;
    color: var(--muted);
    border-top: 1px solid var(--border);
    background: var(--surface2);
    display: flex;
    flex-direction: column;
    gap: 8px;
  }
  .rates-footer .sync-lines { display: flex; flex-direction: column; gap: 3px; line-height: 1.5; }
  .rates-footer .sync-line { display: flex; align-items: center; gap: 5px; }
  .rates-footer .sync-dot {
    width: 5px; height: 5px; border-radius: 50%;
    background: var(--muted); flex-shrink: 0;
  }
  .rates-footer .sync-dot.active { background: var(--accent); animation: pulse .9s ease infinite; }
  @keyframes pulse { 0%,100%{opacity:1} 50%{opacity:.3} }
  #updatePricesBtn {
    display: flex;
    align-items: center;
    gap: 6px;
    padding: 5px 12px;
    border-radius: 6px;
    border: 1px solid var(--border);
    background: var(--surface);
    color: var(--text);
    font-size: 12px;
    font-weight: 600;
    cursor: pointer;
    align-self: flex-start;
    transition: background .12s;
  }
  #updatePricesBtn:hover { background: var(--surface2); }
  #updatePricesBtn:disabled { opacity: .5; cursor: wait; }
  #updatePricesBtn svg { animation: none; }
  #updatePricesBtn.spinning svg { animation: spin .7s linear infinite; }

  .meta { color: var(--text2); font-size: 13px; }


  #refreshBtn {
    display: flex;
    align-items: center;
    gap: 7px;
    padding: 7px 18px;
    border-radius: 7px;
    border: none;
    background: var(--accent);
    color: #fff;
    font-size: 13px;
    font-weight: 600;
    cursor: pointer;
    transition: opacity .15s;
  }
  #refreshBtn:disabled { opacity: .5; cursor: wait; }
  #refreshBtn svg { animation: none; }
  #refreshBtn.spinning svg { animation: spin .7s linear infinite; }
  @keyframes spin { to { transform: rotate(360deg); } }

  /* ── project filter panel ── */
  .proj-filter-wrap { position: relative; }
  #projFilterBtn {
    display: flex; align-items: center; gap: 6px;
    padding: 5px 12px; border-radius: 7px;
    border: 1px solid var(--border); background: var(--surface2);
    color: var(--text); font-size: 12px; font-weight: 600; cursor: pointer;
  }
  #projFilterBtn:hover { background: var(--surface); }
  #projFilterBtn .badge-count {
    background: var(--accent2); color: #fff; border-radius: 10px;
    padding: 1px 6px; font-size: 10px; font-weight: 700;
  }
  .proj-panel {
    display: none; position: absolute; top: calc(100% + 6px); right: 0;
    min-width: 240px; max-height: 320px; overflow-y: auto;
    background: var(--surface); border: 1px solid var(--border);
    border-radius: var(--radius); box-shadow: var(--shadow); z-index: 200;
  }
  .proj-panel.open { display: block; }
  .proj-panel-header {
    padding: 8px 14px; font-size: 11px; font-weight: 700; text-transform: uppercase;
    letter-spacing: .07em; color: var(--muted); border-bottom: 1px solid var(--border);
    background: var(--surface2); display: flex; justify-content: space-between; align-items: center;
    position: sticky; top: 0;
  }
  .proj-panel-header button {
    font-size: 10px; font-weight: 600; color: var(--accent); background: none;
    border: none; cursor: pointer; padding: 0;
  }
  .proj-panel-item {
    display: flex; align-items: center; gap: 10px; padding: 8px 14px;
    border-bottom: 1px solid var(--border);
  }
  .proj-panel-item:last-child { border-bottom: none; }
  .proj-panel-item:hover { background: var(--surface2); }
  .proj-panel-item input[type=checkbox] { accent-color: var(--accent); width: 14px; height: 14px; cursor: pointer; flex-shrink: 0; }
  .proj-panel-item label { font-size: 13px; cursor: pointer; flex: 1; }
  .proj-panel-item .proj-cost { font-size: 11px; color: var(--muted); font-family: monospace; }

  /* ── daily chart ── */
  .daily-legend { display: flex; flex-wrap: wrap; gap: 10px; margin-top: 10px; font-size: 11px; color: var(--text2); }
  .daily-legend-item { display: flex; align-items: center; gap: 5px; }
  .daily-legend-dot { width: 10px; height: 10px; border-radius: 2px; flex-shrink: 0; }
  #dailyChartWrap svg { width: 100%; display: block; }
  #dailyChartWrap .no-data { color: var(--muted); font-size: 13px; padding: 18px 0; }

  /* ── PDF / print ── */
  #exportPdfBtn {
    display: flex; align-items: center; gap: 6px;
    padding: 5px 12px; border-radius: 7px;
    border: 1px solid var(--border); background: var(--surface2);
    color: var(--text); font-size: 12px; font-weight: 600; cursor: pointer; white-space: nowrap;
  }
  #exportPdfBtn:hover { background: var(--surface); }

  @media print {
    * { -webkit-print-color-adjust: exact !important; print-color-adjust: exact !important; box-shadow: none !important; }
    body { background: #fff !important; color: #111 !important; font-size: 12px !important; }

    /* hide interactive chrome */
    header, .filter-bar, .date-range-picker, .gen-at, details, #toast,
    #refreshBtn, #exportPdfBtn, #projFilterBtn, .rates-wrap,
    .daily-legend { display: none !important; }

    main { max-width: 100% !important; padding: 0 !important; }
    .print-header { display: block !important; margin-bottom: 18px; }

    /* section headings */
    .section-title { color: #333 !important; border-color: #bbb !important; font-size: 11px !important; }

    /* KPI strip */
    .kpi { background: #f4f4f4 !important; border: 1px solid #ddd !important; }
    .kpi .label { color: #555 !important; }
    .kpi .value { color: #1a6b3c !important; font-size: 20px !important; }
    .kpi .sub { color: #888 !important; }

    /* cost breakdown chart */
    .chart-wrap { background: #fff !important; border: 1px solid #ddd !important; padding: 14px 18px !important; }
    .chart-bar-track { background: #ebebeb !important; }
    .chart-bar { -webkit-print-color-adjust: exact !important; }
    .chart-label { color: #222 !important; }
    .chart-cost { color: #1a6b3c !important; }

    /* daily SVG chart */
    #dailyChartWrap { margin-bottom: 8px; }
    svg line { stroke: #ddd !important; }
    svg text { fill: #555 !important; }
    svg rect { -webkit-print-color-adjust: exact !important; }
    .daily-legend { display: flex !important; color: #333 !important; }
    .daily-legend-item { color: #333 !important; }

    /* projects section always starts on a new page */
    #projectsHeading { page-break-before: always !important; break-before: page !important; padding-top: 8px; }

    /* cards */
    .cards-grid { grid-template-columns: repeat(2, 1fr) !important; gap: 12px !important; }
    .card { background: #fff !important; border: 1px solid #ccc !important; break-inside: avoid; padding: 14px !important; }
    .card-title { color: #111 !important; }
    .card-path { color: #888 !important; font-size: 10px !important; }
    .card-cost { color: #1a6b3c !important; font-size: 22px !important; }
    .card-cost small { color: #888 !important; }
    .stat { background: #f4f4f4 !important; border: 1px solid #e8e8e8 !important; }
    .stat .s-label { color: #666 !important; }
    .stat .s-value { color: #111 !important; }
    .card-footer { margin-top: 8px !important; }
    .tag { background: #efefef !important; border: 1px solid #ccc !important; color: #444 !important; }
    .tag.model { background: #e8f0ff !important; border-color: #b3c8f0 !important; color: #2a4a8a !important; }
    .dates { color: #888 !important; }
  }

  /* ── toast ── */
  #toast {
    position: fixed;
    bottom: 28px;
    left: 50%;
    transform: translateX(-50%) translateY(80px);
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 10px 20px;
    font-size: 13px;
    font-weight: 500;
    box-shadow: var(--shadow);
    z-index: 999;
    transition: transform .25s ease, opacity .25s ease;
    opacity: 0;
    max-width: 480px;
    text-align: center;
  }
  #toast.show { transform: translateX(-50%) translateY(0); opacity: 1; }
  #toast.success { color: var(--accent); border-color: var(--accent); }
  #toast.error { color: #f87171; border-color: #f87171; }

  /* ── main layout ── */
  main { max-width: 1140px; margin: 0 auto; padding: 28px 20px; }

  /* ── filter bar ── */
  .filter-bar {
    display: flex;
    align-items: center;
    gap: 6px;
    margin-bottom: 24px;
    flex-wrap: wrap;
  }
  .filter-bar .filter-label {
    font-size: 11px;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: .07em;
    color: var(--muted);
    margin-right: 4px;
  }
  .filter-btn {
    padding: 5px 14px;
    border-radius: 20px;
    border: 1px solid var(--border);
    background: var(--surface2);
    color: var(--text2);
    font-size: 12px;
    font-weight: 600;
    cursor: pointer;
    transition: border-color .15s, color .15s, background .15s;
  }
  .filter-btn:hover { border-color: var(--accent); color: var(--text); }
  .filter-btn.active { background: var(--accent); border-color: var(--accent); color: #fff; }
  .filter-btn:disabled { opacity: .45; cursor: wait; }

  .section-title {
    font-size: 11px;
    font-weight: 700;
    letter-spacing: .08em;
    text-transform: uppercase;
    color: var(--muted);
    margin-bottom: 12px;
  }

  /* ── KPI strip ── */
  .kpi-grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
    gap: 12px;
    margin-bottom: 28px;
  }
  .kpi {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    padding: 18px 20px;
  }
  .kpi .label { font-size: 11px; color: var(--text2); text-transform: uppercase; letter-spacing: .07em; margin-bottom: 6px; }
  .kpi .value { font-size: 26px; font-weight: 700; line-height: 1.1; }
  .kpi .value.cost { color: var(--green); }
  .kpi .sub { font-size: 11px; color: var(--muted); margin-top: 4px; }

  /* ── chart ── */
  .chart-wrap {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    padding: 20px 24px;
    margin-bottom: 28px;
  }
  .chart-row {
    display: grid;
    grid-template-columns: 140px 1fr 80px;
    align-items: center;
    gap: 12px;
    margin-bottom: 10px;
  }
  .chart-row:last-child { margin-bottom: 0; }
  .chart-label { font-size: 13px; font-weight: 600; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .chart-bar-track { background: var(--surface2); border-radius: 4px; height: 18px; overflow: hidden; }
  .chart-bar { height: 100%; border-radius: 4px; transition: width .5s ease; }
  .chart-cost { text-align: right; font-size: 13px; font-weight: 700; color: var(--green); }

  /* ── repo cards ── */
  .cards-grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(320px, 1fr));
    gap: 16px;
    margin-bottom: 28px;
  }
  .card {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    padding: 20px;
    display: flex;
    flex-direction: column;
    gap: 14px;
    box-shadow: var(--shadow);
  }
  .card-header { display: flex; align-items: flex-start; gap: 10px; }
  .card-icon { font-size: 22px; }
  .card-title { font-size: 16px; font-weight: 700; word-break: break-all; }
  .card-path { font-size: 11px; color: var(--muted); margin-top: 2px; word-break: break-all; }
  .card-cost { font-size: 28px; font-weight: 800; color: var(--green); }
  .card-cost small { font-size: 13px; font-weight: 400; color: var(--muted); }

  .stat-grid {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 8px;
  }
  .stat { background: var(--surface2); border-radius: 6px; padding: 8px 10px; }
  .stat .s-label { font-size: 10px; color: var(--muted); text-transform: uppercase; letter-spacing: .07em; }
  .stat .s-value { font-size: 14px; font-weight: 700; margin-top: 2px; }

  .card-footer { display: flex; flex-wrap: wrap; gap: 6px; align-items: center; }
  .tag {
    padding: 2px 8px;
    border-radius: 12px;
    font-size: 11px;
    font-weight: 600;
    background: var(--surface2);
    border: 1px solid var(--border);
    color: var(--text2);
  }
  .tag.commit  { color: var(--green);   border-color: var(--green); }
  .tag.workspace { color: var(--accent2); border-color: var(--accent2); }
  .tag.bubble  { color: var(--yellow);  border-color: var(--yellow); }
  .tag.watcher { color: var(--muted);   border-color: var(--muted); }
  .tag.model   { color: var(--accent);  border-color: var(--accent); }

  .dates { font-size: 11px; color: var(--muted); margin-left: auto; text-align: right; }

  /* ── unattributed & notes ── */
  details {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    margin-bottom: 16px;
    overflow: hidden;
  }
  summary {
    padding: 14px 20px;
    cursor: pointer;
    font-weight: 600;
    display: flex;
    align-items: center;
    gap: 8px;
    user-select: none;
    list-style: none;
  }
  summary::-webkit-details-marker { display: none; }
  summary .arrow { transition: transform .2s; font-size: 12px; color: var(--muted); }
  details[open] summary .arrow { transform: rotate(90deg); }
  .details-body { padding: 0 20px 20px; }

  .unattr-stat { display: inline-flex; flex-direction: column; margin-right: 28px; margin-top: 12px; }
  .unattr-stat .s-label { font-size: 11px; color: var(--muted); }
  .unattr-stat .s-value { font-size: 18px; font-weight: 700; }

  .cost-note { font-size: 12px; color: var(--text2); line-height: 1.7; }

  /* ── toast ── */
  #toast {
    position: fixed;
    bottom: 24px;
    right: 24px;
    background: var(--surface2);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 12px 18px;
    font-size: 13px;
    box-shadow: var(--shadow);
    opacity: 0;
    transform: translateY(8px);
    transition: opacity .2s, transform .2s;
    pointer-events: none;
    z-index: 100;
  }
  #toast.show { opacity: 1; transform: none; }
  #toast.err  { border-color: var(--red); color: var(--red); }
  #toast.ok   { border-color: var(--green); color: var(--green); }

  /* ── generated label ── */
  .gen-at { font-size: 11px; color: var(--muted); text-align: center; margin-top: 24px; }

  /* ── custom date range picker ── */
  .date-range-picker {
    display: none;
    align-items: center;
    gap: 8px;
    padding: 10px 14px;
    background: var(--surface2);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    margin-bottom: 16px;
    flex-wrap: wrap;
  }
  .date-range-picker.open { display: flex; }
  .date-range-picker label { font-size: 12px; color: var(--muted); font-weight: 600; }
  .date-range-picker input[type="date"] {
    background: var(--surface);
    border: 1px solid var(--border);
    color: var(--text);
    padding: 5px 10px;
    border-radius: 6px;
    font-size: 13px;
    color-scheme: dark;
    cursor: pointer;
  }
  .date-range-picker input[type="date"]:focus { outline: none; border-color: var(--accent); }
  .dr-sep { color: var(--muted); font-size: 12px; }
</style>
</head>
<body>

<header>
  <h1>cursor<span>track</span></h1>
  <div class="rates-wrap" id="ratesWrap">
    <span class="badge mode" id="hMode" onclick="toggleRatesDropdown()" title="Click to view model rates &amp; sync status"></span>
    <div class="rates-dropdown" id="ratesDropdown">
      <div class="rates-dropdown-header">Model pricing rates (per 1M tokens)</div>
      <div class="rates-table-scroll">
        <table class="rates-table" id="ratesTable"></table>
      </div>
      <div class="rates-footer" id="ratesFooter">
        <span id="ratesFetchLine" style="font-size:10px;color:var(--muted);display:block;margin-bottom:6px"></span>
        <div class="sync-lines">
          <div class="sync-line"><span class="sync-dot" id="dataDot"></span><span id="dataSync"></span></div>
          <div class="sync-line"><span class="sync-dot" id="priceDot"></span><span id="priceSync"></span></div>
        </div>
        <button id="updatePricesBtn" onclick="doUpdatePrices()">
          <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round">
            <path d="M21 2v6h-6"/><path d="M3 12a9 9 0 0 1 15-6.7L21 8"/>
            <path d="M3 22v-6h6"/><path d="M21 12a9 9 0 0 1-15 6.7L3 16"/>
          </svg>
          Update Prices Now
        </button>
      </div>
    </div>
  </div>
  <button id="refreshBtn" onclick="doRefresh()">
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round">
      <path d="M21 2v6h-6"/><path d="M3 12a9 9 0 0 1 15-6.7L21 8"/>
      <path d="M3 22v-6h6"/><path d="M21 12a9 9 0 0 1-15 6.7L3 16"/>
    </svg>
    Refresh
  </button>
</header>

<div id="toast"></div>

<main>
  <div class="print-header" style="display:none;margin-bottom:20px">
    <h2 style="margin:0 0 4px;font-size:20px">cursortrack — Cost Report</h2>
    <div id="printMeta" style="font-size:12px;color:#555"></div>
  </div>

  <div class="filter-bar" id="filterBar">
    <span class="filter-label">Period</span>
    <button class="filter-btn active" data-filter="all"    onclick="applyFilter(this)">All time</button>
    <button class="filter-btn"        data-filter="last7"  onclick="applyFilter(this)">Last 7 days</button>
    <button class="filter-btn"        data-filter="last30" onclick="applyFilter(this)">Last 30 days</button>
    <button class="filter-btn"        data-filter="last90" onclick="applyFilter(this)">Last 90 days</button>
    <button class="filter-btn"        data-filter="month"  onclick="applyFilter(this)">This month</button>
    <button class="filter-btn"        data-filter="custom" onclick="toggleCustomPicker(this)">Custom&#8230;</button>
    <div class="proj-filter-wrap" id="projFilterWrap">
      <button id="projFilterBtn" onclick="toggleProjPanel(event)">
        &#128193; Projects <span class="badge-count" id="hiddenBadge" style="display:none"></span>
      </button>
      <div class="proj-panel" id="projPanel">
        <div class="proj-panel-header">
          <span>Show / hide projects</span>
          <button onclick="showAllProjects()">Show all</button>
        </div>
        <div id="projPanelItems"></div>
      </div>
    </div>
    <button id="exportPdfBtn" onclick="doExportPdf()">
      <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
        <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/>
        <polyline points="14 2 14 8 20 8"/><line x1="12" y1="18" x2="12" y2="12"/>
        <polyline points="9 15 12 18 15 15"/>
      </svg>
      Export PDF
    </button>
  </div>

  <div class="date-range-picker" id="dateRangePicker">
    <label>From</label>
    <input type="date" id="drFrom">
    <span class="dr-sep">&#8594;</span>
    <label>To</label>
    <input type="date" id="drUntil">
    <button class="filter-btn" onclick="applyCustomRange()" id="drApplyBtn">Apply</button>
  </div>

  <p class="section-title">Summary</p>
  <div class="kpi-grid" id="kpiStrip"></div>

  <p class="section-title">Cost breakdown</p>
  <div class="chart-wrap" id="chartWrap"></div>

  <p class="section-title">Daily usage</p>
  <div id="dailyChartWrap"></div>
  <div class="daily-legend" id="dailyLegend"></div>

  <p class="section-title" id="projectsHeading">Projects</p>
  <div class="cards-grid" id="cardsGrid"></div>

  <details id="unattributed">
    <summary>
      <span class="arrow">&#9654;</span>
      Unattributed sessions
      <span id="unattribBadge" class="tag" style="margin-left:4px"></span>
    </summary>
    <div class="details-body" id="unattribBody"></div>
  </details>

  <details>
    <summary>
      <span class="arrow">&#9654;</span>
      Methodology &amp; cost notes
    </summary>
    <div class="details-body">
      <p class="cost-note" id="costNote"></p>
    </div>
  </details>

  <p class="gen-at" id="genAt"></p>
</main>

<div id="toast"></div>

<script>
const COLORS = [
  '#6c63ff','#3ecfcf','#f59e0b','#22c55e','#ec4899','#f97316','#a78bfa','#34d399',
  '#60a5fa','#fb7185','#a3e635','#fbbf24',
];

let _customRange   = null;  // { from: 'YYYY-MM-DD', until: 'YYYY-MM-DD' }
let _lastRawData   = null;  // full unfiltered data from /api/data
let _hiddenProjects = new Set(JSON.parse(localStorage.getItem('hiddenProjects') || '[]'));

function timeAgo(date) {
  const secs = Math.floor((Date.now() - date) / 1000);
  if (secs < 60) return 'just now';
  const mins = Math.floor(secs / 60);
  if (mins < 60) return mins + 'm ago';
  const hrs = Math.floor(mins / 60);
  if (hrs < 24) return hrs + 'h ago';
  return Math.floor(hrs / 24) + 'd ago';
}

function toggleRatesDropdown() {
  document.getElementById('ratesDropdown').classList.toggle('open');
}

// Close dropdown when clicking outside
document.addEventListener('click', e => {
  const wrap = document.getElementById('ratesWrap');
  if (wrap && !wrap.contains(e.target)) {
    document.getElementById('ratesDropdown').classList.remove('open');
  }
});

function populateRatesTable(prices, fetchedAt) {
  if (!prices) return;
  const ORDER_HINT = ['default','claude','gpt','o1','o3','gemini','cursor','composer'];
  const entries = Object.entries(prices).sort(([a], [b]) => {
    const ai = ORDER_HINT.findIndex(h => a.startsWith(h));
    const bi = ORDER_HINT.findIndex(h => b.startsWith(h));
    return (ai < 0 ? 99 : ai) - (bi < 0 ? 99 : bi) || a.localeCompare(b);
  });

  const thead = `<thead><tr><th>Model</th><th>Input $/1M</th><th>Output $/1M</th></tr></thead>`;
  const rows = entries.map(([model, rates]) => {
    const label = model === 'default' ? 'Auto (default)' : model;
    const inp = rates.input != null ? '$' + rates.input.toFixed(2) : '—';
    const out = rates.output != null ? '$' + rates.output.toFixed(2) : '—';
    return `<tr><td>${label}</td><td>${inp}</td><td>${out}</td></tr>`;
  }).join('');
  document.getElementById('ratesTable').innerHTML = thead + '<tbody>' + rows + '</tbody>';

  const fetchLine = document.getElementById('ratesFetchLine');
  if (fetchLine) {
    fetchLine.textContent = fetchedAt
      ? 'Fetched from cursor.com — ' + timeAgo(new Date(fetchedAt))
      : 'Rates loaded from local prices.json';
  }
}

function fmt(n) {
  if (n == null) return '—';
  if (n >= 1e9)  return (n/1e9).toFixed(1)+'B';
  if (n >= 1e6)  return (n/1e6).toFixed(1)+'M';
  if (n >= 1e3)  return (n/1e3).toFixed(1)+'K';
  return String(n);
}
function fmtCost(c) { return c == null ? '—' : '$' + c.toFixed(4); }
function fmtDate(s) {
  if (!s) return '—';
  const d = new Date(s);
  return d.toLocaleDateString(undefined, {month:'short', day:'numeric', year:'numeric'});
}
function attrTag(layer) {
  const map = {
    'commit-linked':   ['commit',    'commit-linked'],
    'workspace-sqlite':['workspace', 'workspace-sqlite'],
    'bubble-context':  ['bubble',    'bubble-context'],
    'watcher-fallback':['watcher',   'watcher-fallback'],
    'bubble-no-files': ['watcher',   'bubble-no-files'],
  };
  const [cls, label] = map[layer] || ['', layer];
  return `<span class="tag ${cls}">${label}</span>`;
}

// ── project filter helpers ────────────────────────────────────────────────────
function saveHiddenProjects() {
  localStorage.setItem('hiddenProjects', JSON.stringify([..._hiddenProjects]));
}

function toggleProjPanel(e) {
  e.stopPropagation();
  document.getElementById('projPanel').classList.toggle('open');
}

function showAllProjects() {
  _hiddenProjects.clear();
  saveHiddenProjects();
  if (_lastRawData) render(_lastRawData);
}

function toggleProject(name, checked) {
  if (checked) _hiddenProjects.delete(name);
  else         _hiddenProjects.add(name);
  saveHiddenProjects();
  if (_lastRawData) render(_lastRawData);
}

function buildProjPanel(allRepos, allCosts) {
  const items = Object.keys(allRepos).map(name => {
    const cost = allCosts[name] || 0;
    const checked = !_hiddenProjects.has(name);
    const safe = name.replace(/\\/g, '\\\\').replace(/'/g, "\\'");
    return `<div class="proj-panel-item">
      <input type="checkbox" id="pc_${name}" ${checked ? 'checked' : ''}
             onchange="toggleProject('${safe}', this.checked)">
      <label for="pc_${name}">${name}</label>
      <span class="proj-cost">${fmtCost(cost)}</span>
    </div>`;
  }).join('');
  document.getElementById('projPanelItems').innerHTML = items || '<div style="padding:10px 14px;color:var(--muted);font-size:12px">No projects yet</div>';
  const hidCount = _hiddenProjects.size;
  const badge = document.getElementById('hiddenBadge');
  badge.textContent = hidCount;
  badge.style.display = hidCount ? '' : 'none';
}

document.addEventListener('click', e => {
  const wrap = document.getElementById('projFilterWrap');
  if (wrap && !wrap.contains(e.target))
    document.getElementById('projPanel').classList.remove('open');
});

// ── daily chart ───────────────────────────────────────────────────────────────
let _lastDailyData = null;

async function loadDailyData() {
  try {
    const body = _customRange
      ? JSON.stringify({ from: _customRange.from, until: _customRange.until })
      : JSON.stringify({});
    const res = await fetch('/api/daily', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body,
    });
    _lastDailyData = await res.json();
    renderDailyChart(_lastDailyData, _hiddenProjects);
  } catch(e) { /* non-critical */ }
}

function renderDailyChart(daily, hiddenSet) {
  const wrap   = document.getElementById('dailyChartWrap');
  const legend = document.getElementById('dailyLegend');
  if (!daily || !daily.days || !Object.keys(daily.days).length) {
    wrap.innerHTML = '<p class="no-data" style="color:var(--muted);font-size:13px;padding:18px 0">No daily data available for this period.</p>';
    legend.innerHTML = '';
    return;
  }

  const visibleRepos = (daily.repos || []).filter(r => !hiddenSet.has(r));
  if (!visibleRepos.length) {
    wrap.innerHTML = '<p class="no-data" style="color:var(--muted);font-size:13px;padding:18px 0">All projects are hidden.</p>';
    legend.innerHTML = '';
    return;
  }

  const days = Object.keys(daily.days).sort();
  const repoColors = {};
  visibleRepos.forEach((r, i) => { repoColors[r] = COLORS[i % COLORS.length]; });

  let maxStack = 0;
  days.forEach(d => {
    const stack = visibleRepos.reduce((s, r) => s + (daily.days[d][r] || 0), 0);
    if (stack > maxStack) maxStack = stack;
  });
  if (!maxStack) maxStack = 0.01;

  const svgW = 900, svgH = 170, padL = 52, padR = 10, padT = 8, padB = 30;
  const chartW = svgW - padL - padR;
  const chartH = svgH - padT - padB;
  const barW   = Math.max(3, Math.min(30, Math.floor(chartW / Math.max(days.length, 1)) - 2));
  const barGap = chartW / Math.max(days.length, 1);

  let yLabels = '', bars = '', xLabels = '';

  // Y axis grid + labels
  const yTicks = 4;
  for (let i = 0; i <= yTicks; i++) {
    const v = maxStack * (i / yTicks);
    const y = padT + chartH - chartH * i / yTicks;
    const label = v < 0.01 ? '$0' : v < 1 ? '$' + v.toFixed(2) : '$' + v.toFixed(2);
    yLabels += `<text x="${padL-5}" y="${y+4}" text-anchor="end" font-size="9" fill="var(--muted)">${label}</text>`;
    yLabels += `<line x1="${padL}" y1="${y}" x2="${svgW-padR}" y2="${y}" stroke="var(--border)" stroke-width="0.5"/>`;
  }

  days.forEach((day, di) => {
    const x      = padL + di * barGap + (barGap - barW) / 2;
    const dayData = daily.days[day] || {};
    let yBase  = padT + chartH;

    // Stack bottom-up
    visibleRepos.forEach(r => {
      const val  = dayData[r] || 0;
      if (val <= 0) return;
      const barH = Math.max(1, (val / maxStack) * chartH);
      yBase -= barH;
      const esc = r.replace(/"/g, '&quot;');
      bars += `<rect x="${x.toFixed(1)}" y="${yBase.toFixed(1)}" width="${barW}" height="${barH.toFixed(1)}" fill="${repoColors[r]}" rx="1"><title>${esc}: ${fmtCost(val)} on ${day}</title></rect>`;
    });

    const showLabel = days.length <= 14 || di % Math.ceil(days.length / 14) === 0 || di === days.length - 1;
    if (showLabel) {
      xLabels += `<text x="${(x + barW/2).toFixed(1)}" y="${svgH-6}" text-anchor="middle" font-size="9" fill="var(--muted)">${day.slice(5)}</text>`;
    }
  });

  wrap.innerHTML = `<svg viewBox="0 0 ${svgW} ${svgH}" xmlns="http://www.w3.org/2000/svg" style="overflow:visible;width:100%">${yLabels}${bars}${xLabels}</svg>`;

  legend.innerHTML = visibleRepos.map(r =>
    `<span class="daily-legend-item"><span class="daily-legend-dot" style="background:${repoColors[r]}"></span>${r}</span>`
  ).join('');
}

// ── Export PDF ────────────────────────────────────────────────────────────────
function doExportPdf() {
  window.print();
}

// ── main render ───────────────────────────────────────────────────────────────
function render(data) {
  _lastRawData = data;

  const allRepos = data.repos || {};
  const allCosts = {};
  Object.entries(allRepos).forEach(([n, r]) => { allCosts[n] = r.estimated_cost_usd || 0; });

  // Apply visibility filter
  const repos = Object.fromEntries(
    Object.entries(allRepos).filter(([n]) => !_hiddenProjects.has(n))
  );

  // Recalculate totals from visible repos only
  const baseTotal = data.totals || {};
  const visibleTotals = {
    estimated_cost_usd: Object.values(repos).reduce((s, r) => s + (r.estimated_cost_usd || 0), 0),
    conversations:      Object.values(repos).reduce((s, r) => s + (r.conversations || 0), 0),
    requests:           Object.values(repos).reduce((s, r) => s + (r.requests || 0), 0),
    input_tokens:       Object.values(repos).reduce((s, r) => s + (r.input_tokens || 0), 0),
    output_tokens:      Object.values(repos).reduce((s, r) => s + (r.output_tokens || 0), 0),
    files_edited:       Object.values(repos).reduce((s, r) => s + (r.files_edited || 0), 0),
  };
  // When all projects are visible, use server totals (includes unattributed)
  const totals = _hiddenProjects.size === 0 ? baseTotal : visibleTotals;
  const unattr = _hiddenProjects.size === 0 ? (data.unattributed || {}) : {};

  // Populate project filter panel (always full list)
  buildProjPanel(allRepos, allCosts);

  // Header badge
  let modeText = 'per-model rates \u25be';
  if (data.prices_fetched_at) {
    modeText += '  \u00b7  fetched ' + timeAgo(new Date(data.prices_fetched_at));
  }
  document.getElementById('hMode').textContent = modeText;
  populateRatesTable(data.prices, data.prices_fetched_at);

  // KPIs
  const kpis = [
    { label:'Total estimated cost', value: fmtCost(totals.estimated_cost_usd), cls:'cost', sub:'per-model API rates' },
    { label:'Conversations',        value: fmt(totals.conversations),  sub:'across all repos' },
    { label:'AI requests',          value: fmt(totals.requests),       sub:'messages sent' },
    { label:'Input tokens',         value: fmt(totals.input_tokens),   sub:'context window (actual)' },
    { label:'Output tokens',        value: fmt(totals.output_tokens),  sub:'delta estimate' },
    { label:'Files edited',         value: fmt(totals.files_edited),   sub:'by AI agents' },
  ];
  document.getElementById('kpiStrip').innerHTML = kpis.map(k =>
    `<div class="kpi">
      <div class="label">${k.label}</div>
      <div class="value ${k.cls||''}">${k.value}</div>
      <div class="sub">${k.sub}</div>
    </div>`
  ).join('');

  // Cost breakdown chart
  const repoEntries = Object.entries(repos).map(([name, r], i) => ({
    name, cost: r.estimated_cost_usd || 0, ci: i
  }));
  if (_hiddenProjects.size === 0 && (unattr.estimated_cost_usd || 0) > 0) {
    repoEntries.push({ name: 'unattributed', cost: unattr.estimated_cost_usd, ci: repoEntries.length });
  }
  const maxCost = Math.max(...repoEntries.map(e => e.cost), 0.01);
  document.getElementById('chartWrap').innerHTML = repoEntries.map(e =>
    `<div class="chart-row">
      <div class="chart-label" title="${e.name}">${e.name}</div>
      <div class="chart-bar-track">
        <div class="chart-bar" style="width:${(e.cost/maxCost*100).toFixed(1)}%;background:${COLORS[e.ci%COLORS.length]}"></div>
      </div>
      <div class="chart-cost">${fmtCost(e.cost)}</div>
    </div>`
  ).join('');

  // Daily chart re-render with updated hidden set
  if (_lastDailyData) renderDailyChart(_lastDailyData, _hiddenProjects);

  // Repo cards
  document.getElementById('cardsGrid').innerHTML = Object.entries(repos).map(([name, r], i) => {
    const layers = (r.attribution_layers || []).map(attrTag).join('');
    const models = Object.keys(r.models || {}).map(m => {
      const label = (m === 'default' || m === 'unknown')
        ? 'Auto' : m.replace('claude-','').replace('gpt-','');
      return `<span class="tag model">${label}</span>`;
    }).join('');
    const cost = r.estimated_cost_usd > 0
      ? fmtCost(r.estimated_cost_usd)
      : '<span style="color:var(--muted)">n/a</span>';
    return `<div class="card">
      <div class="card-header">
        <div class="card-icon">&#128193;</div>
        <div>
          <div class="card-title">${name}</div>
          <div class="card-path">${r.path || ''}</div>
        </div>
      </div>
      <div class="card-cost">${cost} <small>estimated</small></div>
      <div class="stat-grid">
        <div class="stat"><div class="s-label">Conversations</div><div class="s-value">${fmt(r.conversations)}</div></div>
        <div class="stat"><div class="s-label">Requests</div><div class="s-value">${fmt(r.requests)}</div></div>
        <div class="stat"><div class="s-label">Input tokens</div><div class="s-value">${fmt(r.input_tokens)}</div></div>
        <div class="stat"><div class="s-label">Output tokens</div><div class="s-value">${fmt(r.output_tokens)}</div></div>
        <div class="stat"><div class="s-label">Files edited</div><div class="s-value">${fmt(r.files_edited)}</div></div>
        <div class="stat"><div class="s-label">Last active</div><div class="s-value" style="font-size:12px">${fmtDate(r.last_seen)}</div></div>
      </div>
      <div class="card-footer">
        ${layers}${models}
        ${r.first_seen ? `<span class="dates">${fmtDate(r.first_seen)}<br>&#8594; ${fmtDate(r.last_seen)}</span>` : ''}
      </div>
    </div>`;
  }).join('');

  // Unattributed
  const uConvs = unattr.conversations || 0;
  document.getElementById('unattribBadge').textContent = uConvs + ' conversations';
  document.getElementById('unattribBody').innerHTML = `
    <div>
      <span class="unattr-stat"><span class="s-label">Conversations</span><span class="s-value">${fmt(uConvs)}</span></span>
      <span class="unattr-stat"><span class="s-label">Requests</span><span class="s-value">${fmt(unattr.requests)}</span></span>
      <span class="unattr-stat"><span class="s-label">Input tokens</span><span class="s-value">${fmt(unattr.input_tokens)}</span></span>
      <span class="unattr-stat"><span class="s-label">Output tokens</span><span class="s-value">${fmt(unattr.output_tokens)}</span></span>
      <span class="unattr-stat"><span class="s-label">Estimated cost</span><span class="s-value" style="color:var(--green)">${fmtCost(unattr.estimated_cost_usd)}</span></span>
    </div>
    <p style="margin-top:12px;font-size:12px;color:var(--muted)">${unattr.note || ''}</p>`;

  // Notes + generated-at
  document.getElementById('costNote').textContent = data.cost_note || '';
  document.getElementById('genAt').textContent =
    data.generated_at ? 'Generated ' + new Date(data.generated_at).toLocaleString() : '';

  // Update print header meta
  const activeBtn = document.querySelector('.filter-btn.active');
  const period    = activeBtn ? activeBtn.textContent.trim() : 'All time';
  const hidInfo   = _hiddenProjects.size ? ` \u00b7 ${_hiddenProjects.size} project(s) hidden` : '';
  document.getElementById('printMeta').innerHTML =
    `Period: <strong>${period}</strong>${hidInfo}<br>` +
    `Projects: <strong>${Object.keys(repos).join(', ') || 'none'}</strong><br>` +
    `Generated: ${data.generated_at ? new Date(data.generated_at).toLocaleString() : '\u2014'}`;
}

function showToast(msg, type) {
  const t = document.getElementById('toast');
  t.textContent = msg;
  const cls = (type === 'ok' || type === 'success') ? 'success' : 'error';
  t.className = 'show ' + cls;
  clearTimeout(t._timer);
  t._timer = setTimeout(() => { t.className = ''; }, 4000);
}

async function loadData() {
  const res = await fetch('/api/data');
  if (!res.ok) throw new Error('Failed to load data');
  return res.json();
}

async function loadAndRender() {
  const data = await loadData();
  render(data);
  loadDailyData(); // non-blocking
}

function setAllBusy(busy) {
  document.getElementById('refreshBtn').disabled = busy;
  document.getElementById('refreshBtn').classList.toggle('spinning', busy);
  const upBtn = document.getElementById('updatePricesBtn');
  if (upBtn) upBtn.disabled = busy;
  document.querySelectorAll('.filter-btn').forEach(b => b.disabled = busy);
  const drApply = document.getElementById('drApplyBtn');
  if (drApply) drApply.disabled = busy;
  const drInputs = document.querySelectorAll('.date-range-picker input');
  drInputs.forEach(i => i.disabled = busy);
}

async function doRefresh() {
  setAllBusy(true);
  try {
    // Re-sync from Cursor, then re-apply the currently active filter
    await fetch('/api/refresh', { method: 'POST' });
    const active = document.querySelector('.filter-btn.active');
    const filterKey = active ? active.dataset.filter : 'all';
    // For custom range, re-use stored _customRange
    let payload;
    if (filterKey === 'custom' && _customRange) {
      payload = _customRange;
    } else if (filterKey === 'month') {
      const now = new Date();
      payload = now.getFullYear() + '-' + String(now.getMonth()+1).padStart(2,'0');
    } else {
      payload = filterKey;
    }
    const res = await fetch('/api/filter', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ filter: payload })
    });
    if (!res.ok) throw new Error('Refresh failed: ' + res.statusText);
    const data = await res.json();
    render(data);
    loadDailyData();
    showToast('Data refreshed', 'ok');
  } catch(e) {
    showToast(e.message, 'err');
  } finally {
    setAllBusy(false);
  }
}

async function doUpdatePrices() {
  const btn = document.getElementById('updatePricesBtn');
  btn.disabled = true;
  btn.classList.add('spinning');
  const origText = btn.innerHTML;
  btn.innerHTML = `<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M21 2v6h-6"/><path d="M3 12a9 9 0 0 1 15-6.7L21 8"/><path d="M3 22v-6h6"/><path d="M21 12a9 9 0 0 1-15 6.7L3 16"/></svg> Fetching prices\u2026`;
  try {
    const res = await fetch('/api/update-prices', { method: 'POST' });
    const data = await res.json();
    if (!res.ok || data.error) {
      showToast('Price update failed: ' + (data.error || res.statusText), 'err');
    } else if (data.changed) {
      const ap = data.auto_pool || {};
      const inRate = ap.input != null ? '$' + ap.input.toFixed(2) : '?';
      const outRate = ap.output != null ? '$' + ap.output.toFixed(2) : '?';
      const mCount = data.model_count != null ? `  \u2022  ${data.model_count} models` : '';
      showToast(`Prices updated \u2014 Auto: ${inRate} in / ${outRate} out${mCount}`, 'success');
      // Re-render with fresh data so rates dropdown updates
      await loadAndRender();
    } else {
      showToast('Prices already up to date', 'success');
    }
    updateSyncStatus();
  } catch(e) {
    showToast('Price update error: ' + e.message, 'err');
  } finally {
    btn.disabled = false;
    btn.classList.remove('spinning');
    btn.innerHTML = origText;
  }
}

async function applyFilter(btn) {
  document.querySelectorAll('.filter-btn').forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
  // Hide custom picker when a preset filter is chosen
  document.getElementById('dateRangePicker').classList.remove('open');
  const filter = btn.dataset.filter;
  setAllBusy(true);
  try {
    // For "This month" convert to YYYY-MM
    let payload = filter;
    if (filter === 'month') {
      const now = new Date();
      payload = now.getFullYear() + '-' + String(now.getMonth()+1).padStart(2,'0');
    }
    const res = await fetch('/api/filter', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ filter: payload })
    });
    if (!res.ok) throw new Error('Filter failed: ' + res.statusText);
    const data = await res.json();
    render(data);
    loadDailyData();
  } catch(e) {
    showToast(e.message, 'err');
  } finally {
    setAllBusy(false);
  }
}

function toggleCustomPicker(btn) {
  document.querySelectorAll('.filter-btn').forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
  const picker = document.getElementById('dateRangePicker');
  const opening = !picker.classList.contains('open');
  picker.classList.toggle('open', opening);
  if (opening) {
    // Pre-fill: 30 days ago → today
    const today = new Date();
    const from  = new Date(today);
    from.setDate(from.getDate() - 30);
    document.getElementById('drFrom').value  = from.toISOString().split('T')[0];
    document.getElementById('drUntil').value = today.toISOString().split('T')[0];
  }
}

async function applyCustomRange() {
  const fromVal  = document.getElementById('drFrom').value;
  const untilVal = document.getElementById('drUntil').value;
  if (!fromVal || !untilVal) { showToast('Please select both dates', 'err'); return; }
  if (fromVal > untilVal)    { showToast('From date must be before To date', 'err'); return; }
  _customRange = { from: fromVal, until: untilVal };
  setAllBusy(true);
  try {
    const res = await fetch('/api/filter', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ filter: _customRange })
    });
    if (!res.ok) throw new Error('Filter failed: ' + res.statusText);
    const data = await res.json();
    render(data);
    loadDailyData();
    document.getElementById('dateRangePicker').classList.remove('open');
  } catch(e) {
    showToast(e.message, 'err');
  } finally {
    setAllBusy(false);
  }
}

// initial load
loadAndRender().catch(e => showToast('Load error: ' + e.message, 'err'));

// ── auto-sync status polling ───────────────────────────────────────────────
function _fmtNext(isoStr) {
  if (!isoStr) return '';
  const diff = Math.round((new Date(isoStr) - Date.now()) / 1000);
  if (diff <= 0) return 'now';
  if (diff < 60)  return `in ${diff}s`;
  const m = Math.round(diff / 60);
  if (m < 60) return `in ${m} min`;
  const h = Math.round(diff / 3600);
  return `in ${h} h`;
}

function _fmtLast(isoStr) {
  if (!isoStr) return 'never';
  return timeAgo(new Date(isoStr));
}

async function updateSyncStatus() {
  try {
    const s = await fetch('/api/status').then(r => r.json());

    const dataActive  = s.data_sync_running;
    const priceActive = s.price_sync_running;

    document.getElementById('dataDot').className  = 'sync-dot' + (dataActive  ? ' active' : '');
    document.getElementById('priceDot').className = 'sync-dot' + (priceActive ? ' active' : '');

    const dataText = dataActive
      ? 'Syncing data\u2026'
      : `Data: synced ${_fmtLast(s.last_data_sync)} \u00b7 next ${_fmtNext(s.next_data_sync)} (every ${s.data_sync_interval_min} min)`;
    const priceText = priceActive
      ? 'Updating prices\u2026'
      : `Prices: updated ${_fmtLast(s.last_price_sync)} \u00b7 next ${_fmtNext(s.next_price_sync)} (every ${s.price_sync_interval_hrs} h)`;

    document.getElementById('dataSync').innerHTML  = dataText;
    document.getElementById('priceSync').innerHTML = priceText;

    // If data sync just completed, reload display data silently
    if (!dataActive && _lastDataSyncRunning) {
      loadAndRender().catch(() => {});
    }
    _lastDataSyncRunning = dataActive;
  } catch(e) { /* server might be momentarily busy */ }
}

let _lastDataSyncRunning = false;
updateSyncStatus();
setInterval(updateSyncStatus, 30000); // poll every 30 s
</script>
</body>
</html>"""


# ── HTTP handler ──────────────────────────────────────────────────────────────

def run_tracker(extra_args: list[str] | None = None) -> dict:
    """Run tracker.py (with optional extra args) and return the parsed JSON output."""
    cmd = [sys.executable, str(TRACKER)] + (extra_args or [])
    result = subprocess.run(cmd, capture_output=True, text=True, cwd=str(BASE_DIR))
    if result.returncode != 0:
        print(result.stderr, file=sys.stderr)
    return json.loads(DATA_FILE.read_text(encoding="utf-8"))


def _daily_data(from_date: str | None, until_date: str | None) -> dict:
    """
    Query history.db for per-day, per-repo estimated cost.

    Returns:
        {
          "days":  { "YYYY-MM-DD": { "repo_name": cost, ... }, ... },
          "repos": ["repo1", "repo2", ...]   (sorted by total cost desc)
        }
    """
    import sqlite3 as _sqlite3

    db_path = BASE_DIR / "history.db"
    if not db_path.exists():
        return {"days": {}, "repos": []}

    params: list = []
    where_clauses = ["estimated_cost_usd IS NOT NULL", "estimated_cost_usd > 0"]

    if from_date:
        # Convert YYYY-MM-DD → ms timestamp (start of day UTC)
        from datetime import datetime as _dt
        ts_ms = int(_dt.strptime(from_date, "%Y-%m-%d").replace(
            tzinfo=__import__("datetime").timezone.utc
        ).timestamp() * 1000)
        where_clauses.append("first_ts >= ?")
        params.append(ts_ms)

    if until_date:
        from datetime import datetime as _dt
        from datetime import timedelta as _td
        ts_ms = int((_dt.strptime(until_date, "%Y-%m-%d").replace(
            tzinfo=__import__("datetime").timezone.utc
        ) + _td(days=1)).timestamp() * 1000)
        where_clauses.append("first_ts < ?")
        params.append(ts_ms)

    where = " AND ".join(where_clauses)

    con = _sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, check_same_thread=False)
    try:
        rows = con.execute(f"""
            SELECT
                date(first_ts / 1000, 'unixepoch') AS day,
                COALESCE(repo_path, '__unattributed__') AS repo,
                SUM(estimated_cost_usd) AS cost
            FROM conversations
            WHERE {where}
            GROUP BY day, repo
            ORDER BY day, repo
        """, params).fetchall()
    finally:
        con.close()

    days: dict[str, dict[str, float]] = {}
    repo_totals: dict[str, float] = {}
    for day, repo, cost in rows:
        if repo in ("unattributed", "__unattributed__", None):
            repo_name = "unattributed"
        else:
            repo_name = repo.replace("\\", "/").rstrip("/").split("/")[-1]
        prev = days.setdefault(day, {}).get(repo_name, 0)
        days[day][repo_name] = round(prev + cost, 6)
        repo_totals[repo_name] = round(repo_totals.get(repo_name, 0) + cost, 6)

    repos_sorted = sorted(repo_totals, key=lambda r: repo_totals[r], reverse=True)
    return {"days": days, "repos": repos_sorted}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _in_seconds_iso(secs: int) -> str:
    from datetime import timedelta
    return (datetime.now(timezone.utc) + timedelta(seconds=secs)).isoformat()


def _data_sync_loop() -> None:
    """Background thread: re-sync Cursor data every DATA_SYNC_INTERVAL seconds."""
    while True:
        time.sleep(DATA_SYNC_INTERVAL)
        _sched["data_sync_running"] = True
        _sched["next_data_sync"] = None
        print(f"[scheduler] Auto data sync starting …")
        try:
            run_tracker()
            _sched["last_data_sync"] = _now_iso()
            print(f"[scheduler] Auto data sync done.")
        except Exception as exc:
            print(f"[scheduler] Auto data sync failed: {exc}")
        finally:
            _sched["data_sync_running"] = False
            _sched["next_data_sync"] = _in_seconds_iso(DATA_SYNC_INTERVAL)


def _price_sync_loop(skip_first: bool = False) -> None:
    """Background thread: re-fetch Cursor pricing every PRICE_SYNC_INTERVAL seconds."""
    if skip_first:
        time.sleep(PRICE_SYNC_INTERVAL)
    while True:
        _sched["price_sync_running"] = True
        _sched["next_price_sync"] = None
        print(f"[scheduler] Auto price sync starting …")
        try:
            result = subprocess.run(
                [sys.executable, str(TRACKER), "--prices-only"],
                capture_output=True, text=True, cwd=str(BASE_DIR), timeout=90,
            )
            if result.returncode == 0:
                info = json.loads(result.stdout)
                _sched["last_price_sync"] = _now_iso()
                changed = info.get("changed", False)
                count = info.get("model_count", "?")
                print(f"[scheduler] Auto price sync done (changed={changed}, models={count}).")
            else:
                print(f"[scheduler] Auto price sync exited {result.returncode}: {result.stderr.strip()}")
        except Exception as exc:
            print(f"[scheduler] Auto price sync failed: {exc}")
        finally:
            _sched["price_sync_running"] = False
            _sched["next_price_sync"] = _in_seconds_iso(PRICE_SYNC_INTERVAL)
        time.sleep(PRICE_SYNC_INTERVAL)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        # Suppress per-request access log — only errors shown
        if args and str(args[1]) not in ("200", "304"):
            super().log_message(fmt, *args)

    def _send(self, status: int, content_type: str, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            self._send(200, "text/html; charset=utf-8", HTML.encode())
        elif self.path == "/api/data":
            if DATA_FILE.exists():
                self._send(200, "application/json", DATA_FILE.read_bytes())
            else:
                self._send(404, "application/json", b'{"error":"cursor-usage.json not found"}')
        elif self.path == "/api/status":
            body = json.dumps({
                **_sched,
                "data_sync_interval_min":  DATA_SYNC_INTERVAL  // 60,
                "price_sync_interval_hrs": PRICE_SYNC_INTERVAL // 3600,
            }, ensure_ascii=False).encode()
            self._send(200, "application/json", body)
        else:
            self._send(404, "text/plain", b"Not found")

    def do_POST(self):
        if self.path == "/api/refresh":
            try:
                data = run_tracker()
                body = json.dumps(data, ensure_ascii=False).encode()
                self._send(200, "application/json", body)
            except Exception as exc:
                err = json.dumps({"error": str(exc)}).encode()
                self._send(500, "application/json", err)
        elif self.path == "/api/filter":
            try:
                length = int(self.headers.get("Content-Length", 0))
                payload = json.loads(self.rfile.read(length))
                extra: list[str] = []
                f = payload.get("filter", "all")
                if isinstance(f, dict):
                    # Custom date range: { from: "YYYY-MM-DD", until: "YYYY-MM-DD" }
                    if f.get("from"):
                        extra += ["--from", f["from"]]
                    if f.get("until"):
                        extra += ["--until", f["until"]]
                elif f == "all":
                    pass  # no extra args = all-time
                elif isinstance(f, int) or (isinstance(f, str) and f.isdigit()):
                    extra = ["--last", str(f)]
                elif isinstance(f, str) and f.startswith("last"):
                    # e.g. "last7", "last30"
                    days = f.replace("last", "").strip()
                    if days.isdigit():
                        extra = ["--last", days]
                elif isinstance(f, str) and len(f) == 7 and f[4] == "-":
                    # YYYY-MM
                    extra = ["--since", f]
                data = run_tracker(extra)
                body = json.dumps(data, ensure_ascii=False).encode()
                self._send(200, "application/json", body)
            except Exception as exc:
                err = json.dumps({"error": str(exc)}).encode()
                self._send(400, "application/json", err)
        elif self.path == "/api/daily":
            try:
                length = int(self.headers.get("Content-Length", 0))
                payload = json.loads(self.rfile.read(length)) if length else {}
                body = json.dumps(
                    _daily_data(payload.get("from"), payload.get("until")),
                    ensure_ascii=False,
                ).encode()
                self._send(200, "application/json", body)
            except Exception as exc:
                self._send(500, "application/json",
                           json.dumps({"error": str(exc)}).encode())
        elif self.path == "/api/update-prices":
            try:
                result = subprocess.run(
                    [sys.executable, str(TRACKER), "--prices-only"],
                    capture_output=True, text=True, cwd=str(BASE_DIR),
                    timeout=60,
                )
                if result.returncode != 0:
                    err_msg = result.stderr.strip() or "tracker exited with code " + str(result.returncode)
                    self._send(500, "application/json",
                               json.dumps({"error": err_msg}).encode())
                    return
                payload = json.loads(result.stdout)
                self._send(200, "application/json",
                           json.dumps(payload, ensure_ascii=False).encode())
            except subprocess.TimeoutExpired:
                self._send(504, "application/json",
                           b'{"error":"Price fetch timed out (>60 s)"}')
            except Exception as exc:
                self._send(500, "application/json",
                           json.dumps({"error": str(exc)}).encode())
        else:
            self._send(404, "text/plain", b"Not found")


# ── entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Cursor usage dashboard")
    parser.add_argument("--no-refresh", action="store_true", help="Skip initial tracker.py run")
    parser.add_argument("--port", type=int, default=8765, help="Port to serve on (default 8765)")
    args = parser.parse_args()

    if not args.no_refresh:
        print("[dashboard] Syncing and refreshing data ...")
        try:
            run_tracker()
            _sched["last_data_sync"] = _now_iso()
            print("[dashboard] Data refreshed.")
        except Exception as exc:
            print(f"[dashboard] Warning: tracker failed ({exc}). Serving existing data.")

    # Initialise next-sync timestamps
    _sched["next_data_sync"]  = _in_seconds_iso(DATA_SYNC_INTERVAL)
    _sched["next_price_sync"] = _in_seconds_iso(PRICE_SYNC_INTERVAL)

    # Background scheduler threads
    threading.Thread(target=_data_sync_loop,  daemon=True, name="data-sync").start()
    # Price sync: run immediately on first tick (skip_first=False) only if we
    # haven't already fetched prices at startup (prices.json exists).
    prices_exist = (BASE_DIR / "prices.json").exists()
    threading.Thread(
        target=_price_sync_loop,
        kwargs={"skip_first": prices_exist},
        daemon=True,
        name="price-sync",
    ).start()
    if prices_exist:
        _sched["last_price_sync"] = _now_iso()
        print(f"[dashboard] Auto data sync every {DATA_SYNC_INTERVAL//60} min, "
              f"price sync every {PRICE_SYNC_INTERVAL//3600} h.")
    else:
        print(f"[dashboard] Auto data sync every {DATA_SYNC_INTERVAL//60} min, "
              f"price sync every {PRICE_SYNC_INTERVAL//3600} h (first run immediately).")

    server = HTTPServer(("127.0.0.1", args.port), Handler)
    url = f"http://localhost:{args.port}"
    print(f"[dashboard] Serving at {url}")
    print("[dashboard] Press Ctrl-C to stop.")

    # Open browser after a short delay so the server is ready
    def _open():
        time.sleep(0.4)
        webbrowser.open(url)

    threading.Thread(target=_open, daemon=True).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[dashboard] Stopped.")


if __name__ == "__main__":
    main()
