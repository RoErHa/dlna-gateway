#!/usr/bin/env python3
"""
dlna_gateway.py — Entry point. Wires all modules together and starts the server.

Usage:
    python dlna_gateway.py [--host 0.0.0.0] [--port 8765]
                           [--probe http://<ip>:<port>/desc.xml]
                           [--debug] [--no-browser]

Test individual modules:
    python dlna_config.py
    python dlna_discovery.py [http://probe-url]
    python dlna_content.py <control-url>
    python dlna_library.py
    python dlna_player.py [http://test-url]
    python dlna_server.py
"""
import argparse
import logging
import os
import socket
import ssl
import subprocess
import threading
import time

import dlna_discovery as _disc
from dlna_cast import start_discovery as _cast_start, stop_discovery as _cast_stop
from dlna_config import load_config, save_config, setup_logging
from dlna_library import DB, INDEXER, DEVICE_ROLES
from dlna_server import (GW_UDN, ThreadedHTTPServer, GatewayHandler,
                         gw_ssdp_announcer, gw_ssdp_byebye)

log = logging.getLogger("dlna.gateway")


# ── Web UI ────────────────────────────────────────────────────────
# Imported by dlna_server.GatewayHandler when serving GET /

WEB_UI = """<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="apple-mobile-web-app-title" content="DLNA GW">
<meta name="theme-color" content="#0e0d0b">
<link rel="manifest" href="/manifest.json">
<link rel="apple-touch-icon" href="/icon-192.png">
<title>DLNA Gateway</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=DM+Mono:wght@300;400;500&family=Syne:wght@400;600;700;800&display=swap" rel="stylesheet">
<style>
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
:root{--ink:#f2ede4;--ink-dim:#9a907f;--bg:#0e0d0b;--surface:#161410;--raised:#1f1d19;--border:#2a2720;--amber:#d4a843;--amber-d:#a07820;--red:#c84b3c;--green:#4a9c6d;--r:6px;--fh:'Syne',sans-serif;--fm:'DM Mono',monospace}
html,body{height:100%;overflow:hidden}
body{background:var(--bg);color:var(--ink);font-family:var(--fm);font-size:13px;display:flex;flex-direction:column}
header{display:flex;align-items:center;gap:12px;padding:0 16px;height:52px;background:var(--surface);border-bottom:1px solid var(--border);flex-shrink:0}
.logo{font-family:var(--fh);font-weight:800;font-size:15px;letter-spacing:-.03em;color:var(--amber)}
.logo sup{font-size:9px;color:var(--ink-dim);vertical-align:super}
.sep{width:1px;height:24px;background:var(--border)}
.disc-wrap{display:flex;align-items:center;gap:7px}
.disc-dot{width:7px;height:7px;border-radius:50%;background:var(--border);transition:background .4s,box-shadow .4s}
.disc-dot.on{background:var(--green);box-shadow:0 0 8px var(--green)}
.disc-label{font-size:11px;color:var(--ink-dim)}
#server-sel{flex:1;max-width:260px;background:var(--raised);border:1px solid var(--border);color:var(--ink);font-family:var(--fm);font-size:12px;padding:5px 8px;border-radius:var(--r);cursor:pointer;outline:none}
#server-sel:focus{border-color:var(--amber)}
/* search bar in header */
.search-wrap{display:flex;align-items:center;gap:6px;flex:1;max-width:300px}
#search-input{flex:1;background:var(--raised);border:1px solid var(--border);color:var(--ink);font-family:var(--fm);font-size:12px;padding:5px 10px;border-radius:var(--r);outline:none}
#search-input:focus{border-color:var(--amber)}
#search-input::placeholder{color:var(--ink-dim)}
.hdr-right{margin-left:auto;font-size:11px;color:var(--ink-dim);max-width:200px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
/* tab bar under header */
.tab-bar{display:flex;gap:0;background:var(--surface);border-bottom:1px solid var(--border);flex-shrink:0;padding:0 16px}
.tab{padding:7px 16px;font-size:11px;cursor:pointer;color:var(--ink-dim);border-bottom:2px solid transparent;transition:color .15s,border-color .15s}
.tab.active{color:var(--amber);border-bottom-color:var(--amber)}
.tab:hover{color:var(--ink)}
.workspace{display:flex;flex:1;overflow:hidden}
#browser{width:360px;min-width:240px;flex-shrink:0;display:flex;flex-direction:column;background:var(--surface);border-right:1px solid var(--border)}
#breadcrumb{display:flex;align-items:center;gap:4px;padding:7px 14px;min-height:34px;border-bottom:1px solid var(--border);flex-wrap:wrap;flex-shrink:0}
.crumb{color:var(--amber);cursor:pointer;font-size:11px}
.crumb:hover{text-decoration:underline}
.crumb-sep{color:var(--ink-dim);font-size:10px}
.crumb-cur{font-size:11px;color:var(--ink-dim);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:200px}
#item-list{flex:1;overflow-y:auto}
#item-list::-webkit-scrollbar{width:5px}
#item-list::-webkit-scrollbar-thumb{background:var(--border);border-radius:3px}
.row{display:flex;align-items:center;gap:10px;padding:8px 14px;border-bottom:1px solid var(--border);cursor:pointer;transition:background .1s;position:relative}
.row:hover{background:var(--raised)}
.row.active{background:rgba(212,168,67,.08)}
.row.active::before{content:'';position:absolute;left:0;top:0;bottom:0;width:2px;background:var(--amber)}
.row-icon{font-size:15px;width:20px;text-align:center;flex-shrink:0}
.row-body{flex:1;min-width:0}
.row-title{white-space:nowrap;overflow:hidden;text-overflow:ellipsis;font-size:12.5px}
.row-sub{font-size:11px;color:var(--ink-dim);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;margin-top:1px}
.row-dur{font-size:11px;color:var(--ink-dim);flex-shrink:0;font-variant-numeric:tabular-nums}
.row-actions{display:flex;gap:4px;flex-shrink:0;opacity:0;transition:opacity .15s}
.row:hover .row-actions{opacity:1}
.icon-btn{background:none;border:1px solid var(--border);border-radius:4px;color:var(--ink-dim);cursor:pointer;font-size:11px;padding:3px 6px;line-height:1;transition:color .1s,background .1s}
.icon-btn:hover{color:var(--ink);background:var(--raised)}
.icon-btn.fav{color:var(--amber)}
.msg{text-align:center;color:var(--ink-dim);padding:36px 20px;font-size:12px;line-height:1.8}
.msg code{color:var(--amber);font-size:11px}
.spinner-wrap{display:flex;justify-content:center;padding:36px}
.spinner{width:26px;height:26px;border:2px solid var(--border);border-top-color:var(--amber);border-radius:50%;animation:spin .7s linear infinite}
@keyframes spin{to{transform:rotate(360deg)}}
/* player panel */
#player{flex:1;display:flex;flex-direction:column;align-items:center;justify-content:center;padding:28px 40px 36px;background:var(--bg);position:relative;overflow:hidden}
#player::before{content:'';position:absolute;top:10%;left:50%;transform:translateX(-50%);width:300px;height:300px;border-radius:50%;background:radial-gradient(circle,rgba(212,168,67,.07) 0%,transparent 70%);pointer-events:none;opacity:0;transition:opacity .8s}
#player.playing::before{opacity:1}
.art-wrap{width:160px;height:160px;margin-bottom:20px}
.art{width:100%;height:100%;border-radius:12px;background:var(--raised);border:1px solid var(--border);display:flex;align-items:center;justify-content:center;font-size:48px;box-shadow:0 20px 50px rgba(0,0,0,.6);transition:box-shadow .5s;overflow:hidden}
#player.playing .art{box-shadow:0 20px 70px rgba(212,168,67,.18),0 6px 24px rgba(0,0,0,.6)}
#player.playing.is-audio .art{animation:vinyl 8s linear infinite}
@keyframes vinyl{to{transform:rotate(360deg)}}
.np-title{font-family:var(--fh);font-size:17px;font-weight:700;text-align:center;max-width:420px;letter-spacing:-.02em;margin-bottom:5px;line-height:1.2}
.np-meta{font-size:12px;color:var(--ink-dim);text-align:center;margin-bottom:20px;max-width:380px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.seek-section{width:100%;max-width:400px;margin-bottom:16px}
.seek-track{width:100%;position:relative;height:4px;background:var(--raised);border-radius:2px;cursor:pointer;margin-bottom:8px}
.seek-fill{height:100%;background:var(--amber);border-radius:2px;width:0%;pointer-events:none;transition:width .5s linear}
.seek-track.dragging .seek-fill{transition:none}
.seek-thumb{position:absolute;top:50%;transform:translate(-50%,-50%);width:12px;height:12px;border-radius:50%;background:var(--amber);pointer-events:none;opacity:0;transition:opacity .2s;left:0%}
.seek-track:hover .seek-thumb{opacity:1}
.seek-times{display:flex;justify-content:space-between;font-size:11px;color:var(--ink-dim);font-variant-numeric:tabular-nums}
.controls{display:flex;align-items:center;gap:8px;margin-bottom:16px}
.btn{display:flex;align-items:center;justify-content:center;background:none;border:1px solid var(--border);border-radius:var(--r);color:var(--ink-dim);cursor:pointer;transition:color .15s,background .15s,border-color .15s;font-size:13px;padding:7px 12px;font-family:var(--fm)}
.btn:hover{color:var(--ink);background:var(--raised);border-color:var(--amber-d)}
.btn.primary{background:var(--amber);border-color:var(--amber);color:#000;font-weight:600;padding:8px 20px;font-size:14px}
.btn.primary:hover{background:#e8b84e}
.vol-row{display:flex;align-items:center;gap:8px;color:var(--ink-dim);font-size:11px}
input[type=range].vol{width:100px;-webkit-appearance:none;height:3px;background:var(--border);border-radius:2px;outline:none;cursor:pointer}
input[type=range].vol::-webkit-slider-thumb{-webkit-appearance:none;width:11px;height:11px;border-radius:50%;background:var(--ink-dim);cursor:pointer}
input[type=range].vol:hover::-webkit-slider-thumb{background:var(--amber)}
/* playlists panel */
#pl-panel{width:260px;flex-shrink:0;display:flex;flex-direction:column;background:var(--surface);border-left:1px solid var(--border);overflow:hidden}
.pl-header{display:flex;align-items:center;justify-content:space-between;padding:8px 14px;border-bottom:1px solid var(--border);flex-shrink:0}
.pl-header-title{font-family:var(--fh);font-size:12px;font-weight:600;color:var(--ink)}
.pl-list{flex:1;overflow-y:auto}
.pl-list::-webkit-scrollbar{width:4px}
.pl-list::-webkit-scrollbar-thumb{background:var(--border);border-radius:3px}
.pl-item{padding:8px 14px;border-bottom:1px solid var(--border);cursor:pointer;transition:background .1s}
.pl-item:hover{background:var(--raised)}
.pl-item.active{background:rgba(212,168,67,.07)}
.pl-item-name{font-size:12px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.pl-item-count{font-size:11px;color:var(--ink-dim);margin-top:2px}
.pl-actions{display:flex;gap:6px;padding:10px 14px;border-bottom:1px solid var(--border);flex-shrink:0;flex-wrap:wrap}
.pl-tracks{flex:1;overflow-y:auto;display:none}
.pl-tracks.visible{display:block}
.pl-track{display:flex;align-items:center;gap:8px;padding:7px 14px;border-bottom:1px solid var(--border);font-size:12px}
.pl-track-body{flex:1;min-width:0}
.pl-track-title{white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.pl-track-sub{font-size:11px;color:var(--ink-dim)}
/* statusbar */
#statusbar{height:26px;background:var(--surface);border-top:1px solid var(--border);display:flex;align-items:center;padding:0 16px;gap:16px;flex-shrink:0}
.sb-item{font-size:11px;color:var(--ink-dim)}
.sb-item span{color:var(--ink)}
.sb-dot{width:5px;height:5px;border-radius:50%;background:var(--border);flex-shrink:0}
.sb-dot.playing{background:var(--green)}
.sb-dot.paused{background:var(--amber)}
#toast{position:fixed;bottom:36px;left:50%;transform:translateX(-50%) translateY(10px);background:var(--raised);border:1px solid var(--border);color:var(--ink);font-size:12px;padding:8px 18px;border-radius:20px;opacity:0;transition:opacity .25s,transform .25s;pointer-events:none;z-index:100}
#toast.show{opacity:1;transform:translateX(-50%) translateY(0)}
/* add-to-playlist dropdown */
.pl-dropdown{position:fixed;background:var(--raised);border:1px solid var(--border);border-radius:var(--r);z-index:200;min-width:180px;box-shadow:0 8px 24px rgba(0,0,0,.5);padding:4px 0}
.pl-dropdown-item{padding:7px 14px;font-size:12px;cursor:pointer;white-space:nowrap;transition:background .1s}
.pl-dropdown-item:hover{background:var(--border)}

/* ── Mini player (mobile, shown when playing) ─────────────────── */
#mini-player{display:none;position:fixed;bottom:56px;left:0;right:0;background:var(--surface);border-top:1px solid var(--border);padding:8px 14px;flex-direction:row;align-items:center;gap:10px;z-index:50;cursor:pointer}
#mini-art{width:38px;height:38px;border-radius:5px;background:var(--raised);border:1px solid var(--border);display:flex;align-items:center;justify-content:center;font-size:18px;flex-shrink:0;overflow:hidden}
#mini-info{flex:1;min-width:0}
#mini-title{font-size:12.5px;font-family:var(--fh);font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
#mini-artist{font-size:11px;color:var(--ink-dim);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
#mini-pp{padding:5px 12px;flex-shrink:0;font-size:16px}
#mini-next{padding:5px 10px;flex-shrink:0}
/* progress stripe under mini player */
#mini-progress{position:absolute;bottom:0;left:0;height:2px;background:var(--amber);width:0%;transition:width 1s linear;pointer-events:none}

/* ── Bottom nav (mobile) ──────────────────────────────────────── */
#bottom-nav{display:none;position:fixed;bottom:0;left:0;right:0;height:56px;background:var(--surface);border-top:1px solid var(--border);z-index:60;align-items:stretch}
.bnav-btn{flex:1;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:2px;background:none;border:none;color:var(--ink-dim);cursor:pointer;padding:4px 0;font-family:var(--fm);transition:color .15s}
.bnav-btn.active{color:var(--amber)}
.bnav-btn span:first-child{font-size:18px;line-height:1}
.bnav-btn span:last-child{font-size:9px;letter-spacing:.03em}

/* ── Mobile responsive ────────────────────────────────────────── */
@media (max-width:768px){
  /* ── Base ──────────────────────────────────────────────────── */
  html,body{overflow:auto;height:auto}
  body{padding-bottom:116px;font-size:14px}

  /* ── Header ────────────────────────────────────────────────── */
  header{flex-wrap:wrap;height:auto;padding:10px 14px;gap:8px}
  .sep{display:none}
  .hdr-right{display:none}
  /* Server selector full width */
  #server-sel{max-width:100%;flex:1;font-size:13px;padding:8px 10px}
  /* Output selector compact next to server selector */
  #output-sel{font-size:13px;padding:8px 10px}
  /* Search bar full width on second row */
  .search-wrap{max-width:100%;flex:0 0 100%;order:10}
  #search-input{padding:10px 14px;font-size:14px;border-radius:20px}

  /* ── Hide desktop chrome ───────────────────────────────────── */
  .tab-bar{display:none}
  #statusbar{display:none}

  /* ── Layout ────────────────────────────────────────────────── */
  .workspace{flex-direction:column;overflow:visible}
  #browser{width:100%;min-width:unset;border-right:none;flex-shrink:0}
  #pl-panel{width:100%;border-left:none}
  #player{display:none;flex-direction:column;padding:24px 20px 32px}

  /* ── Touch-friendly list rows ──────────────────────────────── */
  .row{padding:13px 16px;min-height:52px}
  .row-title{font-size:14px}
  .row-sub{font-size:12px;margin-top:3px}
  .row-dur{font-size:12px}
  /* Always show action buttons on mobile (no hover) */
  .row-actions{opacity:1}
  .icon-btn{padding:6px 10px;font-size:13px}

  /* ── Mini player ───────────────────────────────────────────── */
  #mini-player{display:flex;padding:10px 16px;bottom:60px}
  #mini-art{width:44px;height:44px;border-radius:6px;font-size:20px}
  #mini-title{font-size:13.5px}
  #mini-artist{font-size:12px}
  #mini-pp{padding:8px 14px;font-size:18px}
  #mini-next{padding:8px 12px;font-size:18px}

  /* ── Bottom nav ────────────────────────────────────────────── */
  #bottom-nav{display:flex;height:60px}
  .bnav-btn span:first-child{font-size:20px}
  .bnav-btn span:last-child{font-size:10px;letter-spacing:.02em}

  /* ── Now Playing (full screen) ─────────────────────────────── */
  body.m-np #player{
    display:flex;position:fixed;top:0;left:0;right:0;bottom:60px;
    z-index:40;overflow-y:auto;background:var(--bg);
    padding:32px 24px 24px;justify-content:flex-start;gap:0
  }
  body.m-np #mini-player{display:none}
  /* Large art */
  body.m-np .art-wrap{width:min(280px,75vw);height:min(280px,75vw);margin:0 auto 24px}
  /* Title/meta spacing */
  body.m-np .np-title{font-size:20px;margin-bottom:8px}
  body.m-np #np-artist{font-size:13px;margin-bottom:4px}
  body.m-np #np-album{font-size:12px;margin-bottom:8px}
  body.m-np .np-meta{font-size:12px;margin-bottom:20px}
  /* Seek bar — fat and easy to tap */
  body.m-np .seek-section{width:100%;max-width:100%;margin-bottom:20px}
  .seek-track{height:6px;border-radius:3px;margin-bottom:10px}
  .seek-thumb{width:20px;height:20px;opacity:1}
  .seek-times{font-size:12px}
  /* Controls — big tap targets in a single row */
  body.m-np .controls{
    display:grid;grid-template-columns:repeat(5,1fr);
    gap:8px;width:100%;margin-bottom:18px
  }
  .btn{padding:12px 10px;font-size:15px;border-radius:10px}
  .btn.primary{padding:14px 10px;font-size:16px}
  /* Volume row */
  body.m-np .vol-row{gap:10px;font-size:13px}
  input[type=range].vol{width:160px;height:4px}

  /* ── Browse panel ───────────────────────────────────────────── */
  body.m-browse #browser{display:flex;flex-direction:column}
  body.m-browse #pl-panel{display:none}
  #breadcrumb{padding:10px 16px;min-height:40px}
  .crumb{font-size:12px}
  .crumb-cur{font-size:12px}

  /* ── Playlists / Favourites panel ──────────────────────────── */
  body.m-pl #pl-panel{display:flex;flex-direction:column;min-height:60vh}
  body.m-pl #browser{display:none}
  body.m-fav #pl-panel{display:flex;flex-direction:column;min-height:60vh}
  body.m-fav #browser{display:none}
  .pl-item{padding:13px 16px;min-height:52px}
  .pl-item-name{font-size:14px}
  .pl-item-count{font-size:12px;margin-top:3px}
  .pl-track{padding:11px 16px;min-height:48px}
  .pl-track-title{font-size:13.5px}
  .pl-track-sub{font-size:11.5px}
  .pl-actions{padding:12px 16px;gap:10px}
  .pl-header{padding:12px 16px}
  .pl-header-title{font-size:14px}

  /* ── Search view ────────────────────────────────────────────── */
  body.m-search #browser{display:flex;flex-direction:column}
  body.m-search #pl-panel{display:none}
}

/* ── Safe area for iPhone notch / home indicator ───────────────── */
@supports(padding-bottom:env(safe-area-inset-bottom)){
  @media(max-width:768px){
    #bottom-nav{padding-bottom:env(safe-area-inset-bottom)}
    body{padding-bottom:calc(116px + env(safe-area-inset-bottom))}
  }
}
</style>
</head>
<body>
<header>
  <div class="logo">DLNA<sup>GW</sup></div>
  <div class="sep"></div>
  <div class="disc-wrap">
    <div class="disc-dot" id="disc-dot"></div>
    <div class="disc-label" id="disc-label">Scanning…</div>
  </div>
  <div class="sep"></div>
  <select id="server-sel"><option value="">— scanning… —</option></select>
  <div class="sep"></div>
  <div style="display:flex;align-items:center;gap:6px;flex-shrink:0">
    <span style="font-size:10px;color:var(--ink-dim);letter-spacing:.05em">OUT</span>
    <select id="output-sel" style="background:var(--raised);border:1px solid var(--border);color:var(--ink);font-family:var(--fm);font-size:12px;padding:5px 8px;border-radius:var(--r);cursor:pointer;outline:none">
      <option value="browser" selected>📱 Browser</option>
      <option value="iina">🖥 IINA (local)</option>
    </select>
  </div>
  <div class="sep"></div>
  <div class="search-wrap">
    <input type="text" id="search-input" placeholder="🔍 Search library…" autocomplete="off">
  </div>
  <div class="hdr-right" id="hdr-status">Ready</div>
</header>
<div class="tab-bar">
  <div class="tab active" id="tab-browse" onclick="showTab('browse')">Browse</div>
  <div class="tab" id="tab-search" onclick="showTab('search')">Search</div>
  <div class="tab" id="tab-playlists" onclick="showTab('playlists')">Playlists</div>
  <div class="tab" id="tab-favourites" onclick="showTab('favourites')">⭐ Favourites</div>
</div>
<div class="workspace">
  <!-- LEFT: browser / search / playlist view -->
  <div id="browser">
    <div id="breadcrumb"><span class="crumb-cur">Select a server above</span></div>
    <div id="index-bar" style="display:none;padding:7px 14px;border-bottom:1px solid var(--border);background:var(--raised);flex-shrink:0">
      <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:4px">
        <span style="font-size:11px;color:var(--ink-dim)" id="index-label">Indexing library…</span>
        <button class="icon-btn" style="font-size:10px" onclick="reindex()">↺ Rebuild</button>
      </div>
      <div style="height:3px;background:var(--border);border-radius:2px;overflow:hidden">
        <div id="index-progress-bar" style="height:100%;background:var(--amber);border-radius:2px;width:0%;transition:width .3s"></div>
      </div>
    </div>
    <div id="item-list">
      <div class="msg">Scanning for DLNA servers…<br>Usually 5–15 s via SSDP,<br>up to 30 s via subnet scan.<br><br><code>AssetUPnP · Plex · Jellyfin · MinimServer</code></div>
    </div>
  </div>
  <!-- CENTRE: player -->
  <div id="player">
    <!-- Back button — mobile only, closes Now Playing fullscreen -->
    <button id="np-back" onclick="mobileTab('browse')" style="display:none;position:absolute;top:14px;left:16px;background:none;border:none;color:var(--ink-dim);font-size:22px;cursor:pointer;z-index:10;padding:4px 8px">‹</button>
    <div class="art-wrap"><div class="art" id="art">🎵</div></div>
    <div class="np-title" id="np-title">Nothing playing</div>
    <div class="np-artist" id="np-artist" style="font-size:11px;color:var(--amber);text-align:center;margin-bottom:2px;max-width:380px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;min-height:16px"></div>
    <div id="np-album" style="font-size:11px;color:var(--ink-dim);text-align:center;margin-bottom:4px;max-width:380px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;min-height:15px;font-style:italic"></div>
    <div class="np-meta"  id="np-meta">Browse or search your library</div>
    <div class="seek-section">
      <div class="seek-track" id="seek-track">
        <div class="seek-fill" id="seek-fill"></div>
        <div class="seek-thumb" id="seek-thumb"></div>
      </div>
      <div class="seek-times"><span id="t-pos">0:00</span><span id="t-dur">0:00</span></div>
    </div>
    <div class="controls">
      <button class="btn" id="btn-prev">⏮</button>
      <button class="btn" id="btn-rew">« 30s</button>
      <button class="btn" id="btn-stop">⏹ Stop</button>
      <button class="btn primary" id="btn-pp">▶ Play</button>
      <button class="btn" id="btn-fwd">30s »</button>
      <button class="btn" id="btn-next">⏭</button>
    </div>
    <div class="vol-row">
      <span>🔈</span>
      <input type="range" class="vol" id="vol" min="0" max="100" value="80">
      <span>🔊</span>
      <span id="vol-label" style="min-width:26px;font-variant-numeric:tabular-nums">80</span>
      <div class="sep" style="margin:0 8px"></div>
      <button class="btn" id="btn-shuffle" title="Toggle shuffle">🔀</button>
    </div>
  </div>
  <!-- RIGHT: playlists panel -->
  <div id="pl-panel">
    <div class="pl-header">
      <span class="pl-header-title" id="pl-panel-title">Playlists</span>
      <button class="icon-btn" id="pl-back-btn" onclick="showPlaylists()" style="display:none">← back</button>
    </div>
    <div class="pl-actions" id="pl-actions">
      <button class="btn" style="font-size:11px;padding:5px 10px" onclick="newPlaylist()">+ New playlist</button>
    </div>
    <div class="pl-list" id="pl-list"></div>
    <div class="pl-tracks" id="pl-tracks"></div>
  </div>
</div>
<div id="statusbar">
  <div class="sb-dot" id="sb-dot"></div>
  <div class="sb-item">State: <span id="sb-state">stopped</span></div>
  <div class="sb-item" style="flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">Track: <span id="sb-uri">—</span></div>
</div>
<div id="toast"></div>

<!-- Hidden audio element for 📱 Browser output -->
<audio id="browser-audio" preload="auto"></audio>

<!-- Mini player — mobile only, shown when playing -->
<div id="mini-player" onclick="mobileTab('nowplaying');$('bnav-nowplaying').classList.add('active')">
  <div id="mini-art">🎵</div>
  <div id="mini-info">
    <div id="mini-title">Nothing playing</div>
    <div id="mini-artist"></div>
  </div>
  <button class="btn" id="mini-pp" onclick="event.stopPropagation();control({action:'pause'})">⏸</button>
  <button class="btn" id="mini-next" onclick="event.stopPropagation();control({action:'next'})">⏭</button>
  <div id="mini-progress"></div>
</div>

<!-- Bottom navigation — mobile only -->
<nav id="bottom-nav">
  <button class="bnav-btn active" id="bnav-browse"      onclick="mobileTab('browse')"><span>📁</span><span>Browse</span></button>
  <button class="bnav-btn"        id="bnav-search"      onclick="mobileTab('search')"><span>🔍</span><span>Search</span></button>
  <button class="bnav-btn"        id="bnav-playlists"   onclick="mobileTab('playlists')"><span>📋</span><span>Playlists</span></button>
  <button class="bnav-btn"        id="bnav-favourites"  onclick="mobileTab('favourites')"><span>⭐</span><span>Favs</span></button>
  <button class="bnav-btn"        id="bnav-nowplaying"  onclick="mobileTab('nowplaying')"><span id="bnav-np-icon">🎵</span><span>Playing</span></button>
</nav>

<script>
const $=id=>document.getElementById(id);
const esc=s=>String(s||"").replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;");
const enc=s=>encodeURIComponent(s||"");
const fmtSec=s=>{if(s==null||isNaN(s))return"0:00";s=Math.max(0,Math.floor(s));const h=Math.floor(s/3600),m=Math.floor((s%3600)/60),sc=s%60,p=n=>String(n).padStart(2,"0");return h?`${h}:${p(m)}:${p(sc)}`:`${m}:${p(sc)}`;};
const fmtDur=d=>{if(!d)return"";const p=d.split(":").map(Number);return p.length===3?fmtSec(p[0]*3600+p[1]*60+p[2]):d;};
async function api(url,opts){try{return await fetch(url,opts);}catch{return null;}}
function toast(msg,ms=2400){const t=$("toast");t.textContent=msg;t.classList.add("show");setTimeout(()=>t.classList.remove("show"),ms);}

let servers={},curServer=null,navStack=[{id:"0",title:"Root"}],curItemId=null,ps={state:"stopped"};
let seeking=false,seekTarget=0;
let curTab="browse";
let playlists=[],curPlId=null;

// ── Browser audio queue ───────────────────────────────────────────
const browserAudio=document.getElementById("browser-audio");
let browserQueue=[],browserIdx=0;
let shuffleOn=false, originalQueue=[];

// Fisher-Yates shuffle — proper uniform random, no bias
function fyshuffle(arr){
  const a=[...arr];
  for(let i=a.length-1;i>0;i--){
    const j=Math.floor(Math.random()*(i+1));
    [a[i],a[j]]=[a[j],a[i]];
  }
  return a;
}

browserAudio.addEventListener("ended",()=>{
  if(browserIdx<browserQueue.length-1){browserIdx++;_browserPlayIdx(browserIdx);}
  else{activeDevice="browser";} // playlist done
});
browserAudio.addEventListener("error",e=>{toast("⚠ Stream error — format may not be supported in browser");});

function _browserPlayIdx(idx){
  const t=browserQueue[idx];if(!t)return;
  browserAudio.src=`/stream?url=${enc(t.url)}`;
  browserAudio.play().catch(()=>{});
  $("np-title").textContent=t.title||"";
  $("np-artist").textContent=t.artist||"";
  $("np-album").textContent=t.album||"";
  $("np-meta").textContent=`Track ${idx+1} of ${browserQueue.length}`;
  if(t.art){$("art").innerHTML=`<img src="${esc(t.art)}" style="width:100%;height:100%;object-fit:cover;border-radius:12px" onerror="this.parentElement.textContent='💿'">`;}
  else{$("art").textContent="💿";}
  $("player").className="playing is-audio";
  $("btn-pp").textContent="⏸ Pause";
  $("hdr-status").textContent=t.title||"";
  _updateMiniPlayer(t);
  _updateMediaSession(t, idx);
}

function _updateMediaSession(t, idx){
  if(!("mediaSession" in navigator)) return;
  navigator.mediaSession.metadata = new MediaMetadata({
    title:  t.title  || "Unknown",
    artist: t.artist || "",
    album:  t.album  || "",
    artwork: t.art ? [
      {src: t.art, sizes: "256x256", type: "image/jpeg"},
      {src: t.art, sizes: "512x512", type: "image/jpeg"}
    ] : []
  });
  navigator.mediaSession.playbackState = "playing";
  // Wire CarPlay / headphone / lock-screen controls
  navigator.mediaSession.setActionHandler("play",  ()=>{browserAudio.play();navigator.mediaSession.playbackState="playing";$("btn-pp").textContent="⏸ Pause";});
  navigator.mediaSession.setActionHandler("pause", ()=>{browserAudio.pause();navigator.mediaSession.playbackState="paused";$("btn-pp").textContent="▶ Play";});
  navigator.mediaSession.setActionHandler("previoustrack", ()=>{
    if(browserIdx>0){browserIdx--;_browserPlayIdx(browserIdx);}
  });
  navigator.mediaSession.setActionHandler("nexttrack", ()=>{
    if(browserIdx<browserQueue.length-1){browserIdx++;_browserPlayIdx(browserIdx);}
  });
  navigator.mediaSession.setActionHandler("seekto", e=>{
    if(e.seekTime!=null) browserAudio.currentTime=e.seekTime;
  });
  // Update position state for CarPlay seek bar
  if(browserAudio.duration && !isNaN(browserAudio.duration)){
    try{
      navigator.mediaSession.setPositionState({
        duration: browserAudio.duration,
        playbackRate: 1,
        position: browserAudio.currentTime
      });
    }catch(e){}
  }
}

function _updateMiniPlayer(t){
  $("mini-title").textContent=t?.title||"Nothing playing";
  $("mini-artist").textContent=t?.artist||"";
  if(t?.art){$("mini-art").innerHTML=`<img src="${esc(t.art)}" style="width:100%;height:100%;object-fit:cover;border-radius:6px" onerror="this.parentElement.textContent='🎵'">`;}
  else{$("mini-art").textContent=t?"💿":"🎵";}
  // Pulse the now-playing nav icon when something is playing
  const icon=$("bnav-np-icon");
  if(icon) icon.textContent=t?"▶":"🎵";
}

// ── Mobile navigation ─────────────────────────────────────────────
const _mobileClasses=["m-browse","m-search","m-pl","m-fav","m-np"];
function mobileTab(tab){
  // Only applies on mobile (bottom nav visible)
  if(!$("bottom-nav").offsetParent&&$("bottom-nav").style.display==="none")return;
  document.body.classList.remove(..._mobileClasses);
  ["browse","search","playlists","favourites","nowplaying"].forEach(t=>
    $("bnav-"+t)?.classList.toggle("active",t===tab));
  // Show/hide back button on Now Playing
  const nb=$("np-back");if(nb)nb.style.display=tab==="nowplaying"?"":"none";
  if(tab==="nowplaying"){document.body.classList.add("m-np");}
  else if(tab==="playlists"){document.body.classList.add("m-pl");showTab("playlists");}
  else if(tab==="favourites"){document.body.classList.add("m-fav");showTab("favourites");}
  else if(tab==="search"){document.body.classList.add("m-search");showTab("search");}
  else{document.body.classList.add("m-browse");showTab("browse");}
}

// ── Capabilities (IINA available on this host?) ───────────────────
async function checkCapabilities(){
  const r=await api("/api/capabilities");if(!r)return;
  const caps=await r.json();
  if(!caps.iina){
    const opt=document.querySelector('#output-sel option[value="iina"]');
    if(opt)opt.remove();
    if($("output-sel").value==="iina"){
      // Default to browser on mobile/remote
      $("output-sel").value="browser";
      activeDevice="browser";
    }
  }
}

// ── Tabs ──────────────────────────────────────────────────────────
function showTab(tab){
  curTab=tab;
  ["browse","search","playlists","favourites"].forEach(t=>{
    $("tab-"+t).classList.toggle("active",t===tab);
  });
  if(tab==="browse"){
    $("breadcrumb").style.display="";
    renderBreadcrumb();
    if(curServer) showArtists();
    else $("item-list").innerHTML='<div class="msg">Select a server above</div>';
  } else if(tab==="search"){
    $("breadcrumb").style.display="none";
    $("search-input").focus();
    $("item-list").innerHTML='<div class="msg">Type in the search box above…</div>';
  } else if(tab==="playlists"){
    $("breadcrumb").style.display="none";
    showPlaylists();
  } else if(tab==="favourites"){
    $("breadcrumb").style.display="none";
    showFavourites();
  }
}

// ── Servers (source) ─────────────────────────────────────────────
let renderers = {};   // udn → MediaRenderer
async function refreshServers(){
  const r=await api("/api/servers");if(!r)return;
  const data=await r.json();
  const sel=$("server-sel"),prev=sel.value||curServer?.udn;
  servers={};data.forEach(s=>servers[s.udn]=s);
  const dot=$("disc-dot"),lbl=$("disc-label");
  const online=data.filter(s=>s.online);
  if(online.length){dot.className="disc-dot on";lbl.textContent=`${online.length} server${online.length>1?"s":""} online`;}
  else if(data.length){dot.className="disc-dot";lbl.textContent="Server offline — reconnecting…";}
  else{dot.className="disc-dot";lbl.textContent="Scanning…";}
  // Populate selector — offline servers shown dimmed but selectable
  if(data.length){
    sel.innerHTML=data.map(s=>`<option value="${esc(s.udn)}" ${s.online?"":"style='color:var(--ink-dim)'"}>
      ${esc(s.name)}${s.online?"":" ⚠"}${s.tracks?" · "+s.tracks.toLocaleString()+" tracks":""}
    </option>`).join("");
  } else {
    sel.innerHTML='<option value="">— scanning… —</option>';
  }
  // Restore previous server selection (keep curServer even when offline)
  if(prev&&servers[prev]){sel.value=prev;curServer=servers[prev];}
  else if(data.length&&!curServer){curServer=data[0];sel.value=data[0].udn;if(curTab==="browse")showArtists();}
}

// ── Renderers (output) ────────────────────────────────────────────
let castDevices={};
async function refreshRenderers(){
  const r=await api("/api/renderers");if(!r)return;
  const data=await r.json();
  renderers={};data.forEach(rd=>renderers[rd.udn]=rd);

  // Fetch Chromecast devices too
  let castList=[];
  const cr=await api("/api/cast_devices");
  if(cr){castList=await cr.json();castDevices={};castList.forEach(d=>castDevices[d.uuid]=d);}

  const out=$("output-sel"),prev=out.value;
  const upnpOpts=data.map(rd=>`<option value="upnp:${esc(rd.udn)}">📡 ${esc(rd.name)}</option>`).join("");
  const castOpts=castList.map(d=>`<option value="cast:${esc(d.uuid)}">📺 ${esc(d.name)}</option>`).join("");
  const iinaCap=out.querySelector('option[value="iina"]');
  const iinaTxt=iinaCap?iinaCap.text:'🖥 IINA (local)';
  out.innerHTML=`<option value="browser">📱 Browser</option>${upnpOpts}${castOpts}<option value="iina">${iinaTxt}</option>`;
  // Restore previous selection
  if(prev&&(prev==="iina"||prev==="browser"
    ||renderers[prev.replace("upnp:","")]
    ||castDevices[prev.replace("cast:","")])){out.value=prev;}
  else{out.value="browser";}
}
$("server-sel").addEventListener("change",e=>{
  const s=servers[e.target.value];if(!s)return;
  curServer=s;navStack=[{id:"0",title:"Root"}];
  if(curTab==="browse")showArtists();
});

// ── Browse ────────────────────────────────────────────────────────
let browsing=false;

// ── Top-level library navigation ─────────────────────────────────
function _navButtons(){
  const wrap=document.createElement("div");
  wrap.style.cssText="display:flex;gap:8px;padding:10px 14px;border-bottom:1px solid var(--border);flex-wrap:wrap";
  const btns=[
    {label:"👤 Artists",fn:showArtists},
    {label:"💿 Albums",fn:showAlbums},
    {label:"🎭 Genres",fn:showGenres},
    {label:"📁 Folders",fn:()=>{navStack=[{id:"0",title:"Root"}];browse("0");}},
  ];
  btns.forEach(b=>{
    const el=document.createElement("button");
    el.className="btn";el.style.cssText="font-size:12px;padding:6px 14px";
    el.textContent=b.label;
    el.addEventListener("click",b.fn);
    wrap.appendChild(el);
  });
  return wrap;
}

// ── Artists view (SQLite — default startup view) ──────────────────
async function showArtists(){
  if(!curServer)return;
  navStack=[{id:"__artists__",title:"Artists"}];
  renderBreadcrumb();
  $("item-list").innerHTML='<div class="spinner-wrap"><div class="spinner"></div></div>';
  const r=await api(`/api/artists?udn=${enc(curServer.udn)}`);
  if(!r){$("item-list").innerHTML='<div class="msg">Could not load artists.</div>';return;}
  const artists=await r.json();
  if(!artists.length){
    browse("0");
    return;
  }
  $("item-list").innerHTML="";
  $("item-list").appendChild(_navButtons());
  artists.forEach(a=>{
    const div=document.createElement("div");
    div.className="row";
    div.innerHTML=`${a.art?`<img src="${esc(a.art)}" style="width:36px;height:36px;object-fit:cover;border-radius:4px;flex-shrink:0" onerror="this.style.display='none'">`:`<div class="row-icon">👤</div>`}<div class="row-body"><div class="row-title">${esc(a.artist)}</div><div class="row-sub">${a.album_count} album${a.album_count!==1?"s":""} · ${a.track_count} tracks</div></div><div class="row-actions"><button class="icon-btn" data-artist="${esc(a.artist)}" data-action="play">▶</button></div>`;
    div.addEventListener("click",e=>{
      const btn=e.target.closest("[data-action]");
      if(btn){e.stopPropagation();browseArtist(btn.dataset.artist);return;}
      browseArtist(a.artist);
    });
    $("item-list").appendChild(div);
  });
}

// ── Albums view (SQLite) ─────────────────────────────────────────
async function showAlbums(){
  if(!curServer)return;
  navStack=[{id:"__albums__",title:"Albums"}];
  renderBreadcrumb();
  $("item-list").innerHTML='<div class="spinner-wrap"><div class="spinner"></div></div>';
  const r=await api(`/api/albums?udn=${enc(curServer.udn)}`);
  if(!r){$("item-list").innerHTML='<div class="msg">Could not load albums.</div>';return;}
  const albums=await r.json();
  if(!albums.length){$("item-list").innerHTML='<div class="msg">No albums indexed yet</div>';return;}
  $("item-list").innerHTML="";
  $("item-list").appendChild(_navButtons());
  albums.forEach(a=>{
    const div=document.createElement("div");
    div.className="row";
    div.innerHTML=`${a.art?`<img src="${esc(a.art)}" style="width:36px;height:36px;object-fit:cover;border-radius:4px;flex-shrink:0" onerror="this.style.display='none'">`:`<div class="row-icon">💿</div>`}<div class="row-body"><div class="row-title">${esc(a.album)}</div><div class="row-sub">${esc(a.artist)} · ${a.track_count} tracks</div></div><div class="row-actions"><button class="icon-btn" data-artist="${esc(a.artist)}" data-album="${esc(a.album)}" data-action="play">▶</button></div>`;
    div.addEventListener("click",e=>{
      const btn=e.target.closest("[data-action]");
      if(btn){e.stopPropagation();playAlbumFromDB(btn.dataset.artist,btn.dataset.album);return;}
      playAlbumFromDB(a.artist,a.album);
    });
    $("item-list").appendChild(div);
  });
}

// ── Genres view (SQLite) ─────────────────────────────────────────
async function showGenres(){
  if(!curServer)return;
  navStack=[{id:"__genres__",title:"Genres"}];
  renderBreadcrumb();
  $("item-list").innerHTML='<div class="spinner-wrap"><div class="spinner"></div></div>';
  const r=await api(`/api/genres?udn=${enc(curServer.udn)}`);
  if(!r){$("item-list").innerHTML='<div class="msg">Could not load genres.</div>';return;}
  const genres=await r.json();
  if(!genres.length){$("item-list").innerHTML='<div class="msg">No genres indexed yet</div>';return;}
  $("item-list").innerHTML="";
  $("item-list").appendChild(_navButtons());
  genres.forEach(g=>{
    const div=document.createElement("div");
    div.className="row";
    div.innerHTML=`<div class="row-icon">🎭</div><div class="row-body"><div class="row-title">${esc(g.genre)}</div><div class="row-sub">${g.album_count} albums · ${g.track_count} tracks</div></div>`;
    div.addEventListener("click",()=>browseGenre(g.genre));
    $("item-list").appendChild(div);
  });
}

async function browseGenre(genre){
  if(!curServer)return;
  navStack=[{id:"__genres__",title:"Genres"},{id:"genre:"+genre,title:genre}];
  renderBreadcrumb();
  $("item-list").innerHTML='<div class="spinner-wrap"><div class="spinner"></div></div>';
  const r=await api(`/api/genre_albums?udn=${enc(curServer.udn)}&genre=${enc(genre)}`);
  if(!r){$("item-list").innerHTML='<div class="msg">Could not load genre.</div>';return;}
  const albums=await r.json();
  $("item-list").innerHTML="";
  if(!albums.length){$("item-list").innerHTML='<div class="msg">No albums in this genre</div>';return;}
  // Play all button
  const playAll=document.createElement("div");
  playAll.className="row";
  playAll.innerHTML=`<div class="row-icon">▶</div><div class="row-body"><div class="row-title">Play all ${genre}</div><div class="row-sub">${albums.reduce((s,a)=>s+a.track_count,0)} tracks</div></div>`;
  playAll.addEventListener("click",async()=>{
    const tr=await api(`/api/genre_tracks?udn=${enc(curServer.udn)}&genre=${enc(genre)}`);
    if(!tr)return;const d=await tr.json();
    if(d.tracks)await playTracklist(d.tracks,genre,"");
  });
  $("item-list").appendChild(playAll);
  albums.forEach(a=>{
    const div=document.createElement("div");
    div.className="row";
    div.innerHTML=`${a.art?`<img src="${esc(a.art)}" style="width:36px;height:36px;object-fit:cover;border-radius:4px;flex-shrink:0" onerror="this.style.display='none'">`:`<div class="row-icon">💿</div>`}<div class="row-body"><div class="row-title">${esc(a.album)}</div><div class="row-sub">${esc(a.artist)} · ${a.track_count} tracks</div></div><div class="row-actions"><button class="icon-btn" data-artist="${esc(a.artist)}" data-album="${esc(a.album)}" data-action="play">▶</button></div>`;
    div.addEventListener("click",e=>{
      const btn=e.target.closest("[data-action]");
      if(btn){e.stopPropagation();playAlbumFromDB(btn.dataset.artist,btn.dataset.album);return;}
      playAlbumFromDB(a.artist,a.album);
    });
    $("item-list").appendChild(div);
  });
}

let _browseRetryTimer=null;
let _browseRetries=0;
const _BROWSE_MAX_RETRIES=5;
async function browse(id){
  if(!curServer)return;
  if(browsing)return;  // drop if already in flight
  browsing=true;
  clearTimeout(_browseRetryTimer);
  $("item-list").innerHTML='<div class="spinner-wrap"><div class="spinner"></div></div>';
  try{
    const r=await api(`/api/browse?udn=${enc(curServer.udn)}&id=${enc(id)}`);
    if(!r){
      $("item-list").innerHTML='<div class="msg">Browse failed — check server connection.</div>';
      return;
    }
    const data=await r.json();
    if(data.error){
      if(_browseRetries<_BROWSE_MAX_RETRIES){
        _browseRetries++;
        const delay=_browseRetries*3000;
        $("item-list").innerHTML=`<div class="msg">Library loading… <span style="color:var(--ink-dim);font-size:11px">(attempt ${_browseRetries}/${_BROWSE_MAX_RETRIES})</span></div>`;
        _browseRetryTimer=setTimeout(()=>browse(id),delay);
      } else {
        // Give up — show error with manual retry button
        $("item-list").innerHTML=`<div class="msg">Browse unavailable.<br><span style="color:var(--ink-dim);font-size:11px">${esc(data.error)}</span><br><br><button class="btn" style="font-size:11px;padding:5px 12px" onclick="_browseRetries=0;browse('${id}')">↺ Retry</button></div>`;
      }
      return;
    }
    _browseRetries=0;
    renderBreadcrumb();renderList(data);
  }finally{browsing=false;}
}

function renderBreadcrumb(){
  $("breadcrumb").innerHTML=navStack.map((c,i)=>i<navStack.length-1?`<span class="crumb" data-id="${c.id}">${esc(c.title)}</span><span class="crumb-sep"> › </span>`:`<span class="crumb-cur">${esc(c.title)}</span>`).join("");
  $("breadcrumb").querySelectorAll(".crumb").forEach(el=>el.addEventListener("click",()=>{
    const idx=navStack.findIndex(c=>c.id===el.dataset.id);
    if(idx>=0){
      navStack=navStack.slice(0,idx+1);
      if(el.dataset.id==="__artists__") showArtists();
      else if(el.dataset.id==="__albums__") showAlbums();
      else if(el.dataset.id==="__genres__") showGenres();
      else browse(el.dataset.id);
    }
  }));
}

// Item registry — avoids encoding items as JSON in onclick attributes
// which breaks on apostrophes and special characters in track titles.
const itemRegistry = new Map();
let itemRegSeq = 0;
function regItem(item){ const k=itemRegSeq++;itemRegistry.set(k,item);return k; }

function renderList(data,context="browse"){
  const list=$("item-list"),all=[...(data.containers||[]),...(data.items||[])];
  if(!all.length){list.innerHTML='<div class="msg">Empty folder</div>';return;}
  list.innerHTML="";
  all.forEach(item=>{
    const k = regItem(item);
    const div=document.createElement("div");
    const icon=item.type==="container"?"📁":item.type==="video"?"🎬":"🎵";
    const sub=[item.artist,item.album].filter(Boolean).join(" · ");
    div.className="row"+(curItemId===item.id?" active":"");
    const artEl=item.art
      ?`<img src="${esc(item.art)}" style="width:36px;height:36px;object-fit:cover;border-radius:4px;flex-shrink:0" onerror="this.style.display='none'">`
      :`<div class="row-icon">${icon}</div>`;

    const isItem = item.type !== "container";
    div.innerHTML=`${artEl}<div class="row-body"><div class="row-title">${esc(item.title)}</div>${sub?`<div class="row-sub">${esc(sub)}</div>`:""}</div>${item.duration?`<div class="row-dur">${fmtDur(item.duration)}</div>`:""}<div class="row-actions">${isItem?`<button class="icon-btn" title="Add to Favourites" data-k="${k}" data-action="fav">⭐</button><button class="icon-btn" title="Add to playlist…" data-k="${k}" data-action="add">＋</button>`:""}<button class="icon-btn" title="${isItem?"Play":"Play album"}" data-k="${k}" data-action="play">▶</button></div>`;

    div.addEventListener("click",e=>{
      const btn=e.target.closest("[data-action]");
      if(btn){
        e.stopPropagation();
        const it=itemRegistry.get(Number(btn.dataset.k));
        if(!it)return;
        if(btn.dataset.action==="fav") addToPlaylist("__favourites__",it);
        else if(btn.dataset.action==="add") showAddToPlaylistForItem(e,it);
        else if(btn.dataset.action==="play"){
          if(it.type==="container") playAlbum(it);
          else startPlay(it,div);
        }
        return;
      }
      if(item.type==="container"){navStack.push({id:item.id,title:item.title});browse(item.id);}
      else startPlay(item,div);
    });
    list.appendChild(div);
  });
}

// ── Search ────────────────────────────────────────────────────────
let searchTimer=null;
$("search-input").addEventListener("input",e=>{
  const q=e.target.value.trim();
  clearTimeout(searchTimer);
  if(!q){if(curTab==="search")$("item-list").innerHTML='<div class="msg">Type to search…</div>';return;}
  // Switch to search tab automatically
  if(curTab!=="search"){showTab("search");}
  searchTimer=setTimeout(()=>doSearch(q),400);
});
$("search-input").addEventListener("keydown",e=>{
  if(e.key==="Enter"){clearTimeout(searchTimer);doSearch(e.target.value.trim());}
});

async function doSearch(q){
  if(!curServer||!q)return;
  $("item-list").innerHTML='<div class="spinner-wrap"><div class="spinner"></div></div>';
  const r=await api(`/api/search?udn=${enc(curServer.udn)}&q=${enc(q)}`);
  if(!r){$("item-list").innerHTML='<div class="msg">Search failed.</div>';return;}
  const data=await r.json();
  if(data.error){$("item-list").innerHTML=`<div class="msg">${esc(data.error)}</div>`;return;}

  const total=(data.tracks||[]).length+(data.albums||[]).length+(data.artists||[]).length;
  if(!total){$("item-list").innerHTML=`<div class="msg">No results for <b>${esc(q)}</b></div>`;return;}

  $("item-list").innerHTML="";

  // Artists section
  if((data.artists||[]).length){
    addSearchSection(`Artists (${data.artists.length})`);
    data.artists.forEach(a=>{
      const k=regItem({id:"artist:"+a.artist,title:a.artist,type:"container",art:a.art||""});
      const div=document.createElement("div");
      div.className="row";
      div.innerHTML=`${a.art?`<img src="${esc(a.art)}" style="width:36px;height:36px;object-fit:cover;border-radius:4px;flex-shrink:0" onerror="this.style.display='none'">`:
        `<div class="row-icon">👤</div>`}<div class="row-body"><div class="row-title">${esc(a.artist)}</div><div class="row-sub">${a.album_count} albums · ${a.track_count} tracks</div></div>`;
      div.addEventListener("click",()=>browseArtist(a.artist));
      $("item-list").appendChild(div);
    });
  }

  // Albums section
  if((data.albums||[]).length){
    addSearchSection(`Albums (${data.albums.length})`);
    data.albums.forEach(a=>{
      const pseudo={id:"album:"+a.artist+"/"+a.album,title:a.album,artist:a.artist,type:"album",art:a.art||""};
      const k=regItem(pseudo);
      const div=document.createElement("div");
      div.className="row";
      div.innerHTML=`${a.art?`<img src="${esc(a.art)}" style="width:36px;height:36px;object-fit:cover;border-radius:4px;flex-shrink:0" onerror="this.style.display='none'">`:
        `<div class="row-icon">💿</div>`}<div class="row-body"><div class="row-title">${esc(a.album)}</div><div class="row-sub">${esc(a.artist)} · ${a.track_count} tracks</div></div><div class="row-actions"><button class="icon-btn" data-k="${k}" data-action="fav">⭐</button><button class="icon-btn" data-k="${k}" data-action="play">▶</button></div>`;
      div.addEventListener("click",e=>{
        const btn=e.target.closest("[data-action]");
        if(btn){e.stopPropagation();const it=itemRegistry.get(Number(btn.dataset.k));if(!it)return;
          if(btn.dataset.action==="play") playAlbumFromDB(a.artist,a.album);
          else if(btn.dataset.action==="fav") addAlbumToPlaylist("__favourites__",a.artist,a.album);
          return;}
        playAlbumFromDB(a.artist,a.album);
      });
      $("item-list").appendChild(div);
    });
  }

  // Tracks section
  if((data.tracks||[]).length){
    addSearchSection(`Tracks (${data.tracks.length})`);
    renderListAppend({containers:[],items:data.tracks});
  }
}

function addSearchSection(label){
  const h=document.createElement("div");
  h.style.cssText="padding:6px 14px 4px;font-size:10px;color:var(--ink-dim);letter-spacing:.08em;text-transform:uppercase;border-bottom:1px solid var(--border)";
  h.textContent=label;
  $("item-list").appendChild(h);
}

async function browseArtist(artist){
  // Find all tracks by this artist from the DB and show them grouped by album
  if(!curServer)return;
  const r=await api(`/api/search?udn=${enc(curServer.udn)}&q=${enc(artist)}`);
  if(!r)return;
  const data=await r.json();
  $("item-list").innerHTML="";
  const hdr=document.createElement("div");
  hdr.style.cssText="padding:8px 14px;font-family:var(--fh);font-weight:600;font-size:12px;border-bottom:1px solid var(--border);display:flex;align-items:center;gap:8px";
  hdr.innerHTML=`<span style="cursor:pointer;color:var(--amber)" onclick="showTab('search')">← Search</span><span>${esc(artist)}</span>`;
  $("item-list").appendChild(hdr);
  if((data.albums||[]).length){
    addSearchSection("Albums");
    data.albums.filter(a=>a.artist.toLowerCase()===artist.toLowerCase()).forEach(a=>{
      const div=document.createElement("div");
      div.className="row";
      div.innerHTML=`${a.art?`<img src="${esc(a.art)}" style="width:36px;height:36px;object-fit:cover;border-radius:4px;flex-shrink:0" onerror="this.style.display='none'">`:
        `<div class="row-icon">💿</div>`}<div class="row-body"><div class="row-title">${esc(a.album)}</div><div class="row-sub">${a.track_count} tracks</div></div><div class="row-actions"><button class="icon-btn" onclick="event.stopPropagation();playAlbumFromDB('${esc(a.artist)}','${esc(a.album)}')">▶</button></div>`;
      div.addEventListener("click",()=>playAlbumFromDB(a.artist,a.album));
      $("item-list").appendChild(div);
    });
  }
  if((data.tracks||[]).length){
    addSearchSection("Tracks");
    renderListAppend({containers:[],items:data.tracks.filter(t=>t.artist&&t.artist.toLowerCase()===artist.toLowerCase())});
  }
}

async function playAlbumFromDB(artist,album){
  if(!curServer)return;
  toast("Loading album…",3000);
  const r=await api(`/api/album_tracks?udn=${enc(curServer.udn)}&artist=${enc(artist)}&album=${enc(album)}`);
  if(!r){toast("Failed to load album");return;}
  const data=await r.json();
  if(!data.tracks||!data.tracks.length){toast("No tracks found for this album");return;}
  await playTracklist(data.tracks, album, artist);
}

// Central function: play a list of track objects via whatever output is selected
async function playTracklist(tracks, title, artist){
  const out=$("output-sel").value;
  const first=tracks[0];

  // Update now-playing panel
  $("np-title").textContent=title||first?.title||"";
  $("np-artist").textContent=artist||first?.artist||"";
  $("np-album").textContent=first?.album||"";
  $("np-meta").textContent=`${tracks.length} tracks`;
  if(first?.art){$("art").innerHTML=`<img src="${esc(first.art)}" style="width:100%;height:100%;object-fit:cover;border-radius:12px" onerror="this.parentElement.textContent='💿'">`;}
  else{$("art").textContent="💿";}
  $("player").className="playing is-audio";
  $("btn-pp").textContent="⏸ Pause";
  $("hdr-status").textContent=title||"";
  activeDevice=out;

  if(out==="browser"){
    originalQueue=[...tracks];
    browserQueue=shuffleOn?fyshuffle(tracks):[...tracks];
    browserIdx=0;
    _browserPlayIdx(0);
    toast(`▶ Playing ${tracks.length} tracks in browser${shuffleOn?" (shuffled)":""}`);
  } else if(out==="iina"){
    const r=await api("/api/play_tracks",{method:"POST",
      headers:{"Content-Type":"application/json"},
      body:JSON.stringify({tracks,title:title||""})});
    if(!r){toast("Failed to start IINA");return;}
    const d=await r.json();
    if(d.error){toast("Error: "+d.error);return;}
    toast(`▶ Playing ${tracks.length} tracks in IINA`);
  } else if(out.startsWith("cast:")){
    // Chromecast
    const castUuid=out.replace("cast:","");
    const r=await api("/api/cast_queue",{method:"POST",
      headers:{"Content-Type":"application/json"},
      body:JSON.stringify({uuid:castUuid, tracks})});
    if(!r){toast("Failed to reach Chromecast");return;}
    const d=await r.json();
    if(d.error){toast("Error: "+d.error);return;}
    const cname=castDevices[castUuid]?.name||castUuid;
    toast(`▶ Casting ${tracks.length} tracks to ${cname}`);
  } else {
    // UPnP renderer
    const rendUdn=out.replace("upnp:","");
    const r=await api("/api/render_queue",{method:"POST",
      headers:{"Content-Type":"application/json"},
      body:JSON.stringify({udn:rendUdn, tracks})});
    if(!r){toast("Failed to reach renderer");return;}
    const d=await r.json();
    if(d.error){toast("Error: "+d.error);return;}
    const rname=renderers[rendUdn]?.name||rendUdn;
    toast(`▶ Playing ${tracks.length} tracks on ${rname}`);
  }
}

async function addAlbumToPlaylist(plId,artist,album){
  if(!curServer)return;
  const r=await api(`/api/album_tracks?udn=${enc(curServer.udn)}&artist=${enc(artist)}&album=${enc(album)}`);
  if(!r)return;
  const data=await r.json();
  if(!data.tracks)return;
  let added=0;
  for(const t of data.tracks){
    const params=new URLSearchParams({pl:plId,url:t.url,title:t.title||"",artist:t.artist||"",album:t.album||"",duration:t.duration||"",art:t.art||""});
    const res=await api("/api/playlist/add?"+params.toString());
    if(res){const d=await res.json();if(d.ok&&!d.duplicate)added++;}
  }
  await loadPlaylists();
  const plName=playlists.find(p=>p.id===plId)?.name||plId;
  toast(`✓ Added ${added} tracks to ${plName}`);
}

function renderListAppend(data){
  const list=$("item-list"),all=[...(data.containers||[]),...(data.items||[])];
  all.forEach(item=>{
    const k=regItem(item);
    const div=document.createElement("div");
    const icon=item.type==="container"?"📁":item.type==="video"?"🎬":"🎵";
    const sub=[item.artist,item.album].filter(Boolean).join(" · ");
    div.className="row"+(curItemId===item.id?" active":"");
    const artEl=item.art
      ?`<img src="${esc(item.art)}" style="width:36px;height:36px;object-fit:cover;border-radius:4px;flex-shrink:0" onerror="this.style.display='none'">`
      :`<div class="row-icon">${icon}</div>`;
    const isItem=item.type!=="container";
    div.innerHTML=`${artEl}<div class="row-body"><div class="row-title">${esc(item.title)}</div>${sub?`<div class="row-sub">${esc(sub)}</div>`:""}</div>${item.duration?`<div class="row-dur">${fmtDur(item.duration)}</div>`:""}<div class="row-actions">${isItem?`<button class="icon-btn" data-k="${k}" data-action="fav">⭐</button><button class="icon-btn" data-k="${k}" data-action="add">＋</button>`:""}<button class="icon-btn" data-k="${k}" data-action="play">▶</button></div>`;
    div.addEventListener("click",e=>{
      const btn=e.target.closest("[data-action]");
      if(btn){
        e.stopPropagation();
        const it=itemRegistry.get(Number(btn.dataset.k));
        if(!it)return;
        if(btn.dataset.action==="fav") addToPlaylist("__favourites__",it);
        else if(btn.dataset.action==="add") showAddToPlaylistForItem(e,it);
        else if(btn.dataset.action==="play") startPlay(it,div);
        return;
      }
      if(isItem) startPlay(item,div);
    });
    list.appendChild(div);
  });
}

// ── Playlists panel ───────────────────────────────────────────────
async function loadPlaylists(){
  const r=await api("/api/playlists");if(!r)return;
  playlists=await r.json();
}

async function showPlaylists(){
  await loadPlaylists();
  $("pl-panel-title").textContent="Playlists";
  $("pl-back-btn").style.display="none";
  $("pl-list").style.display="";
  $("pl-tracks").classList.remove("visible");
  $("pl-actions").innerHTML=`<button class="btn" style="font-size:11px;padding:5px 10px" onclick="newPlaylist()">+ New playlist</button>`;
  const list=$("pl-list");
  list.innerHTML="";
  if(!playlists.length){list.innerHTML='<div class="msg" style="padding:20px">No playlists yet</div>';return;}
  playlists.forEach(pl=>{
    const div=document.createElement("div");
    div.className="pl-item"+(curPlId===pl.id?" active":"");
    div.innerHTML=`<div class="pl-item-name">${esc(pl.name)}</div><div class="pl-item-count">${pl.count} tracks</div>`;
    div.addEventListener("click",()=>openPlaylist(pl.id));
    list.appendChild(div);
  });
}

async function openPlaylist(plId){
  curPlId=plId;
  const r=await api(`/api/playlist?id=${enc(plId)}`);if(!r)return;
  const pl=await r.json();
  $("pl-panel-title").textContent=pl.name;
  $("pl-back-btn").style.display="";
  $("pl-list").style.display="none";
  $("pl-tracks").classList.add("visible");

  const isFav=plId==="__favourites__";
  $("pl-actions").innerHTML=`
    <button class="btn" style="font-size:11px;padding:5px 10px" onclick="playPlaylist('${plId}',false)">▶ Play</button>
    <button class="btn" style="font-size:11px;padding:5px 10px" onclick="playPlaylist('${plId}',true)">🔀 Radio</button>
    ${!isFav?`<button class="btn" style="font-size:11px;padding:5px 10px;color:var(--red)" onclick="deletePlaylist('${plId}')">🗑 Delete</button>`:""}
  `;

  const tracks=$("pl-tracks");
  tracks.innerHTML="";
  if(!pl.tracks||!pl.tracks.length){tracks.innerHTML='<div class="msg" style="padding:20px">Empty playlist</div>';return;}
  pl.tracks.forEach(t=>{
    const div=document.createElement("div");
    div.className="pl-track";
    div.innerHTML=`${t.art?`<img src="${esc(t.art)}" style="width:28px;height:28px;object-fit:cover;border-radius:3px;flex-shrink:0" onerror="this.style.display='none'">`:""}<div class="pl-track-body"><div class="pl-track-title">${esc(t.title)}</div><div class="pl-track-sub">${esc([t.artist,t.album].filter(Boolean).join(" · "))}</div></div><button class="icon-btn" style="flex-shrink:0" onclick="removeFromPlaylist('${plId}',${JSON.stringify(t.url)})">✕</button>`;
    div.querySelector(".pl-track-body").addEventListener("click",()=>startPlayTrack(t));
    tracks.appendChild(div);
  });
}

async function playPlaylist(plId,shuffle){
  const r=await api(`/api/playlist?id=${enc(plId)}`);
  if(!r)return;
  const pl=await r.json();
  let tracks=pl.tracks||[];
  if(!tracks.length){toast("Playlist is empty");return;}
  if(shuffle) tracks=fyshuffle(tracks);
  await playTracklist(tracks, pl.name, "");
}

async function newPlaylist(){
  const name=prompt("Playlist name:");
  if(!name)return;
  const r=await api(`/api/playlist/create?name=${enc(name)}`);
  if(!r)return;
  await showPlaylists();
  toast(`Created: ${name}`);
}

async function deletePlaylist(plId){
  if(!confirm("Delete this playlist?"))return;
  await api(`/api/playlist/delete?id=${enc(plId)}`);
  curPlId=null;showPlaylists();toast("Playlist deleted");
}

async function removeFromPlaylist(plId,url){
  await api(`/api/playlist/remove?pl=${enc(plId)}&url=${enc(url)}`);
  openPlaylist(plId);
}

function showFavourites(){
  openPlaylist("__favourites__");
  $("pl-list").style.display="none";
  $("pl-tracks").classList.add("visible");
}

// ── Add-to-playlist dropdown ──────────────────────────────────────
let dropdownEl=null;
function hideDropdown(){if(dropdownEl){dropdownEl.remove();dropdownEl=null;}}
document.addEventListener("click",hideDropdown);

async function showAddToPlaylistForItem(event,item){
  hideDropdown();
  await loadPlaylists(); // always refresh before showing
  const custom=playlists.filter(p=>p.id!=="__favourites__");
  if(!custom.length){toast("No playlists yet — create one in the Playlists tab");return;}
  const el=document.createElement("div");
  el.className="pl-dropdown";
  el.style.top=(event.clientY+4)+"px";
  el.style.left=Math.min(event.clientX,window.innerWidth-200)+"px";
  custom.forEach(pl=>{
    const d=document.createElement("div");
    d.className="pl-dropdown-item";d.textContent=pl.name;
    d.addEventListener("click",e=>{e.stopPropagation();hideDropdown();addToPlaylist(pl.id,item);});
    el.appendChild(d);
  });
  document.body.appendChild(el);dropdownEl=el;
}

async function addToPlaylist(plId,item){
  if(!item||!item.url){toast("Error: no track URL");return;}
  const params=new URLSearchParams({
    pl: plId,
    url: item.url,
    title: item.title||"",
    artist: item.artist||"",
    album: item.album||"",
    duration: item.duration||"",
    art: item.art||""
  });
  const r=await api("/api/playlist/add?"+params.toString());
  if(!r){toast("❌ Network error");return;}
  const data=await r.json();
  await loadPlaylists();
  if(data.ok===false && r.status===404){
    toast("❌ Playlist not found — try reloading");
  } else if(data.duplicate){
    toast("Already in playlist");
  } else {
    const plName=playlists.find(p=>p.id===plId)?.name||plId;
    toast(`✓ Added to ${plName}`);
  }
}

// ── Playback ──────────────────────────────────────────────────────
async function playAlbum(item){
  // Browse-pane album play — fetch track list via UPnP then route through output selector
  toast("Loading album…",3000);
  const r=await api(`/api/browse?udn=${enc(curServer.udn)}&id=${enc(item.id)}`);
  if(!r){toast("Failed to load album");return;}
  const data=await r.json();
  if(data.error){toast("Error: "+data.error);return;}
  const tracks=(data.items||[]).filter(t=>t.type!=="container");
  if(!tracks.length){toast("No tracks found in album");return;}
  await playTracklist(tracks, item.title, item.artist||"");
}

function startPlayTrack(item){
  PLAYER_play(item.url,item.title,item.art,item.type||"audio",item.artist||"",item.album||"");
}

async function startPlay(item,rowEl){
  curItemId=item.id;
  document.querySelectorAll(".row").forEach(r=>r.classList.remove("active"));
  if(rowEl)rowEl.classList.add("active");
  PLAYER_play(item.url,item.title,item.art,item.type||"audio",item.artist||"",item.album||"");
}

async function PLAYER_play(url,title,art,mtype,artist,album){
  $("np-title").textContent=title||"";
  $("np-artist").textContent=artist||"";
  $("np-album").textContent=album||"";
  $("np-meta").textContent="";
  if(art){$("art").innerHTML=`<img src="${esc(art)}" style="width:100%;height:100%;object-fit:cover;border-radius:12px" onerror="this.parentElement.textContent='🎵'">`;}
  else{$("art").textContent=mtype==="video"?"🎬":"🎵";}
  $("btn-pp").textContent="⏸ Pause";
  $("hdr-status").textContent=title||"";
  $("player").className="playing is-"+(mtype);

  const out=$("output-sel").value;
  activeDevice=out;
  _updateMiniPlayer({title,artist,art});

  if(out==="browser"){
    browserQueue=[{url,title,artist,album,art,mime:""}];browserIdx=0;
    browserAudio.src=`/stream?url=${enc(url)}`;
    browserAudio.play().catch(()=>{});
    toast("▶ Streaming in browser…");
  } else if(out==="iina"){
    await api(`/api/play?url=${enc(url)}&title=${enc(title)}`);
    toast("▶ Opening in IINA…");
  } else if(out.startsWith("cast:")){
    const castUuid=out.replace("cast:","");
    await api("/api/cast_queue",{method:"POST",
      headers:{"Content-Type":"application/json"},
      body:JSON.stringify({uuid:castUuid,tracks:[{url,title,artist,album,art,mime:""}]})});
    toast(`▶ Casting to ${castDevices[castUuid]?.name||castUuid}…`);
  } else {
    const rendUdn=out.replace("upnp:","");
    await api("/api/render_queue",{method:"POST",
      headers:{"Content-Type":"application/json"},
      body:JSON.stringify({udn:rendUdn,tracks:[{url,title,artist,art,mime:""}]})});
    toast(`▶ Sending to ${renderers[rendUdn]?.name||rendUdn}…`);
  }
}

// activeDevice tracks the current output: "iina" or "upnp:<udn>"
let activeDevice="browser";  // default — always available

// Sync activeDevice when user changes output selector
$("output-sel").addEventListener("change",()=>{activeDevice=$("output-sel").value;});

$("btn-pp").addEventListener("click",()=>control({action:"pause"}));
$("btn-stop").addEventListener("click",()=>{control({action:"stop"});resetPlayer();});
$("btn-rew").addEventListener("click",()=>control({action:"seek",value:-30}));
$("btn-fwd").addEventListener("click",()=>control({action:"seek",value:30}));
$("btn-prev").addEventListener("click",()=>control({action:"prev"}));
$("btn-next").addEventListener("click",()=>control({action:"next"}));
$("btn-shuffle").addEventListener("click",()=>control({action:"shuffle"}));
$("vol").addEventListener("input",()=>{const v=parseInt($("vol").value);$("vol-label").textContent=v;control({action:"volume",value:v});});
async function control(cmd){
  if(activeDevice==="browser"){
    // Handle browser audio locally — no server round-trip needed
    switch(cmd.action){
      case "pause":
        if(browserAudio.paused){browserAudio.play().catch(()=>{});$("btn-pp").textContent="⏸ Pause";$("mini-pp").textContent="⏸";}
        else{browserAudio.pause();$("btn-pp").textContent="▶ Play";$("mini-pp").textContent="▶";}
        break;
      case "stop":
        browserAudio.pause();browserAudio.currentTime=0;
        $("btn-pp").textContent="▶ Play";$("mini-pp").textContent="▶";
        break;
      case "next":
        if(browserIdx<browserQueue.length-1){browserIdx++;_browserPlayIdx(browserIdx);}
        break;
      case "prev":
        if(browserIdx>0){browserIdx--;_browserPlayIdx(browserIdx);}
        else{browserAudio.currentTime=0;}
        break;
      case "seek":
        browserAudio.currentTime=Math.max(0,browserAudio.currentTime+(cmd.value||0));break;
      case "seek_abs":
        browserAudio.currentTime=Math.max(0,cmd.value||0);break;
      case "volume":
        browserAudio.volume=Math.max(0,Math.min(1,(cmd.value||80)/100));break;
      case "shuffle":
        shuffleOn=!shuffleOn;
        $("btn-shuffle").style.color=shuffleOn?"var(--amber)":"";
        $("btn-shuffle").style.borderColor=shuffleOn?"var(--amber)":"";
        if(shuffleOn){
          // Save original order, shuffle remaining tracks (keep current playing)
          originalQueue=[...browserQueue];
          const cur=browserQueue[browserIdx];
          const rest=browserQueue.filter((_,i)=>i!==browserIdx);
          browserQueue=[cur,...fyshuffle(rest)];
          browserIdx=0;
          toast("🔀 Shuffle on");
        } else {
          // Restore original order, find current track in it
          const curUrl=browserQueue[browserIdx]?.url;
          browserQueue=[...originalQueue];
          browserIdx=Math.max(0,browserQueue.findIndex(t=>t.url===curUrl));
          toast("🔀 Shuffle off");
        }
        break;
    }
    return;
  }
  await api("/api/control",{method:"POST",headers:{"Content-Type":"application/json"},
    body:JSON.stringify({...cmd,device:activeDevice})});
}

function resetPlayer(){
  $("np-title").textContent="Nothing playing";$("np-artist").textContent="";$("np-album").textContent="";
  $("np-meta").textContent="Browse or search your library";
  $("art").textContent="🎵";$("btn-pp").textContent="▶ Play";$("player").className="";
  $("seek-fill").style.width="0%";$("seek-thumb").style.left="0%";
  $("t-pos").textContent="0:00";$("t-dur").textContent="0:00";curItemId=null;
}

const seekTrack=$("seek-track");
function pct(e){const r=seekTrack.getBoundingClientRect();return Math.max(0,Math.min(1,(e.clientX-r.left)/r.width));}
function applyPct(p){$("seek-fill").style.width=(p*100)+"%";$("seek-thumb").style.left=(p*100)+"%";$("t-pos").textContent=fmtSec(p*(ps.duration||0));}
seekTrack.addEventListener("mousedown",e=>{seeking=true;seekTrack.classList.add("dragging");const p=pct(e);applyPct(p);seekTarget=p*(ps.duration||0);});
document.addEventListener("mousemove",e=>{if(!seeking)return;const p=pct(e);applyPct(p);seekTarget=p*(ps.duration||0);});
document.addEventListener("mouseup",()=>{if(!seeking)return;seeking=false;seekTrack.classList.remove("dragging");control({action:"seek_abs",value:seekTarget});});

async function pollState(){
  // Poll the right player depending on active output
  if(activeDevice==="browser"){
    // ── Browser <audio> state (no server poll needed) ─────────
    const paused=browserAudio.paused||browserAudio.ended;
    const pos=browserAudio.currentTime||0;
    const dur=isNaN(browserAudio.duration)?0:browserAudio.duration;
    $("sb-dot").className="sb-dot "+(paused?"paused":"playing");
    $("sb-state").textContent=paused?"paused":"playing";
    $("btn-pp").textContent=paused?"▶ Play":"⏸ Pause";
    if(!seeking&&dur>0){
      const p=(pos/dur)*100;
      $("seek-fill").style.width=p+"%";$("seek-thumb").style.left=p+"%";
      $("t-pos").textContent=fmtSec(pos);$("t-dur").textContent=fmtSec(dur);
    }
    const t=browserQueue[browserIdx];
    if(t) $("sb-uri").textContent=t.title||"—";
    if(dur>0) $("mini-progress").style.width=((pos/dur)*100)+"%";
    // Keep CarPlay seek bar in sync
    if("mediaSession" in navigator && dur>0 && !paused){
      try{navigator.mediaSession.setPositionState({duration:dur,playbackRate:1,position:pos});}catch(e){}
    }
    return;
  }
  if(activeDevice.startsWith("cast:")){
    // ── Chromecast state ──────────────────────────────────────
    const r=await api("/api/cast_state");if(!r)return;
    const cs=await r.json();
    $("sb-dot").className="sb-dot "+(cs.state||"stopped");
    $("sb-state").textContent=cs.state||"stopped";
    $("sb-uri").textContent=cs.media_title||"—";
    if(cs.alive){
      $("btn-pp").textContent=cs.paused?"▶ Play":"⏸ Pause";
      if(cs.media_title){
        $("np-title").textContent=cs.media_title;
        $("hdr-status").textContent=cs.media_title;
      }
      if(cs.artist) $("np-artist").textContent=cs.artist;
      if(cs.album)  $("np-album").textContent=cs.album;
      if(cs.queue_len>1)
        $("np-meta").textContent=`Track ${cs.queue_pos} of ${cs.queue_len}`;
      if(!seeking&&cs.duration&&cs.position!=null){
        const p=(cs.position/cs.duration)*100;
        $("seek-fill").style.width=p+"%";$("seek-thumb").style.left=p+"%";
        $("t-pos").textContent=fmtSec(cs.position);$("t-dur").textContent=fmtSec(cs.duration);
        if(cs.duration>0) $("mini-progress").style.width=p+"%";
      }
      _updateMiniPlayer({title:cs.media_title,artist:cs.artist,art:""});
    } else {
      if($("player").classList.contains("playing")){
        $("player").classList.remove("playing");$("btn-pp").textContent="▶ Play";
      }
    }
    return;
  }
  if(activeDevice!=="iina"){
    // ── Renderer (Uniti) state ────────────────────────────────
    const r=await api("/api/renderer_state");if(!r)return;
    const ps=await r.json();
    $("sb-dot").className="sb-dot "+(ps.state||"stopped");
    $("sb-state").textContent=ps.state||"stopped";
    $("sb-uri").textContent=ps.media_title||ps.title||"—";
    if(ps.alive){
      $("btn-pp").textContent=ps.paused?"▶ Play":"⏸ Pause";
      if(ps.media_title||ps.title){
        $("np-title").textContent=ps.media_title||ps.title||"";
        $("hdr-status").textContent=ps.media_title||ps.title||"";
      }
      if(ps.artist) $("np-artist").textContent=ps.artist;
      if(ps.album)  $("np-album").textContent=ps.album;
      if(ps.queue_len>1)
        $("np-meta").textContent=`Track ${ps.queue_pos} of ${ps.queue_len}`;
      if(!seeking&&ps.duration&&ps.position!=null){
        const p=(ps.position/ps.duration)*100;
        $("seek-fill").style.width=p+"%";$("seek-thumb").style.left=p+"%";
        $("t-pos").textContent=fmtSec(ps.position);$("t-dur").textContent=fmtSec(ps.duration);
      }
    } else {
      if($("player").classList.contains("playing")){
        $("player").classList.remove("playing");$("btn-pp").textContent="▶ Play";
      }
    }
    return;
  }
  // ── IINA / mpv state ─────────────────────────────────────────
  const r=await api("/api/state");if(!r)return;
  ps=await r.json();
  if(!seeking&&ps.duration&&ps.position!=null){const p=(ps.position/ps.duration)*100;$("seek-fill").style.width=p+"%";$("seek-thumb").style.left=p+"%";$("t-pos").textContent=fmtSec(ps.position);$("t-dur").textContent=fmtSec(ps.duration);}
  if(ps.alive){
    $("btn-pp").textContent=ps.paused?"▶ Play":"⏸ Pause";
    if(ps.volume!=null){$("vol").value=Math.round(ps.volume);$("vol-label").textContent=Math.round(ps.volume);}
    if(ps.media_title){
      $("np-title").textContent=ps.media_title;
      $("hdr-status").textContent=ps.media_title;
    }
    if(ps.artist) $("np-artist").textContent=ps.artist;
    if(ps.album)  $("np-album").textContent=ps.album;
    $("btn-shuffle").style.color=ps.shuffle?"var(--amber)":"";
    $("btn-shuffle").style.borderColor=ps.shuffle?"var(--amber)":"";
  }
  $("sb-dot").className="sb-dot "+(ps.state||"stopped");
  $("sb-state").textContent=ps.state||"stopped";
  $("sb-uri").textContent=ps.media_title||ps.title||"—";
  if(!ps.alive&&$("player").classList.contains("playing")){$("player").classList.remove("playing");$("btn-pp").textContent="▶ Play";}
}

async function pollIndex(){
  if(!curServer)return;
  const r=await api(`/api/index/status?udn=${enc(curServer.udn)}`);
  if(!r)return;
  const s=await r.json();
  const bar=$("index-bar"),lbl=$("index-label"),pb=$("index-progress-bar");
  if(s.status==="running"){
    bar.style.display="";
    const pct=s.total>0?Math.round((s.progress/s.total)*100):0;
    lbl.textContent=`Indexing… ${s.progress}/${s.total} albums · ${s.tracks} tracks`;
    pb.style.width=pct+"%";
  } else if(s.status==="done"){
    bar.style.display="";
    lbl.textContent=`Library: ${s.db_tracks.toLocaleString()} tracks indexed ✓`;
    pb.style.width="100%";
  } else if(s.status==="error"){
    bar.style.display="";
    lbl.textContent=`Index error: ${s.error}`;
    pb.style.background="var(--red)";pb.style.width="100%";
  }
}

async function reindex(){
  if(!curServer)return;
  if(!confirm("Rebuild the full library index? This takes a few minutes."))return;
  await api(`/api/index/rebuild?udn=${enc(curServer.udn)}`);
  toast("Rebuilding index…",3000);
}

// ── Visibility-aware polling ─────────────────────────────────────
// Stops all intervals when the page/tab is hidden (screen locked,
// app backgrounded) → zero network wake-ups, zero battery drain.
// Restarts when the user returns.
let _pollIds = [];
function startPolling() {
  stopPolling();
  _pollIds = [
    setInterval(refreshServers, 8000),
    setInterval(refreshRenderers, 10000),
    setInterval(pollState, 1000),
    setInterval(pollIndex, 2000),
  ];
  // Immediate refresh on return
  refreshServers(); refreshRenderers(); pollState(); pollIndex();
}
function stopPolling() {
  _pollIds.forEach(id => clearInterval(id));
  _pollIds = [];
}
document.addEventListener("visibilitychange", () => {
  if (document.hidden) stopPolling();
  else startPolling();
});

// ── Init ──────────────────────────────────────────────────────────
checkCapabilities();
refreshServers();
refreshRenderers();
loadPlaylists().then(showPlaylists);
startPolling();

// ── Service Worker registration ──────────────────────────────────
if ('serviceWorker' in navigator) {
  navigator.serviceWorker.register('/sw.js')
    .then(reg => {
      // Check for updates every 30 min
      setInterval(() => reg.update(), 30 * 60 * 1000);
    })
    .catch(err => console.warn('SW registration failed:', err));
}
</script>
</body>
"""


# ── Helpers ───────────────────────────────────────────────────────

def get_lan_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


def open_browser(port: int):
    url = f"http://localhost:{port}/"
    for cmd in (["open", url], ["xdg-open", url]):
        try:
            subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
            return
        except FileNotFoundError:
            continue


def _on_server_found(server):
    """Hook called by discovery when a new MediaServer is registered.
    Skip indexing for combined devices (e.g. Naim Uniti) that appear as
    both a MediaServer and a MediaRenderer — they have no music library."""
    from dlna_discovery import RENDERERS
    if RENDERERS.get(server.udn):
        log.info(f"Skipping indexer for {server.name!r} "
                 f"— registered as renderer (combined device)")
        return
    INDEXER.start(server, force=False)


# ── Main ──────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="DLNA/UPnP Gateway → IINA",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  ./setup.sh --run                                  # Normal start
  ./setup.sh --run --probe http://192.168.1.x:port/DeviceDescription.xml
                                                    # Add server that doesn't respond to SSDP
  ./setup.sh --run --reset-devices                  # Clear device DB and rediscover everything
  ./setup.sh --run --list-devices                   # Show known devices table then exit
        """)
    parser.add_argument("--host",          default="0.0.0.0")
    parser.add_argument("--port",          type=int, default=8765)
    parser.add_argument("--tls-port",      type=int, default=8443,
                        help="HTTPS port (only used when --tls-cert is set)")
    parser.add_argument("--tls-cert",      default="",
                        help="Path to TLS certificate (.crt) for HTTPS")
    parser.add_argument("--tls-key",       default="",
                        help="Path to TLS private key (.key) for HTTPS")
    parser.add_argument("--probe",         default="",
                        help="Direct device URL — bypasses SSDP, adds permanently to DB")
    parser.add_argument("--no-browser",    action="store_true")
    parser.add_argument("--debug",         action="store_true")
    parser.add_argument("--reset-devices", action="store_true",
                        help="Clear the device_roles table and rediscover from scratch. "
                             "Use when a device was mis-classified or you want to "
                             "remove a device that no longer exists.")
    parser.add_argument("--list-devices",  action="store_true",
                        help="Print the known devices table and exit.")
    args = parser.parse_args()

    setup_logging(debug=args.debug)

    # ── --list-devices: print table and exit ──────────────────────
    if args.list_devices:
        rows = DB.roles_all()
        if not rows:
            print("\nNo devices in DB yet. Run the gateway once to populate.\n")
        else:
            print(f"\n{'UDN':<42} {'Name':<28} {'Host':<16} {'Server':<8} {'Renderer':<10} Last seen")
            print("─" * 115)
            for r in rows:
                print(f"{r['udn']:<42} {(r['name'] or ''):<28} "
                      f"{(r['host'] or ''):<16} "
                      f"{'yes' if r['is_server'] else '':<8} "
                      f"{'yes' if r['is_renderer'] else '':<10} "
                      f"{r['last_seen']}")
            print()
        return

    # ── --reset-devices: wipe and exit (don't start the gateway) ──
    if args.reset_devices:
        rows_before = DB.roles_all()
        conn = DB._connect()
        conn.execute("DELETE FROM device_roles")
        conn.commit()
        conn.close()
        print(f"\n✓  Cleared {len(rows_before)} device(s) from device_roles table.")
        print("   Start the gateway normally to rediscover all devices:")
        print("   ./setup.sh --run")
        print("   Add --probe <url> if a server doesn't respond to SSDP.\n")
        return

    setup_logging(debug=args.debug)

    lan_ip = get_lan_ip()
    url    = f"http://localhost:{args.port}/"

    print()
    print("  ┌──────────────────────────────────────────────┐")
    print("  │   DLNA / UPnP  →  IINA  Gateway  v2         │")
    print("  ├──────────────────────────────────────────────┤")
    print(f"  │  Web UI   :  {url:<33}│")
    print(f"  │  LAN IP   :  {lan_ip:<33}│")
    print("  ├──────────────────────────────────────────────┤")
    print("  │  Remote   :  http://<tailscale-ip>:8765/     │")
    print("  │  HTTPS    :  https://<tailscale-ip>:8443/    │")
    print("  │  Module tests:  python dlna_config.py        │")
    print("  │                 python dlna_discovery.py     │")
    print("  │                 python dlna_library.py       │")
    print("  │                 python dlna_player.py        │")
    print("  └──────────────────────────────────────────────┘")
    print()

    # Wire the indexer callback into discovery
    _disc._on_server_found = _on_server_found

    # Load persistent device role cache BEFORE any discovery thread starts.
    # This means the Uniti (or any other combined device seen before) is
    # classified as renderer-only instantly, with no race condition.
    DEVICE_ROLES.load()

    # Immediately probe all previously-known servers from the DB cache.
    # This gets AssetUPnP (and any other servers) online in < 1 second,
    # before SSDP or subnet scan have a chance to run.
    known_servers = DEVICE_ROLES.known_servers()
    if known_servers:
        log.info(f"Pre-probing {len(known_servers)} known server(s) from DB…")
        for s in known_servers:
            log.info(f"  → {s['name']!r}  {s['location']}")
            threading.Thread(
                target=_disc.probe_url,
                args=(s["location"], GW_UDN),
                daemon=True,
                name=f"probe-{s['udn'][:8]}").start()

    # SSDP discovery — finds MediaServers AND MediaRenderers
    threading.Thread(
        target=_disc.ssdp_discovery_thread,
        args=(lan_ip, GW_UDN),
        daemon=True, name="ssdp").start()

    # Gateway SSDP announcer — broadcasts ourselves as a MediaServer
    threading.Thread(
        target=gw_ssdp_announcer,
        args=(lan_ip, args.port),
        daemon=True, name="gw-ssdp").start()

    # Subnet scanner fallback — only fires if nothing was found via
    # pre-probe or SSDP (i.e. a genuinely fresh install with no DB cache).
    threading.Thread(
        target=_disc.subnet_scan_if_empty,
        args=(lan_ip, GW_UDN),
        daemon=True, name="subnet-scan").start()

    # Chromecast discovery (mDNS/zeroconf) — finds Cast-capable devices
    _cast_start()

    # CLI --probe or config.json probe (fresh install / manual override).
    # On subsequent runs the DB cache handles this — but honour explicit CLI.
    probe_url = args.probe
    if not probe_url and not known_servers:
        # Only fall back to config.json probe if DB has no known servers
        cfg = load_config()
        probe_url = cfg.get("probe", "")
        if probe_url:
            log.info(f"No DB cache yet — probing saved URL: {probe_url}")

    if probe_url:
        def _probe():
            _disc.probe_url(probe_url, GW_UDN)
            cfg = load_config()
            cfg["probe"] = probe_url
            save_config(cfg)
        threading.Thread(target=_probe, daemon=True).start()

    # HTTP server
    server = ThreadedHTTPServer((args.host, args.port), GatewayHandler)

    # ── HTTPS server (Tailscale certs) ───────────────────────────
    # Auto-detect cert files in working directory if not specified
    tls_cert = args.tls_cert
    tls_key  = args.tls_key
    if not tls_cert:
        import glob
        certs = glob.glob(os.path.join(os.getcwd(), "*.crt"))
        keys  = glob.glob(os.path.join(os.getcwd(), "*.key"))
        if len(certs) == 1 and len(keys) == 1:
            tls_cert = certs[0]
            tls_key  = keys[0]
            log.info(f"Auto-detected TLS cert: {os.path.basename(tls_cert)}")

    tls_server = None
    if tls_cert and tls_key:
        if os.path.isfile(tls_cert) and os.path.isfile(tls_key):
            try:
                ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
                ctx.load_cert_chain(tls_cert, tls_key)
                tls_server = ThreadedHTTPServer((args.host, args.tls_port), GatewayHandler)
                tls_server.socket = ctx.wrap_socket(tls_server.socket, server_side=True)
                threading.Thread(
                    target=tls_server.serve_forever,
                    daemon=True, name="https").start()
                log.info(f"HTTPS ready → https://localhost:{args.tls_port}/")
            except Exception as e:
                log.warning(f"HTTPS failed to start: {e}")
                tls_server = None
        else:
            log.warning(f"TLS cert/key not found: {tls_cert} / {tls_key}")

    if not args.no_browser:
        threading.Timer(1.0, open_browser, args=(args.port,)).start()

    tls_note = f"  HTTPS  → https://localhost:{args.tls_port}/" if tls_server else ""
    log.info(f"Gateway ready → {url}{('  |  ' + tls_note) if tls_note else ''}   (Ctrl-C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print()
        log.info("Shutting down…")
        _cast_stop()
        if tls_server:
            tls_server.shutdown()
        gw_ssdp_byebye(lan_ip, args.port)
        server.shutdown()


if __name__ == "__main__":
    main()