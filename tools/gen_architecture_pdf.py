#!/usr/bin/env python3
"""Generate ARCHITECTURE.PDF — a coloured diagram of the current (2.0)
DLNA Gateway architecture plus reference lists.

Page 1  : the architecture drawing + colour legend.
Page 2+ : the lists of every node in the drawing (programs P/*, tools
          T/*, devices D/*, external services E/*, scheduled jobs J/*)
          and the commands / options used.

This is a DOC GENERATOR, not part of the gateway runtime. It needs
`reportlab` (not a gateway dependency). Regenerate after architecture
changes:

    /tmp/docgen-venv/bin/python tools/gen_architecture_pdf.py
    # or: pip install reportlab && python tools/gen_architecture_pdf.py
"""
import math
import os

from reportlab.lib.colors import HexColor, white
from reportlab.lib.pagesizes import A3, landscape
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.graphics.shapes import Drawing, Rect, String, Line, Polygon
from reportlab.platypus import (SimpleDocTemplate, PageBreak, Paragraph,
                                Spacer, Table, TableStyle, KeepTogether)

# ── stream colours (the legend) ──────────────────────────────────────
BLUE  = HexColor(0x1f6feb)   # FROM     — inbound client → gateway
GREEN = HexColor(0x2da44e)   # TO       — gateway ↔ LAN device (control + data)
GREY  = HexColor(0x6e7781)   # INTERNAL — within gateway / DB / tools / jobs
RED   = HexColor(0xcf222e)   # EXTERNAL — gateway → internet over TLS

BLUE_L  = HexColor(0xddeeff)
GREEN_L = HexColor(0xdcf5e4)
GREY_L  = HexColor(0xeceef1)
RED_L   = HexColor(0xffe4e1)
GW_L    = HexColor(0xeef1f7)
GW_TILE = HexColor(0xffffff)
INK     = HexColor(0x24292f)

W, H = 1120, 720   # drawing canvas


# ── tiny drawing helpers ─────────────────────────────────────────────
def box(d, x, y, w, h, fill, stroke, rx=5, sw=1.1):
    d.add(Rect(x, y, w, h, rx=rx, ry=rx, fillColor=fill,
               strokeColor=stroke, strokeWidth=sw))


def txt(d, x, y, s, size=7, color=INK, bold=False, anchor='start'):
    st = String(x, y, s, fontSize=size, fillColor=color, textAnchor=anchor)
    st.fontName = 'Helvetica-Bold' if bold else 'Helvetica'
    d.add(st)


def arrow(d, x1, y1, x2, y2, color, w=1.5, dash=None):
    ln = Line(x1, y1, x2, y2, strokeColor=color, strokeWidth=w)
    if dash:
        ln.strokeDashArray = dash
    d.add(ln)
    ang = math.atan2(y2 - y1, x2 - x1)
    L, sp = 9, 0.42
    d.add(Polygon(points=[
        x2, y2,
        x2 - L * math.cos(ang - sp), y2 - L * math.sin(ang - sp),
        x2 - L * math.cos(ang + sp), y2 - L * math.sin(ang + sp),
    ], fillColor=color, strokeColor=color))


def cluster(d, x, y, w, h, title, header_color, body_color):
    box(d, x, y, w, h, body_color, header_color, rx=7, sw=1.4)
    box(d, x, y + h - 18, w, 18, header_color, header_color, rx=7, sw=0)
    txt(d, x + 8, y + h - 13, title, size=8.5, color=white, bold=True)


def tile(d, x, y, w, h, title, lines, accent):
    box(d, x, y, w, h, GW_TILE, accent, rx=4, sw=0.9)
    txt(d, x + 5, y + h - 11, title, size=7.5, color=accent, bold=True)
    yy = y + h - 22
    for ln in lines:
        txt(d, x + 5, yy, ln, size=6.6, color=INK)
        yy -= 9.3


# ── build the diagram ────────────────────────────────────────────────
def build_diagram():
    d = Drawing(W, H)

    # title
    txt(d, 6, H - 16, "DLNA Gateway — Architecture (2.0 · ASGI/Hypercorn)",
        size=15, bold=True, color=INK)
    txt(d, 6, H - 30,
        "What runs where · stream colours show direction/scope · node codes "
        "(P/T/D/E/J) index into the lists overleaf",
        size=8, color=GREY)

    # ── CLIENTS (blue) ───────────────────────────────────────────────
    cluster(d, 8, 380, 198, 270, "CLIENTS  (inbound)", BLUE, BLUE_L)
    tile(d, 18, 540, 178, 92, "P/c1  PWA front-end", [
        "static/: index.html, app.js,",
        "app.css, sw.js, manifest.json",
        "SW: APP shell network-first,",
        "ART cache-first, API SWR",
        "MediaSession, offline shell,",
        "video player (hls.js vendored)",
    ], BLUE)
    tile(d, 18, 398, 178, 110, "P/c2  Subsonic client", [
        "Amperfy / substreamer /",
        "play:Sub  (3rd-party iOS app)",
        "→ CarPlay over Tailscale",
        "talks /rest/* to the gateway,",
        "not to the music server",
    ], BLUE)

    # ── GATEWAY PROCESS (centre) ─────────────────────────────────────
    gx, gw_ = 228, 478
    cluster(d, gx, 180, gw_, 468,
            "GATEWAY PROCESS   (Hypercorn serves P/g27 dlna_asgi:app; "
            "P/g1 start_background_services spawns daemon threads)", INK, GW_L)
    ix, iw = gx + 9, gw_ - 18

    tile(d, ix, 580, iw, 44,
         "HTTP edge  (Hypercorn ASGI · TLS+HTTP/2 :8443 · plain :8765)", [
        "P/g27 dlna_asgi (FastAPI)  P/g28 dlna_asgi_bridge (legacy shim)  "
        "P/g30 dlna_events (SSE /api/events)",
    ], INK)
    tile(d, ix, 524, iw, 50, "API handlers  (/api/*  /rest/*  /gw/*)", [
        "P/g20 api_browse   P/g21 api_playback   P/g22 api_playlists",
        "P/g23 api_upnp (gateway-as-MediaServer, incl. Videos folder)   "
        "P/g24 api_subsonic   P/g25 api_radio",
    ], INK)
    tile(d, ix, 470, iw, 48, "Playback & control", [
        "P/g15 dlna_player  (RendererQueue / QUEUES per UDN)",
        "P/g14 dlna_avtransport (AVTransport+RenderingControl SOAP)   "
        "P/g16 dlna_stream_proxy (/stream)",
    ], GREEN)
    tile(d, ix, 426, iw, 38, "Discovery", [
        "P/g4 dlna_discovery (SSDP, heartbeat, subnet scan)   "
        "P/g5 dlna_registry   P/g6 dlna_devices",
    ], GREEN)
    tile(d, ix, 352, iw, 68, "Library & index", [
        "P/g7 dlna_library (LibraryDB: tracks/FTS5 w/ auto-heal + type-ahead/"
        "playlists/overrides/videos…)",
        "P/g8 db_pool (SQLite WAL pool)   P/g9 dlna_indexer (crawler)",
        "P/g10 dlna_providers/ (seam: upnp, localfs, mock)   "
        "P/g13 dlna_content (ContentDirectory SOAP)",
        "DATA → SQLite library.db   ·   config.json · gateway.log",
    ], GREY)
    tile(d, ix, 300, iw, 46, "LocalFs serving  (RoHaLocalFS)", [
        "P/g11 dlna_localfs_server — bit-perfect file server :8200 "
        "(Range/206, DLNA headers)",
        "/localfs/stream + /localfs/art + /localfs/video   "
        "P/g12 dlna_localfs_wiring",
    ], GREEN)
    tile(d, ix, 246, iw, 48, "Video  (GWMovies — V0–V3)", [
        "P/g31 dlna_video_index (5-min incremental scan → videos table)   "
        "P/g32 dlna_ffmpeg (probe/poster/",
        "HLS transcode)   PWA: /api/videos + /video/<id> + /video_hls "
        "(hls.js)   LG: /gw Videos folder",
    ], GREEN)
    tile(d, ix, 184, iw, 56, "Background fetchers  (event-driven, TLS out)", [
        "P/g17 dlna_art_fetcher (MusicBrainz + Cover Art Archive)   "
        "P/g29 dlna_art_cache (disk bytes)",
        "P/g19 dlna_lyrics (lrclib)   P/g33 dlna_geocode (Nominatim place "
        "names, sticky cache)",
        "P/g26 dlna_config (logging/config)",
    ], RED)

    # ── LAN DEVICES (green) ──────────────────────────────────────────
    dx = 718
    cluster(d, dx, 180, 246, 468, "LAN DEVICES  (direct, HTTP/SOAP — no TLS)",
            GREEN, GREEN_L)
    tile(d, dx + 10, 528, 226, 92, "D/1  Naim Uniti  (renderer)", [
        "UPnP MediaRenderer",
        "← AVTransport/RenderingControl",
        "  SOAP (SetURI/Play/Vol/poll)",
        "→ pulls audio BYTES from :8200",
        "also browses gateway playlists",
        "  (UPnP control point)",
    ], GREEN)
    tile(d, dx + 10, 440, 226, 80, "D/2  UPnP MediaServer", [
        "MinimServer / generic UPnP",
        "(AssetUPnP — decommissioned)",
        "via P/g10 UpnpProvider +",
        "P/g13 ContentDirectory SOAP",
    ], GREEN)
    tile(d, dx + 10, 376, 226, 56, "D/5  LG TV  (webOS)", [
        "DLNA control point + player",
        "browses /gw (incl. Videos),",
        "pulls bytes from :8200",
    ], GREEN)
    tile(d, dx + 10, 284, 226, 84, "D/3  Media files", [
        "/Volumes/SAMDATA/Music +",
        "/Volumes/SAMDATA/GWMovies",
        "(external drive, read-only)",
        "audio: P/g9-11 index + serve",
        "video: P/g31 scan, :8200 serve",
    ], GREEN)
    tile(d, dx + 10, 202, 226, 74, "D/4  Local binaries", [
        "fpcalc — beets fingerprints (T/a13)",
        "ffmpeg/ffprobe — video probe/",
        "poster/HLS transcode (P/g32)",
    ], GREY)

    # ── EXTERNAL services (red) ──────────────────────────────────────
    ex = 974
    cluster(d, ex, 306, 142, 342, "EXTERNAL  (TLS out)", RED, RED_L)
    exb = [
        ("E/x1  musicbrainz.org", "release-group MBID, year"),
        ("E/x2  coverartarchive", "front-cover presence"),
        ("E/x3  lrclib.net", "on-demand lyrics"),
        ("E/x4  radio-browser", "station catalogue"),
        ("E/x5  api.acoustid.org", "beets tool only (T/a13)"),
        ("E/x6  nominatim (OSM)", "GPS → place, video titles"),
    ]
    ey = 568
    for code, desc in exb:
        box(d, ex + 9, ey, 124, 42, GW_TILE, RED, rx=4, sw=0.9)
        txt(d, ex + 14, ey + 30, code, size=7.2, bold=True, color=RED)
        txt(d, ex + 14, ey + 19, desc, size=6.4, color=INK)
        ey -= 48

    # ── TOOLS (grey) ─────────────────────────────────────────────────
    cluster(d, 8, 8, 700, 164, "MAINTENANCE TOOLS  (tools/*.py — operate "
            "directly on library.db / music root)", GREY, GREY_L)
    tools = [
        "T/a1  regen_schema.py", "T/a2  prune_empty_music_dirs.py",
        "T/a3  find_corrupt_audio.py", "T/a4  find_duplicate_audio.py",
        "T/a6  relink_orphan_overrides.py",
        "T/a7  relink_playlists_to_localfs.py", "T/a8  audit_override_mismatches.py",
        "T/a9  correct_year_drift.py", "T/a10 improve_song_years.py",
        "T/a11 localfs_scan.py", "T/a12 localfs_serve.py",
        "T/a13 beets_enrich.py", "T/a14 post_beets_reindex.py",
    ]
    col_w = 232
    for i, t in enumerate(tools):
        cx = 18 + (i % 3) * col_w
        cy = 126 - (i // 3) * 26
        box(d, cx, cy, col_w - 12, 20, GW_TILE, GREY, rx=3, sw=0.8)
        txt(d, cx + 6, cy + 6, t, size=7, color=INK)

    # ── SCHEDULED JOBS / SCRIPTS (grey) ──────────────────────────────
    cluster(d, 718, 8, 246, 164, "SCHEDULED JOBS & SCRIPTS  (launchd)",
            GREY, GREY_L)
    jobs = [
        ("J/1  com.roha.dlna-gateway", "runs the gateway (launchd)"),
        ("J/2  cert-renew + renew-cert.sh", "weekly TLS cert (Mon 04:30)"),
        ("J/4  setup.sh", "venv + run / restart / probe"),
    ]
    jy = 124
    for code, desc in jobs:
        box(d, 728, jy, 226, 26, GW_TILE, GREY, rx=3, sw=0.8)
        txt(d, 734, jy + 15, code, size=7, bold=True, color=INK)
        txt(d, 734, jy + 5, desc, size=6.3, color=GREY)
        jy -= 32

    # ── LEGEND ───────────────────────────────────────────────────────
    lx, ly, lw, lh = 974, 8, 142, 290
    box(d, lx, ly, lw, lh, white, INK, rx=6, sw=1.3)
    txt(d, lx + 8, ly + lh - 14, "LEGEND — stream colours", size=8.2,
        bold=True, color=INK)
    leg = [
        (BLUE,  "FROM", "inbound client → gateway"),
        (GREEN, "TO", "gateway ↔ LAN device"),
        (GREY,  "INTERNAL", "within gateway / DB /"),
        (RED,   "EXTERNAL", "gateway → internet (TLS)"),
    ]
    yy = ly + lh - 36
    for col, name, desc in leg:
        arrow(d, lx + 10, yy + 4, lx + 40, yy + 4, col, w=2.4)
        txt(d, lx + 46, yy + 8, name, size=7.2, bold=True, color=col)
        txt(d, lx + 46, yy - 1, desc, size=6.1, color=INK)
        yy -= 30
    txt(d, lx + 8, yy + 4, "Node codes", size=7.6, bold=True, color=INK)
    yy -= 11
    for line in ["P = program / module", "T = maintenance tool",
                 "D = LAN device", "E = external service",
                 "J = scheduled job / script"]:
        txt(d, lx + 10, yy, line, size=6.3, color=INK)
        yy -= 10
    txt(d, lx + 8, yy - 2, "Dashed = optional /", size=6.2, color=GREY)
    txt(d, lx + 8, yy - 11, "intermittent path", size=6.2, color=GREY)

    # ── ARROWS ───────────────────────────────────────────────────────
    # FROM: clients → HTTP edge (blue)
    arrow(d, 206, 590, 228, 606, BLUE, 1.8)
    arrow(d, 206, 452, 228, 596, BLUE, 1.8)
    txt(d, 150, 662, "HTTPS:8443 / HTTP:8765", size=6.3, color=BLUE, bold=True)
    txt(d, 150, 654, "(Tailscale / LAN)", size=6.0, color=BLUE)
    # FROM: Naim browses gateway playlists (blue dashed, control-point)
    arrow(d, 718, 545, 706, 540, BLUE, 1.4, dash=[3, 2])
    # FROM: LG TV browses the /gw MediaServer incl. Videos (blue dashed)
    arrow(d, 718, 404, 706, 398, BLUE, 1.4, dash=[3, 2])

    # TO: gateway playback → Naim control (green)
    arrow(d, 706, 484, 718, 575, GREEN, 1.9)
    txt(d, 600, 492, "AVTransport SOAP", size=6.3, color=GREEN, bold=True)
    txt(d, 600, 484, "(control · volume)", size=6.0, color=GREEN)
    # TO: Naim/LG pull bytes from :8200 (green)
    arrow(d, 718, 555, 706, 335, GREEN, 1.9)
    txt(d, 582, 322, "HTTP Range → media bytes", size=6.3, color=GREEN, bold=True)
    txt(d, 582, 314, ":8200 (bit-perfect)", size=6.0, color=GREEN)
    # TO: providers/content ↔ UPnP MediaServer (green)
    arrow(d, 706, 386, 718, 468, GREEN, 1.6, dash=[4, 2])
    txt(d, 598, 396, "ContentDirectory SOAP", size=6.0, color=GREEN)
    # TO: localfs/library ↔ media files (green)
    arrow(d, 706, 308, 718, 320, GREEN, 1.7)
    txt(d, 636, 296, "read / serve", size=6.0, color=GREEN)

    # EXTERNAL: fetchers → external services (red)
    arrow(d, 706, 222, 974, 430, RED, 2.0)
    txt(d, 620, 208, "TLS lookups →", size=6.4, color=RED, bold=True)

    # INTERNAL: local binaries → fetchers/video (grey)
    arrow(d, 718, 220, 706, 210, GREY, 1.4)
    # INTERNAL: tools → gateway DB (grey)
    arrow(d, 360, 172, 360, 348, GREY, 1.5, dash=[4, 2])
    txt(d, 600, 358, "T/* write library.db", size=6.2, color=GREY)
    # INTERNAL: jobs → gateway (grey, launchctl kickstart — see J/1)
    arrow(d, 770, 172, 706, 200, GREY, 1.5, dash=[4, 2])

    return d


# ── reference list pages ─────────────────────────────────────────────
def list_pages():
    ss = getSampleStyleSheet()
    body = ParagraphStyle('body', parent=ss['BodyText'], fontSize=7.6,
                          leading=9.4, spaceAfter=0)
    code = ParagraphStyle('code', parent=body, fontName='Helvetica-Bold')
    h2 = ParagraphStyle('h2', parent=ss['Heading2'], fontSize=12,
                        spaceBefore=4, spaceAfter=6, textColor=INK)
    note = ParagraphStyle('note', parent=body, fontSize=7.2,
                          textColor=HexColor(0x57606a))

    def P(s, st=body):
        return Paragraph(s, st)

    def C(s):
        return Paragraph(s, code)

    def make_table(rows, widths, header_bg):
        t = Table(rows, colWidths=widths, repeatRows=1)
        t.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, 0), header_bg),
            ('TEXTCOLOR', (0, 0), (-1, 0), white),
            ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
            ('FONTSIZE', (0, 0), (-1, 0), 8),
            ('VALIGN', (0, 0), (-1, -1), 'TOP'),
            ('GRID', (0, 0), (-1, -1), 0.4, HexColor(0xd0d7de)),
            ('ROWBACKGROUNDS', (0, 1), (-1, -1),
             [white, HexColor(0xf6f8fa)]),
            ('LEFTPADDING', (0, 0), (-1, -1), 4),
            ('RIGHTPADDING', (0, 0), (-1, -1), 4),
            ('TOPPADDING', (0, 0), (-1, -1), 2.5),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 2.5),
        ]))
        return t

    story = []

    # ---- App programs / modules ----
    story.append(P("Node lists — every box in the drawing", h2))
    story.append(P("Codes match the diagram. <b>P</b> = program / module, "
                   "<b>T</b> = tool, <b>D</b> = device, <b>E</b> = external "
                   "service, <b>J</b> = scheduled job.", note))
    story.append(Spacer(1, 4))
    story.append(P("Application programs &amp; modules (P/*)", h2))
    prog_rows = [["Code", "File / location", "Responsibility"]]
    progs = [
        ("P/c1", "static/index.html, app.js, app.css, sw.js, manifest.json",
         "PWA front-end. Browse/playback UI, MediaSession lock-screen, "
         "Service-Worker caches (APP shell network-first since 2026-06-27 — "
         "cache is the offline fallback only, ART cache-first, API "
         "stale-while-revalidate), offline shell. Videos section with a "
         "same-origin &lt;video&gt; player + vendored hls.js for the "
         "on-demand HLS transcode."),
        ("P/c2", "(3rd-party iOS app — Amperfy / substreamer / play:Sub)",
         "Subsonic client for CarPlay over Tailscale. Talks /rest/* to the "
         "gateway; never to the music server directly."),
        ("P/g1", "dlna_gateway.py",
         "Module-wiring + start_background_services(): spawns the daemon "
         "threads (SSDP listener, pre-prober, subnet scanner, heartbeat, "
         "gateway announcer). Called from the dlna_asgi lifespan so "
         "`hypercorn dlna_asgi:app` boots the whole gateway. Its own stdlib "
         "HTTP edge + TLS were removed in 2.0 (Hypercorn owns the edge)."),
        ("P/g3", "dlna_routes.py", "GET_ROUTES / POST_ROUTES path→handler maps; "
         "the ASGI bridge (P/g28) mounts the not-yet-native ones."),
        ("P/g4", "dlna_discovery.py",
         "SSDP multicast listener, probe, subnet scanner, renderer/server "
         "heartbeat. Holds SERVERS / RENDERERS singletons."),
        ("P/g5", "dlna_registry.py",
         "Data classes + thread-safe ServerRegistry / RendererRegistry."),
        ("P/g6", "dlna_devices.py",
         "DeviceRoleCache — in-memory mirror of device_roles for zero-latency "
         "classification."),
        ("P/g7", "dlna_library.py",
         "LibraryDB — SQLite index, FTS5 search, playlists, album_art, "
         "play_counts, lyrics, metadata_overrides, favourites, radio, "
         "videos + geocode_cache. Composition root for DB-owning "
         "singletons + fetchers."),
        ("P/g8", "db_pool.py",
         "SQLite connection pool — WAL, thread-local conns, write "
         "serialization."),
        ("P/g9", "dlna_indexer.py",
         "Background crawler that walks a provider and populates LibraryDB "
         "(with FTS self-heal)."),
        ("P/g10", "dlna_providers/ (__init__, upnp.py, localfs.py, mock.py)",
         "LibraryProvider seam + registry. UpnpProvider wraps the UPnP SOAP "
         "path (kept for MinimServer); LocalFsProvider is the in-process "
         "backend (mutagen, content-hashed ids, watchdog)."),
        ("P/g11", "dlna_localfs_server.py",
         "RoHaLocalFS bit-perfect file server (:8200). Range-aware (206/416), "
         "DLNA-headered, /localfs/stream/<id> + /localfs/art/<id> + "
         "/localfs/video/<id> (the LG plays videos from here). "
         "Path-traversal defended."),
        ("P/g12", "dlna_localfs_wiring.py",
         "Boot-time wiring of the LocalFs provider (maybe_start_localfs); "
         "gated on LOCALFS_MUSIC_ROOT / config. Also starts the GWMovies "
         "video scan (P/g31, VIDEO_UDN) when LOCALFS_VIDEO_ROOT / "
         "localfs.video_root is set — incremental, re-run every 5 min."),
        ("P/g13", "dlna_content.py",
         "UPnP ContentDirectory SOAP client (cd_browse/cd_search). Reached "
         "only via P/g10 upnp.py."),
        ("P/g14", "dlna_avtransport.py",
         "UPnP AVTransport + RenderingControl SOAP (SetURI/Play/Stop/Pause, "
         "state/position probe, SetVolume)."),
        ("P/g15", "dlna_player.py",
         "RendererQueue (sequential per-renderer playback, gapless via "
         "SetNextAVTransportURI, watchdog) + QueueRegistry (QUEUES, one per "
         "UDN)."),
        ("P/g16", "dlna_stream_proxy.py",
         "Browser-audio HTTP proxy /stream (Range pass-through, 5-min idle "
         "timeout) + proxy_radio_stream /radio_stream (ICY de-interleave)."),
        ("P/g17", "dlna_art_fetcher.py",
         "AlbumArtFetcher — MusicBrainz release-group + Cover Art Archive "
         "lookup; sticky notfound cache; ~1 req/s."),
        ("P/g19", "dlna_lyrics.py",
         "On-demand lrclib lyrics; cached in the lyrics table; sticky "
         "positive + negative."),
        ("P/g20", "api_browse.py", "Browse / search / radio-shuffle API."),
        ("P/g21", "api_playback.py",
         "Playback, /stream route, /art proxy, /api/client_log, state, "
         "indexer management."),
        ("P/g22", "api_playlists.py", "Playlist CRUD endpoints."),
        ("P/g23", "api_upnp.py",
         "The gateway-as-MediaServer: a COMPLETE DLNA Media Server the Naim/LG "
         "browse. device.xml (MediaServer + X_DLNADOC + icons + ContentDirectory "
         "AND ConnectionManager), both SCPDs, ContentDirectory#Browse over the "
         "full library + the pre-browse handshake actions, ConnectionManager "
         "#GetProtocolInfo, GENA SUBSCRIBE + initial NOTIFY, SSDP announce + "
         "M-SEARCH responder. Root also lists a Videos folder (only when "
         "videos exist) so the LG TV browses + plays GWMovies natively "
         "(HEVC/MKV incl.)."),
        ("P/g24", "api_subsonic.py",
         "Subsonic-compatible /rest/* API (browse, playlists, favourites→"
         "starred, stream, cover, scrobble, internet radio) for CarPlay "
         "clients."),
        ("P/g25", "api_radio.py",
         "Internet-radio endpoints: radio-browser search, favourites (≤25), "
         "ICY now-playing."),
        ("P/g26", "dlna_config.py",
         "Constants (DB_FILE/CFG_FILE/LOG_FILE), logging setup, config "
         "load/save, .env load (if dotenv present), raise_fd_limit(8192)."),
        ("P/g27", "dlna_asgi.py",
         "2.0 — THE server (Hypercorn owns the whole edge). FastAPI app: "
         "TLS+HTTP/2 on :8443 via ALPN, plain :8765; owns the tailscale cert. "
         "Native routes for the read API, /art, /stream + /radio_stream relays, "
         "static/PWA, the Subsonic byte methods, the video surface (/api/videos, "
         "/video/&lt;id&gt;, /video_poster, /video_transcode, /video_hls — "
         "same-origin so the PWA's &lt;video&gt; works over HTTPS), and the "
         "Naim/LG-facing /gw/* UPnP surface (device.xml/desc.xml/events/control "
         "on the plain :8765 bind — Cleanup C folded it in here, retiring the "
         "separate device server). Remaining legacy handlers run via the "
         "bridge. Lifespan boots P/g1 services. docs_url off (no CDN call)."),
        ("P/g28", "dlna_asgi_bridge.py",
         "Shim that runs the legacy (h, params) handlers unchanged inside the "
         "ASGI app (a fake `h` captures _json/_html/_xml/send_error; runs in a "
         "threadpool). Routes are rewritten native one batch at a time, then "
         "dropped from the bridge."),
        ("P/g29", "dlna_art_cache.py",
         "On-disk cover-art byte cache keyed by source URL. art_fetch_cached() "
         "(in P/g21) fronts art_fetch so /art and Subsonic getCoverArt serve "
         "repeat covers from disk (across clients + restarts) instead of "
         "re-fetching coverartarchive / re-decoding embedded art. TTL + size "
         "capped; art_cache/ gitignored."),
        ("P/g30", "dlna_events.py",
         "EventBus/EVENTS — thread-safe publish to the asyncio loop; native "
         "GET /api/events (SSE). Publishers: RendererQueue state, index status "
         "transitions, discovery changes. The PWA opens an EventSource as a "
         "polling accelerator (fallback intact)."),
        ("P/g31", "dlna_video_index.py",
         "GWMovies scanner (V1) → the videos table. Stable id = "
         "sha1(rel_path); metadata via P/g32 probe (filename/mtime fallback "
         "when ffprobe absent); place name via P/g33; poster frame extracted "
         "per video. Incremental (mtime,size) + prunes gone files; "
         "force=True rebuilds."),
        ("P/g32", "dlna_ffmpeg.py",
         "Optional ffmpeg/ffprobe helpers (V0/V3, same posture as fpcalc — "
         "absent binaries degrade gracefully). probe() metadata/GPS/capture "
         "time, extract_poster(), transcode_cmd() for the on-demand "
         "H.264/AAC HLS transcode (-pix_fmt yuv420p so 10-bit HEVC plays), "
         "build_display_title() for the '&lt;place&gt;_YYYYMMDD_HHMM' "
         "fallback title."),
        ("P/g33", "dlna_geocode.py",
         "Reverse-geocode GPS → place name via Nominatim/OSM (E/x6), "
         "cache-first in geocode_cache. UA + 1.1 s rate limit per OSM "
         "policy; failures NOT cached (retry later), definitive no-name "
         "cached sticky as ''."),
    ]
    for c, f, r in progs:
        prog_rows.append([C(c), P(f), P(r)])
    story.append(make_table(prog_rows, [38, 200, 822], INK))
    story.append(PageBreak())

    # ---- Tools ----
    story.append(P("Maintenance tools (T/*) — tools/*.py", h2))
    story.append(P("Operate directly on library.db and/or the music root. "
                   "All default to dry-run / report-only; deletions move to "
                   "Trash unless --hard-delete. Each has a unit-test sibling "
                   "tools/test_*.py.", note))
    tool_rows = [["Code", "File", "Purpose & key options"]]
    tools = [
        ("T/a1", "regen_schema.py",
         "Regenerate the committed schema.sql artifact after any schema "
         "change. <b>--check</b> = fail if stale (the test_schema_sync gate)."),
        ("T/a2", "prune_empty_music_dirs.py",
         "Trash directories whose subtree has zero music files. "
         "<b>--dry-run -v --limit N --exts a,b --hard-delete -y</b>."),
        ("T/a3", "find_corrupt_audio.py",
         "Flag files with bad/zero magic bytes for their extension. "
         "Writes corrupt-audio.txt. <b>--trash --hard-delete --limit --exts "
         "--out -y</b>."),
        ("T/a4", "find_duplicate_audio.py",
         "Find duplicate recordings on disk (same acoustid metadata); ranks "
         "a winner by bit-depth/sample-rate/size. <b>--trash --hard-delete "
         "-v -y</b>."),
        ("T/a6", "relink_orphan_overrides.py",
         "Relink metadata_overrides orphaned by a UPnP rescan via d-id + "
         "fuzzy (artist,title). <b>--apply --db</b>."),
        ("T/a7", "relink_playlists_to_localfs.py",
         "Repoint playlist_tracks/favourites at RoHaLocalFS by normalised "
         "metadata; prune no-match. <b>--apply --no-backup --no-prune-favs "
         "-y</b>."),
        ("T/a8", "audit_override_mismatches.py",
         "Delete acoustid overrides whose artist AND title both mismatch the "
         "track (d-id collision repair). <b>--clean --top -y</b>."),
        ("T/a9", "correct_year_drift.py",
         "Rewrite metadata_overrides.year to the earliest plausible year "
         "evidenced by another copy in the library. <b>--apply --top --db "
         "-y</b>."),
        ("T/a10", "improve_song_years.py",
         "Query MusicBrainz for each song's earliest recording year → "
         "song_year_cache → apply. <b>--lookup --apply --limit -v</b>."),
        ("T/a11", "localfs_scan.py",
         "Standalone LocalFs indexer (CLI). NOTE: do not run against the live "
         "DB without a base_url — leaves placeholder URLs."),
        ("T/a12", "localfs_serve.py",
         "Standalone LocalFs file-server launcher for testing the :8200 "
         "serving path."),
        ("T/a13", "beets_enrich.py",
         "Run the beets tag-in-place enrichment batch (docs/enrichment.md). "
         "Safe wrapper around `beet import`: enforces write:yes/copy:no/"
         "move:no before any write. <b>--write-config --quiet --timid "
         "--album --revisit --reindex --gateway --dry-run -y</b>."),
        ("T/a14", "post_beets_reindex.py",
         "The post-beets step: drop the historical AcoustID metadata_overrides "
         "(LocalFs URLs are path-stable, so old acoustid rows would re-mask "
         "beets' fresh tags) then reindex LocalFs. <b>--apply --dry-run "
         "--no-clean --no-reindex --no-backup --udn --gateway --db -y</b>."),
    ]
    for c, f, p in tools:
        tool_rows.append([C(c), P(f), P(p)])
    story.append(make_table(tool_rows, [40, 175, 845], GREY))
    story.append(Spacer(1, 10))

    # ---- Devices / External / Jobs ----
    story.append(P("LAN devices (D/*), external services (E/*), scheduled "
                   "jobs (J/*)", h2))
    de_rows = [["Code", "Name", "Role / protocol"]]
    de = [
        ("D/1", "Naim Uniti", "UPnP MediaRenderer. Receives AVTransport+"
         "RenderingControl SOAP (control/volume); pulls audio bytes over HTTP "
         "Range from :8200; also browses gateway playlists as a control "
         "point. Gateway is NOT in its audio path → bit-perfect.", GREEN),
        ("D/2", "UPnP MediaServer", "MinimServer / generic UPnP (AssetUPnP "
         "decommissioned). Browsed via P/g10 UpnpProvider + P/g13 "
         "ContentDirectory SOAP. Kept for any non-LocalFs source.", GREEN),
        ("D/3", "Media files", "/Volumes/SAMDATA/Music + "
         "/Volumes/SAMDATA/GWMovies (external drive, read-only). Audio: "
         "indexed by P/g9, tags read by localfs provider, bytes served by "
         "P/g11. Video: scanned by P/g31, served by P/g11 "
         "(/localfs/video).", GREEN),
        ("D/4", "fpcalc · ffmpeg/ffprobe", "Local CLI binaries (brew). "
         "fpcalc fingerprints for the beets tool (T/a13); ffmpeg/ffprobe "
         "probe metadata, extract posters and run the on-demand HLS "
         "transcode (P/g32). Internal/local — not network.", GREY),
        ("D/5", "LG TV (webOS)", "DLNA control point + player. Browses the "
         "gateway's /gw MediaServer (incl. the Videos folder) and pulls "
         "bytes from :8200. Plays HEVC/MKV natively — the PWA transcode "
         "path is browser-only.", GREEN),
        ("E/x1", "musicbrainz.org", "GET /ws/2/release-group — MBID + original "
         "year. UA + 1.1 s rate limit required.", RED),
        ("E/x2", "coverartarchive.org", "HEAD /release-group/{mbid}/front-500 "
         "— cover presence.", RED),
        ("E/x3", "lrclib.net", "GET /api/get — on-demand lyrics for the "
         "playing track.", RED),
        ("E/x4", "*.api.radio-browser.info", "GET /json/stations/search — "
         "internet-radio catalogue (HLS filtered).", RED),
        ("E/x5", "api.acoustid.org", "fingerprint → MusicBrainz metadata — used "
         "ONLY by the beets tool (T/a13) now; the in-process AcoustID worker "
         "was removed in 2.0.", RED),
        ("E/x6", "nominatim.openstreetmap.org", "GET /reverse — GPS → place "
         "name for video display titles (P/g33). UA + 1.1 s rate limit per "
         "OSM policy; sticky cache in geocode_cache.", RED),
        ("J/1", "com.roha.dlna-gateway", "LaunchAgent that runs the gateway. "
         "Restart: launchctl kickstart -k gui/$(id -u)/com.roha.dlna-gateway.",
         GREY),
        ("J/2", "com.roha.dlna-cert-renew + renew-cert.sh", "Weekly TLS cert "
         "renewal (Mon 04:30; no-op unless &lt;30 days). cert-renewal.log.",
         GREY),
        ("J/4", "setup.sh", "venv setup + run / restart / probe. --run "
         "--restart --no-browser --debug --probe URL --list-devices "
         "--reset-devices. See the setup.sh options reference.", GREY),
    ]
    for c, n, r, _col in de:
        de_rows.append([C(c), P(n), P(r)])
    story.append(make_table(de_rows, [40, 200, 820], GREEN))
    story.append(PageBreak())

    # ---- Commands ----
    story.append(P("Commands &amp; options used in the drawing", h2))
    cmd_rows = [["Context", "Command"]]
    cmds = [
        ("Run (J/4)", "./setup.sh --run [--no-browser] [--debug] "
         "[--probe http://…] [--list-devices] [--reset-devices]"),
        ("Restart (J/4)", "./setup.sh --restart   "
         "(refresh venv/deps + launchctl kickstart -k gui/$(id -u)/"
         "com.roha.dlna-gateway)"),
        ("Cert (J/2)", "./renew-cert.sh [--force]   ·   launchctl kickstart "
         "gui/$(id -u)/com.roha.dlna-cert-renew"),
        ("Subsonic auth", "launchctl setenv SUBSONIC_PASSWORD … ; "
         "launchctl getenv SUBSONIC_PASSWORD"),
        ("Full test suite", "python tests/run_all.py [--offline | --frontend | "
         "--frontend-only | http://host:8765]"),
        ("Unit / Playwright", "python3 -m unittest tests.test_player … ; "
         ".venv/bin/pytest tests/frontend -v"),
        ("Chaos (live)", "python3 tests/chaos.py --iterations N --workers M "
         "[--seed S] [--quiet] [--base https://host:8443]"),
        ("Load (live)", "python3 tests/load_stream.py --concurrency 40 "
         "--count 80 [--gateway https://127.0.0.1:8443 --insecure] "
         "[--max-p95 6]   (threadpool-starvation guard)"),
        ("Safari / iOS smoke", ".venv/bin/python tests/frontend/"
         "safari_smoke.py   ·   ios_sim_smoke.py (Appium :4723 + a booted "
         "Simulator)   — opt-in, not in run_all.py"),
        ("Schema gate (T/a1)", "python3 tools/regen_schema.py [--check]"),
        ("Module self-tests", "python dlna_discovery.py | dlna_content.py "
         "<url> | dlna_library.py | db_pool.py | dlna_player.py"),
        ("Range / 206 check", "curl -r 0-1023 -D - "
         "http://&lt;host&gt;:8200/localfs/stream/&lt;id&gt; -o /dev/null"),
        ("Tools (T/a2–a12)", "python3 tools/&lt;tool&gt;.py [--dry-run|--apply|"
         "--trash|--hard-delete|--limit N|--exts a,b|-v|-y] (see the tools "
         "table above)"),
    ]
    for ctx, cmd in cmds:
        cmd_rows.append([P(f"<b>{ctx}</b>", body), C(cmd)])
    story.append(make_table(cmd_rows, [150, 910], INK))
    story.append(Spacer(1, 8))
    story.append(P("Ports (2.0): 8443 HTTPS — Hypercorn TLS + HTTP/2 (ALPN), "
                   "tailscale cert · 8765 plain HTTP, incl. the Naim/LG-facing "
                   "/gw/* UPnP surface (Cleanup C folded the old :8770 device "
                   "tier into this bind; SSDP advert points here) · 8200 "
                   "RoHaLocalFS file server (audio + /localfs/video) · 26125 "
                   "(legacy AssetUPnP, decommissioned). The HTTP/2 + "
                   "app-owned-TLS roadmap is now DONE — Hypercorn terminates "
                   "TLS/h2 natively. Cutover: docs/CUTOVER_RUNBOOK.md + "
                   "CUTOVER_LAUNCHD.md.", note))

    # ---- Tool options reference (per tool, every flag explained) ----
    story.append(PageBreak())
    story.append(P("Tool options reference — what each flag does", h2))
    story.append(P("Every option for every maintenance tool, with its "
                   "meaning. Codes match the diagram (T/aN) and the tools "
                   "table. Unless noted, file-deleting tools default to "
                   "dry-run / report-only and move to the macOS Trash "
                   "(recoverable ~30 days) — <b>--hard-delete</b> is the "
                   "permanent, non-recoverable variant.", note))
    opt_hdr = ParagraphStyle('opthdr', parent=body, fontName='Helvetica-Bold',
                             fontSize=9, spaceBefore=9, spaceAfter=2,
                             textColor=INK)

    OPTIONS = [
     ("T/a1", "regen_schema.py", "Regenerate the committed schema.sql.", [
        ("(no args)", "Rewrite schema.sql from the live LibraryDB schema."),
        ("--check", "Don't write; exit non-zero if schema.sql is stale "
         "(the test_schema_sync gate)."),
     ]),
     ("T/a2", "prune_empty_music_dirs.py",
      "Trash directories whose subtree has zero music files.", [
        ("<root>", "Music root to walk (positional)."),
        ("--dry-run", "Print decisions; touch nothing."),
        ("-v / --verbose", "Also log every KEPT directory (default: only "
         "deletions print)."),
        ("--limit N", "Stop after evaluating N dirs; when the limit is hit, "
         "NO deletions run (a partial picture = safety belt)."),
        ("--exts a,b,c", "Override the music-extension set (commas, dot "
         "optional)."),
        ("--hard-delete", "Permanent rm -rf instead of Trash. NOT "
         "recoverable."),
        ("-y / --yes", "Skip the confirmation prompt."),
     ]),
     ("T/a3", "find_corrupt_audio.py",
      "Flag files whose magic bytes are wrong/zero for their extension.", [
        ("<root>", "Music root to scan (positional)."),
        ("-v / --verbose", "Also log every OK file (default: only corrupt)."),
        ("--limit N", "Stop after scanning N files (0 = no limit); halts the "
         "delete step when hit."),
        ("--exts a,b,c", "Override the audio-extension list."),
        ("--out PATH", "Where to write the corrupt-paths list (default "
         "./corrupt-audio.txt; pass /dev/null to suppress)."),
        ("--trash", "Move flagged files to the Trash."),
        ("--hard-delete", "Permanent unlink instead of Trash. Mutually "
         "exclusive with --trash."),
        ("-y / --yes", "Skip the confirmation prompt before deleting."),
     ]),
     ("T/a4", "find_duplicate_audio.py",
      "Find duplicate recordings on disk (same acoustid metadata); keeps a "
      "ranked winner.", [
        ("<root>", "Music root to scan (positional)."),
        ("-v / --verbose", "Per-URL ambiguity / not-found logs."),
        ("--trash", "Move the loser files to the Trash (winner kept)."),
        ("--hard-delete", "Permanent delete of losers. NOT recoverable."),
        ("-y / --yes", "Skip the confirmation prompt."),
     ]),
     ("T/a6", "relink_orphan_overrides.py",
      "Relink metadata_overrides orphaned by a UPnP rescan (d-id + fuzzy "
      "artist/title).", [
        ("(default)", "Dry-run preview — no mutation."),
        ("--apply", "Perform the relinks."),
        ("--db PATH", "library.db path."),
     ]),
     ("T/a7", "relink_playlists_to_localfs.py",
      "Repoint playlists/favourites at RoHaLocalFS by normalised metadata.", [
        ("(default)", "Dry-run preview."),
        ("--apply", "Commit the relinks (auto-backs up library.db first)."),
        ("--no-backup", "Skip the automatic library.db backup on --apply."),
        ("--no-prune-favs", "Keep album_favourites that no longer match any "
         "LocalFs album."),
        ("-y / --yes", "Skip the confirmation prompt."),
     ]),
     ("T/a8", "audit_override_mismatches.py",
      "Delete acoustid overrides whose artist AND title both mismatch the "
      "track.", [
        ("(default)", "Dry-run, lists the top 30 suspects."),
        ("--clean", "Delete the suspect acoustid override rows."),
        ("--top N", "Show N rows (0 = full list)."),
        ("-y / --yes", "Non-interactive (skip the confirm)."),
     ]),
     ("T/a9", "correct_year_drift.py",
      "Rewrite metadata_overrides.year to the earliest plausible year from "
      "another copy in the library.", [
        ("(default)", "Dry-run, top 30 candidates."),
        ("--apply", "Write the corrections (as source='manual')."),
        ("--top N", "Preview N candidates (0 = all)."),
        ("--db PATH", "library.db path."),
        ("-y / --yes", "Non-interactive apply."),
     ]),
     ("T/a10", "improve_song_years.py",
      "Query MusicBrainz for each song's earliest recording year → cache → "
      "apply.", [
        ("(default)", "Dry-run preview — no MB calls, no DB writes."),
        ("--lookup", "Query MB for uncached (artist,title) groups; fill "
         "song_year_cache (sticky)."),
        ("--apply", "Write cached hits onto tracks (metadata_overrides.year, "
         "source='manual')."),
        ("--limit N", "Cap the number of groups queried (for testing)."),
        ("-v / --verbose", "Per-group logging."),
     ]),
     ("T/a11", "localfs_scan.py",
      "Standalone LocalFs indexer (CLI). Don't run against the live DB "
      "without a base_url — it leaves placeholder URLs.", [
        ("--root PATH", "Music root to scan (default $LOCALFS_MUSIC_ROOT or "
         "/Volumes/SAMDATA/Music)."),
        ("--force", "Ignore the (mtime, size) cache and re-tag every file. "
         "Use after schema changes."),
        ("--compare", "Skip scanning — just print tracks/albums per UDN "
         "currently in library.db."),
        ("--db PATH", "library.db path."),
        ("-v / --verbose", "Per-file logging."),
     ]),
     ("T/a12", "localfs_serve.py",
      "Standalone RoHaLocalFS file-server launcher (test the :8200 path).", [
        ("--port N", "HTTP port (default 8200 / $LOCALFS_PORT)."),
        ("--host ADDR", "Listen address (default 0.0.0.0 so the Naim can "
         "reach it)."),
        ("--db PATH", "library.db path."),
        ("--root PATH", "Allowed music root; repeatable. Defaults to "
         "$LOCALFS_MUSIC_ROOT."),
        ("-v / --verbose", "Request logging."),
     ]),
     ("T/a13", "beets_enrich.py",
      "Run the beets tag-in-place enrichment batch (docs/enrichment.md). "
      "Safe wrapper around `beet import`. Deps: brew install chromaprint beets "
      "(NOT pip — a Homebrew python upgrade wipes a pip install; that is how "
      "the 2026-06 install died), then add musicbrainzngs + pyacoustid to the "
      "keg venv (beets 2.x pluginized MusicBrainz — the musicbrainz plugin + "
      "musicbrainzngs are REQUIRED or it matches nothing).", [
        ("(no --quiet/--timid)", "Interactive: beets prompts you per album "
         "(apply / skip / …); strong matches auto-apply."),
        ("--write-config", "Write the prog-tuned tag-in-place "
         "~/.config/beets/config.yaml (enables the musicbrainz metadata "
         "plugin; backs up any existing) and exit. Run this first."),
        ("--music-root PATH", "Library root to import (default "
         "/Volumes/SAMDATA/Music)."),
        ("--album PATH", "Import a single album directory instead of the "
         "whole root."),
        ("--config PATH", "beets config path (default "
         "~/.config/beets/config.yaml)."),
        ("--quiet", "Unattended BULK pass: auto-accept only strong matches "
         "(≥ strong_rec_thresh 0.80), skip the rest, no prompts "
         "(beet import -q)."),
        ("--timid", "Most cautious: prompt for EVERY match (per-change "
         "review). Mutually exclusive with --quiet."),
        ("--revisit", "Re-import a directory beets already recorded as done "
         "(-I / noincremental). Use after re-tagging an album."),
        ("--reindex", "After import, find the LocalFs server UDN via "
         "/api/servers and POST /api/index/rebuild so the gateway picks up "
         "the new tags."),
        ("--gateway URL", "Gateway base URL for --reindex (default "
         "http://127.0.0.1:8765)."),
        ("--udn UDN", "Explicit server UDN to reindex (default: auto-pick "
         "the uuid:localfs-* server)."),
        ("-n / --dry-run", "Print the resolved beet command + safety report; "
         "do NOT invoke beets (beets has no true dry-run of its own)."),
        ("-y / --yes", "Skip the in-place-write backup-warning confirmation."),
     ]),
     ("T/a14", "post_beets_reindex.py",
      "After beets has tagged files in place, make its work visible: clear "
      "the AcoustID metadata_overrides (LocalFs URLs are PATH-stable, so the "
      "COALESCE pass would otherwise re-lay old acoustid rows over beets' "
      "fresh tags) THEN reindex LocalFs. source='manual' is never touched; "
      "notfound/video_skip carry NULL metadata so they mask nothing.", [
        ("(default)", "Dry-run: print the override breakdown + the planned "
         "clear/reindex; change nothing."),
        ("--apply", "Actually delete acoustid overrides + start the reindex "
         "(auto-backs up library.db first)."),
        ("-n / --dry-run", "Explicit preview alias (the default when --apply "
         "is absent)."),
        ("--no-clean", "Skip clearing overrides (reindex only)."),
        ("--no-reindex", "Skip the reindex (clean only)."),
        ("--no-backup", "Skip the library.db backup before deleting."),
        ("--udn UDN", "Server UDN to reindex (default: auto-pick "
         "uuid:localfs-*)."),
        ("--gateway URL", "Gateway base URL (default http://127.0.0.1:8765)."),
        ("--db PATH", "library.db path."),
        ("-y / --yes", "Skip the confirmation prompt."),
     ]),
     ("J/4", "setup.sh",
      "Bootstrap + run the gateway. Finds Python 3.14+, creates/repairs the "
      ".venv, installs requirements.txt, then either runs the gateway "
      "(--run, exec in the foreground), restarts the launchd-managed copy "
      "(--restart), or just finishes setup. Unknown flags after --run are "
      "forwarded verbatim to dlna_gateway.py.", [
        ("(no flags)", "Set up only: venv + dependencies, then print the "
         "'how to run' summary. Does not start anything."),
        ("--run", "Set up, then exec the gateway in the foreground on :8765 "
         "(blocks; Ctrl-C to stop). All flags below it are passed through to "
         "dlna_gateway.py."),
        ("--restart", "Refresh the venv/deps, then restart the launchd "
         "gateway via launchctl kickstart -k gui/$(id -u)/"
         "com.roha.dlna-gateway (the launchd-correct restart — a bare kill "
         "races launchd's respawn). Aborts with install hints if the "
         "LaunchAgent isn't loaded. Mutually informative with --run; "
         "--restart wins if both are given."),
        ("--no-browser", "(forwarded) Don't auto-open the browser on start."),
        ("--debug", "(forwarded) Verbose logging."),
        ("--probe http://…", "(forwarded) Add a UPnP server manually by its "
         "device-description URL."),
        ("--list-devices", "(forwarded) Print the known-devices table and "
         "exit."),
        ("--reset-devices", "(forwarded) Wipe the device DB and exit."),
        ("--port N", "(forwarded) HTTP port (default 8765)."),
     ]),
    ]
    for tcode, tname, purpose, opts in OPTIONS:
        rows = [["Option", "Meaning"]]
        for o, m in opts:
            rows.append([C(o), P(m)])
        block = [Paragraph(f"<b>{tcode}&nbsp;&nbsp;{tname}</b> — {purpose}",
                           opt_hdr),
                 make_table(rows, [185, 945], GREY)]
        story.append(KeepTogether(block))
        story.append(Spacer(1, 2))

    return story


def main():
    # docs/ is where tracked PDFs live (.gitignore negates docs/*.pdf).
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    out = os.path.join(repo, "docs", "ARCHITECTURE.PDF")
    doc = SimpleDocTemplate(out, pagesize=landscape(A3),
                            leftMargin=14, rightMargin=14,
                            topMargin=14, bottomMargin=14,
                            title="DLNA Gateway — Architecture",
                            author="dlna-gateway")
    story = [build_diagram(), PageBreak()] + list_pages()
    doc.build(story)
    print("wrote", out)


if __name__ == "__main__":
    main()
