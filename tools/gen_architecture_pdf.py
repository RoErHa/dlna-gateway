#!/usr/bin/env python3
"""Generate ARCHITECTURE.PDF — a coloured diagram of the current (2.1)
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
from reportlab.graphics.shapes import (Drawing, Rect, String, Line,
                                       Polygon, Circle)
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

# Two more lane colours for the 2026-09-21 four-journey redraw. AMBER is
# the BATCH lane — work you start yourself, which by definition cannot
# affect playback; it doubles as the batch tag in the externals panel.
# PINK is the video lane, the only journey with machine learning in it.
AMBER   = HexColor(0x9a5b00)
AMBER_L = HexColor(0xfcf2e2)
PINK    = HexColor(0xa8326b)
PINK_L  = HexColor(0xfbe9f1)
GW_TILE = HexColor(0xffffff)
INK     = HexColor(0x24292f)

# Canvas aspect must match the PAGE, or reportlab scales to fit the
# wider dimension and leaves a band of dead paper. A3 landscape is
# 1190x842pt = 1.414; 1120x792 is the same ratio.
W, H = 1120, 792   # drawing canvas — A3-landscape aspect


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
# Redrawn 2026-09-21 as FOUR JOURNEYS rather than four static columns.
#
# The old drawing was a map of boxes that exist: it answered "what
# modules are there" but not "how does this work", you could not trace a
# track from disk to speaker, and it silently omitted four external
# services. Worse, it gave no way to tell a service hit WHILE YOU LISTEN
# from one hit by a batch tool you run yourself — which is the only
# distinction that matters operationally, because just five of them can
# affect playback.
#
# Each lane reads left to right and answers one question. The node
# tables overleaf (P/T/D/E/J) are unchanged — they were always the
# reference half and they work.

LANES = [
    ("1", "A track reaches your ears",
     "what happens when you press play", GREEN, GREEN_L, [
        ("Files on disk",
         ["Music, audiobooks and video on", "external volumes, read-only."],
         "/Volumes/SAMDATA  (D/3)"),
        ("Index",
         ["Tags read once per file;", "a folder is an album."],
         "P/g10 localfs · P/g9 indexer"),
        ("library.db",
         ["One SQLite index. WAL,", "FTS5 search, per-thread conns."],
         "P/g7 LibraryDB · P/g8 pool"),
        ("Choose",
         ["Browse or search, then queue", "to a renderer or the browser."],
         "P/g20 api_browse · P/g15 player"),
        ("Bytes move",
         ["The renderer pulls from :8200", "ITSELF — the gateway is not in",
          "the audio path. Browser audio", "uses the Range proxy."],
         "P/g11 localfs_server · P/g16 /stream"),
     ]),
    ("2", "The library learns what it knows",
     "batch tools you run; never during playback", AMBER, AMBER_L, [
        ("Tag in place",
         ["beets writes clean tags and", "MBIDs into the files."],
         "T/a13 beets_enrich · T/a14 post_beets"),
        ("Resolve artists",
         ["Artist to MusicBrainz id.", "THE KEYSTONE: four sources",
          "key off it.  2,889/3,289."],
         "T/a15 artist_mbid"),
        ("Fetch facts",
         ["Life-span, birthplace, genres,", "biography, best-known, and",
          "band line-ups with dates."],
         "T/a16 artist_meta"),
        ("Fetch credits",
         ["Composer/lyricist by browsing", "each artist's WORKS — 100 per",
          "request, an hour not sixteen."],
         "T/a17 track_credits"),
        ("Housekeeping",
         ["Orphan relinks, duplicate and", "corrupt-file audits, playlist",
          "repair.  All dry-run by default."],
         "T/a2-a12"),
     ]),
    ("3", "A phone clip becomes a browsable memory",
     "the only journey with machine learning in it", PINK, PINK_L, [
        ("iPhone to Immich",
         ["Phone backup. Immich runs FACE", "RECOGNITION in its own Postgres",
          "— never written into the files."],
         "Immich  (separate service)"),
        ("Import originals",
         ["Copies Immich's originals, never", "its transcodes. Dedup by BLAKE2b",
          "content hash."],
         "T/a18 immich_import"),
        ("Scan and title",
         ["Every 5 min. Titles from METADATA,", "not filenames: GPS reverse-geocoded",
          "plus capture time. No GPS? inferred", "from temporal neighbours."],
         "P/g31 video_index · P/g33 geocode"),
        ("Pull the ML back",
         ["Person tags over Immich's REST API,", "matched by SHA1 CONTENT CHECKSUM",
          "— Immich's paths are container", "paths and cannot be mapped."],
         "T/a19 immich_people_sync"),
        ("Browse it",
         ["By date, country/place, person,", "or all. Native on the TV; the PWA",
          "gets an on-demand HLS transcode."],
         "P/g23 upnp_browse_video · P/g32 ffmpeg"),
     ]),
    ("4", "Four ways to reach the same library",
     "one index, four protocols", BLUE, BLUE_L, [
        ("PWA  (P/c1)",
         ["Browse, playback, lyrics, the", "artist panel, video."],
         "HTTPS/2 :8443 · /api/*"),
        ("Naim  (D/1)",
         ["Browses the gateway AS a DLNA", "Media Server; pulls bytes itself."],
         "UPnP SOAP :8765 /gw/*"),
        ("CarPlay  (P/c2)",
         ["Amperfy over Tailscale, including", "audiobook bookmarks."],
         "Subsonic /rest/*"),
        ("LG TV  (D/5)",
         ["The GWMovies tree; plays HEVC", "and MKV natively."],
         "UPnP · no transcode"),
        ("Renderers, generally",
         ["Any UPnP MediaRenderer. Volume via", "RenderingControl — hardware, so",
          "the audio stays bit-perfect."],
         "P/g14 avtransport"),
     ]),
]

# Every external service, and WHEN it is reached. `live` can affect
# playback; `batch` cannot, because you started it yourself.
EXTERNALS = [
    ("musicbrainz.org", "live", "Release-group for album art; artist facts and works for the sweeps.", "1 req/s · UA required"),
    ("coverartarchive.org", "live", "Front-cover presence for an album MBID.", "follows 307 to archive.org"),
    ("lrclib.net", "live", "Lyrics, on the button. Cached forever, hit or miss.", "once per track URL"),
    ("*.api.radio-browser.info", "live", "Internet-radio station catalogue.", "DNS round-robin · HLS filtered"),
    ("nominatim.openstreetmap.org", "live", "GPS to place name for video titles. THE PRIVACY-RELEVANT ONE.", "1.1 s/req · sticky cache · opt-out"),
    ("upload.wikimedia.org", "live", "Artist photos, through the /art proxy and its disk cache.", "freely licensed · cached"),
    ("en.wikipedia.org", "batch", "Artist biographies. CC BY-SA: stored only WITH its url.", "no key · 74% coverage"),
    ("ws.audioscrobbler.com", "batch", "Last.fm best-known-for, ranked by real listening.", "APPLICATION key · 100%"),
    ("openlibrary.org", "batch", "Audiobook author, title and series number.", "~1 req/s · EN/NL only"),
    ("api.acoustid.org", "batch", "Fingerprints, reached ONLY by beets in its own process.", "beets' own key"),
]


def build_diagram():
    d = Drawing(W, H)

    txt(d, 6, H - 15, "DLNA Gateway — Architecture (2.1 · ASGI/Hypercorn)",
        size=15, bold=True, color=INK)
    txt(d, 6, H - 28,
        "Four journeys, each read left to right · node codes (P/T/D/E/J) "
        "index into the lists overleaf",
        size=8, color=GREY)

    # ── the four lanes ───────────────────────────────────────────────
    LANE_H, GAP = 122, 10
    top = H - 42
    for n, title, question, accent, light, steps in LANES:
        y = top - LANE_H
        box(d, 6, y, W - 12, LANE_H, light, accent, rx=6, sw=1.2)
        box(d, 6, y + LANE_H - 17, W - 12, 17, accent, accent, rx=6, sw=0)
        txt(d, 13, y + LANE_H - 12.5, f"JOURNEY {n}", size=7.5,
            color=white, bold=True)
        txt(d, 72, y + LANE_H - 12.5, title, size=9, color=white, bold=True)
        txt(d, 72 + 5.2 * len(title), y + LANE_H - 12.5, f"   — {question}",
            size=7.5, color=white)

        sw_ = (W - 22) / len(steps)
        for i, (head, lines, mod) in enumerate(steps):
            x = 11 + i * sw_
            if i:
                d.add(Line(x - 3, y + 6, x - 3, y + LANE_H - 22,
                           strokeColor=accent, strokeWidth=0.5))
            txt(d, x + 3, y + LANE_H - 33, head, size=8.4, color=accent,
                bold=True)
            yy = y + LANE_H - 44
            for ln in lines:
                txt(d, x + 3, yy, ln, size=7.0, color=INK)
                yy -= 9.4
            txt(d, x + 3, y + 8, mod, size=6.6, color=GREY)
            # the flow itself: one arrow between consecutive steps
            if i < len(steps) - 1:
                arrow(d, x + sw_ - 9, y + LANE_H - 34,
                      x + sw_ - 1, y + LANE_H - 34, accent, w=1.0)
        top = y - GAP

    # ── externals ────────────────────────────────────────────────────
    ex_h = top - 6
    box(d, 6, 6, W - 12, ex_h, white, GREY, rx=6, sw=1.0)
    box(d, 6, ex_h - 11, W - 12, 17, GREY, GREY, rx=6, sw=0)
    txt(d, 13, ex_h - 6, "EXTERNAL SERVICES", size=7.5, color=white,
        bold=True)
    txt(d, 118, ex_h - 6,
        "LIVE = can affect playback · BATCH = only while a tool you "
        "started is running", size=7.5, color=white)

    cols, colw = 3, (W - 26) / 3
    rows = (len(EXTERNALS) + cols - 1) // cols
    rh = (ex_h - 26) / rows
    for i, (host, when, what, cost) in enumerate(EXTERNALS):
        cx = 13 + (i % cols) * colw
        cy = ex_h - 24 - (i // cols) * rh
        acc = GREEN if when == "live" else AMBER
        d.add(Rect(cx, cy - rh + 5, 3.2, rh - 8, fillColor=acc,
                   strokeColor=acc))
        txt(d, cx + 9, cy - 10, host, size=7.6, color=INK, bold=True)
        txt(d, cx + 9, cy - 19, f"[{when.upper()}]",
            size=6.4, color=acc, bold=True)
        txt(d, cx + 44, cy - 19, what, size=6.5, color=INK)
        txt(d, cx + 9, cy - 29, cost, size=6.3, color=GREY)

    return d


# ── the rings: what feeds what ───────────────────────────────────────
# A second, deliberately different view. The journeys page shows
# SEQUENCE — the order things happen in. This one shows STRUCTURE: what
# the app is made of, and which outside thing acts on which layer.
#
# Three concentric layers, because that is genuinely the dependency
# order and not a decorative choice:
#
#   CONTENT      what you own. Files, and the index over them. Every
#                other layer is worthless without it, and it is the only
#                layer that can be rebuilt from the disk alone.
#   ENRICHMENT   what we have LEARNED about that content — art, lyrics,
#                credits, artist facts, book series, video people. None
#                of it is regenerable from the files, which is why every
#                table here survives clear(udn).
#   SURFACES     the four ways it is consumed. They are peers: same
#                index, four protocols, no privileged client.
#
# Arrows point INWARD, at the layer each service actually touches. The
# colour is the target layer, so you can see at a glance that the great
# majority of the outside world feeds enrichment, not content.

RING_ACTORS = [
    # (label, detail, angle°, target ring 1|2|3)
    # ── content: the sources of what you own (left) ──────────────
    ("LocalFs roots",   "music · books · video",        168, 1),
    ("Immich import",   "phone clips → GWMovies",       192, 1),
    ("UPnP servers",    "MinimServer & friends",        144, 1),
    ("radio-browser",   "station catalogue",            216, 1),
    # ── enrichment: almost everything outside (top + right) ──────
    ("beets",           "canonical tags → the FILES",   120, 2),
    ("AcoustID",        "fingerprints, via beets",       98, 2),
    ("MusicBrainz",     "ids · facts · works",           76, 2),
    ("Cover Art Archive", "album covers",                54, 2),
    ("Last.fm",         "best known for",                32, 2),
    ("Wikipedia",       "biographies · CC BY-SA",        10, 2),
    ("Wikimedia",       "artist photos",                348, 2),
    ("lrclib",          "lyrics",                       326, 2),
    ("OpenLibrary",     "book author · series",         304, 2),
    ("Nominatim",       "GPS → place name",             282, 2),
    ("Immich ML",       "face recognition → people",    260, 2),
    # ── surfaces: how they are reached (bottom) ──────────────────
    ("Tailscale",       "remote, no port forwarding",   238, 3),
    ("LAN",             "SSDP · DLNA · direct bytes",  356, 3),
]

# Angles chosen to sit BETWEEN neighbouring actor arrows — the arrows
# must cross this ring to reach enrichment, so the boxes go in the gaps
# rather than in their path.
# On the DIAGONALS, and in the gaps between neighbouring actor arrows
# (actors sit every ~22°, so their midpoints are the only clear slots).
# Keeping 90° and 270° free is what lets the ring LABELS sit on the
# ring itself without a box landing on them.
SURFACES = [("PWA", "browse · play · info", 43),
            ("Naim", "DLNA MediaServer", 132),
            ("CarPlay", "Subsonic / Amperfy", 227),
            ("LG TV", "video tree", 315)]


def _pol(cx, cy, r, deg):
    a = math.radians(deg)
    return cx + r * math.cos(a), cy + r * math.sin(a)


def build_rings():
    d = Drawing(W, H)
    txt(d, 6, H - 15, "DLNA Gateway — What feeds what",
        size=15, bold=True, color=INK)
    txt(d, 6, H - 28,
        "The same system by STRUCTURE rather than sequence · arrows point "
        "at the layer each service actually touches",
        size=8, color=GREY)

    cx, cy = W / 2, H / 2 - 18
    R1, R2, R3 = 104, 176, 250

    # rings, outermost first so the inner ones paint on top
    d.add(Circle(cx, cy, R3, fillColor=BLUE_L, strokeColor=BLUE,
                 strokeWidth=1.2))
    d.add(Circle(cx, cy, R2, fillColor=AMBER_L, strokeColor=AMBER,
                 strokeWidth=1.2))
    d.add(Circle(cx, cy, R1, fillColor=GREEN_L, strokeColor=GREEN,
                 strokeWidth=1.4))

    # ── centre: CONTENT ──────────────────────────────────────────
    txt(d, cx, cy + 46, "CONTENT", size=12, color=GREEN, bold=True,
        anchor='middle')
    for i, ln in enumerate([
            "the files you own, and the",
            "index over them",
            "",
            "tracks · videos · album_key",
            "FTS5 · localfs_files",
            "",
            "the ONLY layer rebuildable",
            "from the disk alone"]):
        txt(d, cx, cy + 28 - i * 10.5, ln, size=7, color=INK, anchor='middle')

    # ── ENRICHMENT band ──────────────────────────────────────────
    txt(d, cx, cy + R1 + 46, "ENRICHMENT", size=11, color=AMBER, bold=True,
        anchor='middle')
    txt(d, cx, cy + R1 + 34, "what we have LEARNED about it", size=7,
        color=INK, anchor='middle')
    txt(d, cx, cy - R1 - 26,
        "album_art · lyrics · track_credits · artist_meta",
        size=7, color=INK, anchor='middle')
    txt(d, cx, cy - R1 - 36,
        "artist_members · book_meta · video_people · overrides",
        size=7, color=INK, anchor='middle')
    txt(d, cx, cy - R1 - 50,
        "none of it regenerable from the files —", size=6.8,
        color=AMBER, anchor='middle')
    txt(d, cx, cy - R1 - 59,
        "which is why every table here survives clear(udn)", size=6.8,
        color=AMBER, anchor='middle')

    # ── SURFACES ring ────────────────────────────────────────────
    txt(d, cx, cy + (R2 + R3) / 2 + 4, "SURFACES", size=11, color=BLUE,
        bold=True, anchor='middle')
    txt(d, cx, cy + (R2 + R3) / 2 - 8, "one index · four protocols",
        size=7, color=INK, anchor='middle')
    for name, detail, deg in SURFACES:
        sx, sy = _pol(cx, cy, (R2 + R3) / 2, deg)
        box(d, sx - 46, sy - 15, 92, 30, white, BLUE, rx=4, sw=1.0)
        txt(d, sx, sy + 3, name, size=8.5, color=BLUE, bold=True,
            anchor='middle')
        txt(d, sx, sy - 9, detail, size=6.3, color=INK, anchor='middle')

    # ── the outside world ────────────────────────────────────────
    TARGET = {1: (R1, GREEN), 2: (R2, AMBER), 3: (R3, BLUE)}
    for label, detail, deg, ring in RING_ACTORS:
        rr, colr = TARGET[ring]
        bw, bh = 132, 30
        bx, by = _pol(cx, cy, R3 + 78, deg)
        box(d, bx - bw / 2, by - bh / 2, bw, bh, white, colr, rx=4, sw=1.0)
        txt(d, bx, by + 3, label, size=8, color=colr, bold=True,
            anchor='middle')
        txt(d, bx, by - 8, detail, size=6.3, color=INK, anchor='middle')
        # Arrow from the box edge inward. The box is axis-aligned, so
        # the clearance depends on the angle: a box at 180° has to be
        # escaped sideways (half its WIDTH), one at 90° vertically.
        a = math.radians(deg)
        clear = max(abs(math.cos(a)) * (bw / 2), abs(math.sin(a)) * (bh / 2)) + 6
        x1, y1 = _pol(cx, cy, R3 + 78 - clear, deg)
        x2, y2 = _pol(cx, cy, rr + 3, deg)
        arrow(d, x1, y1, x2, y2, colr, w=1.0)

    # ── legend ───────────────────────────────────────────────────
    lx, ly = 12, 74
    box(d, lx, ly - 54, 236, 66, white, GREY, rx=5, sw=0.9)
    txt(d, lx + 9, ly, "ARROW COLOUR = THE LAYER IT ACTS ON", size=7,
        color=INK, bold=True)
    for i, (c, t) in enumerate([
            (GREEN, "content — what you own"),
            (AMBER, "enrichment — what we learned about it"),
            (BLUE, "surfaces — how it is reached")]):
        arrow(d, lx + 12, ly - 14 - i * 13, lx + 36, ly - 14 - i * 13, c, w=1.2)
        txt(d, lx + 43, ly - 17 - i * 13, t, size=6.8, color=INK)

    txt(d, W - 12, 20,
        "Most of the outside world feeds ENRICHMENT, not content: the "
        "library is yours, the facts about it are borrowed.",
        size=7.5, color=GREY, anchor='end')
    return d


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
         "AlbumArtFetcher \u2014 MusicBrainz release-group THEN release + "
         "Cover Art Archive lookup; sticky notfound cache; ~1 req/s. "
         "Art attaches to a RELEASE, so a release-group reports none "
         "unless a pressing is flagged as its cover \u2014 which is why "
         "compilations need the fallback. bare_albums() takes three "
         "opt-in filters (skip audiobooks, skip 1-2 track strays, report "
         "the performer count) because a rate-limited budget spent on "
         "impossible questions is the whole cost."),
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
         "cached sticky as ''. PRIVACY: this sends your clips' GPS "
         "coordinates off-machine automatically; opt out by not setting "
         "LOCALFS_VIDEO_ROOT."),
        ("P/g34", "dlna_ssrf.py",
         "SECURITY (2.1) — the outbound-fetch guard on every "
         "caller-supplied ?url= (/art, /stream, /radio_stream) and every "
         "redirect hop. Refuses private / loopback / link-local "
         "destinations unless the host is a device already in "
         "SERVERS/RENDERERS (so the LocalFs file server on :8200 stays "
         "reachable) and refuses non-http(s) schemes. Before it, /stream "
         "relayed ANY internal HTTP service's body verbatim and /art's "
         "error text was an open/closed/filtered port oracle. Refusals "
         "are logged; the caller gets a uniform failure."),
        ("P/g35", "dlna_credits.py",
         "Songwriting credits (2026-09-20): is this composer/lyricist a "
         "NAME or machine junk? DISPLAY-only — the file tag and the "
         "tracks row are untouched. 8% of this library's credits are "
         "scene adverts (www.t.me/…). REJECTED rule, kept as a test: "
         "\u201ccredits with no A-Za-z letter are junk\u201d matched 55 rows, "
         "every one a real composer (\u0421\u0442\u0440\u0430\u0432\u0438\u043d\u0441\u043a\u0438\u0439, "
         "\u041f\u0440\u043e\u043a\u043e\u0444\u044c\u0435\u0432)."),
        ("P/g36", "dlna_mbid.py",
         "The artist \u2192 MusicBrainz-id decision, pure. Every external "
         "source keys off this id, so one wrong match is wrong in four "
         "places and never invites correction \u2014 it REFUSES rather than "
         "guesses. &amp; is never split (band names contain it), commas "
         "never at all, every dash folds to a space (MB spells names "
         "with typographic dashes), aliases only when nothing matched "
         "by name, and two artists sharing a name are refused."),
        ("P/g37", "dlna_lineup.py",
         "\u201cWho was in the band when this was recorded?\u201d \u2014 pure "
         "date-intersection over membership. Exists because "
         "per-recording performer credits cover only ~17% of tracks "
         "while membership is near-universal. Members REJOIN (Richard "
         "Wright has two stints), and no year means no claim."),
        ("P/g38", "dlna_artist_fetch.py",
         "The artist-metadata sources \u2014 MusicBrainz (identity, "
         "life-span, genres, members), Wikipedia (bio, stored only WITH "
         "its url: CC BY-SA attribution), Last.fm (best-known, needs an "
         "application key). Parsing is split from HTTP so the judgement "
         "is testable. Owns the `member of band` DIRECTION rule: "
         "backward = a group's members, forward = a person's bands."),
        ("P/g39", "dlna_asgi_artist.py",
         "GET /api/artist_info \u2014 the artist panel's ONE request. Resolves "
         "the line-up for the track's year and LABELS it (inferred vs "
         "credits) so the client cannot promote a guess to a fact. Own "
         "module: dlna_asgi_browse is at 397/400 lines."),
        ("P/g41", "dlna_art_query.py",
         "The PURE half of a cover lookup: one (artist, album) \u2192 an "
         "ORDERED list of attempts. The exact pair is ALWAYS first so "
         "nothing that resolves today can regress; looser forms follow "
         "in increasing order of risk, and title-only is offered only "
         "for multi-artist folders where the artist is guaranteed to "
         "fail. tidy_album is LOOKUP ONLY \u2014 a loose query wastes one "
         "request, a loose identity merges two albums."),
        ("P/g42", "dlna_providers/localfs_art.py",
         "What an album LOOKS like: the embedded picture, and the cover "
         "file sitting BESIDE the music when there is none. Choosing "
         "which image is the whole problem \u2014 \u201cbiggest wins\u201d picks "
         "back.jpg, \u201cfirst wins\u201d picks a 75px WMP thumbnail, so "
         "selection is name-first with a size floor only as a last "
         "resort, and rejections match whole WORDS (\u201ccd\u201d would eat "
         "\u201cACDC - cover.jpg\u201d). A disc subfolder also looks in its "
         "parent; an ordinary folder never climbs."),
        ("P/g43", "dlna_library_credits.py",
         "CreditsMixin \u2014 track_credits, composer/lyricist browsed off "
         "MusicBrainz works for the ~68% of tracks whose files carry "
         "none. Not tracks.composer, because clear(udn) would throw away "
         "a night's fetching. THE FILE TAG WINS on read."),
        ("P/g44", "dlna_library_search.py",
         "SearchMixin \u2014 the FTS5 free-text question, split from browse "
         "at exactly 400 lines. Navigating a hierarchy and answering a "
         "question are different jobs with different semantics."),
        ("P/g40", "dlna_library_artists.py",
         "ArtistsMixin \u2014 artist_meta (id + display facts) and "
         "artist_members (line-up). Survives clear(udn): each row cost "
         "a rate-limited round-trip. Keyed by the NORMALISED name so "
         "spelling variants are one question, not three."),
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
        ("T/a15", "artist_mbid.py",
         "Resolve every artist to a MusicBrainz id \u2014 the keystone the "
         "rest of the artist metadata hangs off. Tags first (free, "
         "exact), then name search. Live: 2,889 artists, 87.8% "
         "resolved, 85.9% search acceptance. Resumable (sticky "
         "notfound). <b>--apply --limit N --tags-only -v</b>."),
        ("T/a16", "artist_meta.py",
         "Turn each id into the panel's facts: life-span, birthplace, "
         "genres, biography, best-known, and a band's members with "
         "instruments + dates. Live: 2,889 in 3h37m, 0 errors. SET "
         "LASTFM_API_KEY FIRST \u2014 meta_fetched_at marks an artist done, "
         "so a keyless pass leaves them all without best-known. "
         "<b>--apply --limit N -v</b>."),
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
        ("E/x7", "en.wikipedia.org", "GET /api/rest_v1/page/summary/"
         "{title} \u2014 artist biography for the artist panel. CC BY-SA: the "
         "bio is DROPPED if its url is missing, because the link is the "
         "attribution. Disambiguation stubs refused.", RED),
        ("E/x8", "ws.audioscrobbler.com", "Last.fm artist.getTopTracks \u2014 "
         "\u201cbest known for\u201d, ranked by real listening. Needs an "
         "APPLICATION key (LASTFM_API_KEY); an account password is NOT "
         "an API credential. Absent \u2192 the block is omitted.", RED),
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
                   "TLS/h2 natively.", note))

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
    # Page 1 = the journeys (sequence). Page 2 = the rings
    # (structure). Deliberately BOTH: one answers 'what happens
    # when', the other 'what feeds what', and neither substitutes
    # for the other. Reference tables follow.
    story = ([build_diagram(), PageBreak(),
              build_rings(), PageBreak()] + list_pages())
    doc.build(story)
    print("wrote", out)


if __name__ == "__main__":
    main()
