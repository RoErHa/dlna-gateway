#!/usr/bin/env python3
"""
dlna_artist_fetch.py — the four artist-metadata sources, and what their
answers mean.

Split in half on purpose: the `parse_*` functions are PURE over an
already-decoded document (tested directly, no network), while the
`fetch_*` functions do the HTTP and nothing else. The parsing is where
the judgement lives, so that is the half with tests.

Sources, cheapest first:
  MusicBrainz  identity, life-span, birthplace, genres, band members.
               1 req/s, needs an identifying UA (ToS).
  Wikipedia    the biography paragraph. CC BY-SA — see below.
  Last.fm      "best known for", by actual listening. Needs a free
               APPLICATION key (LASTFM_API_KEY); a user password is NOT
               an API credential. Absent -> the block is simply omitted.

Everything here is DISPLAY-layer. Nothing is written back to a file,
and nothing blocks playback: the panel reads `artist_meta`, which a
background sweep fills.

Two rules below are licence- or correctness-critical and are each a
test: `member of band` runs in BOTH directions, and a Wikipedia bio
without its URL may not be stored.
"""
from __future__ import annotations

import json
import logging
import re
import os
import urllib.parse
import urllib.request

log = logging.getLogger("dlna.artist.fetch")

MB_ROOT = "https://musicbrainz.org/ws/2"
WP_ROOT = "https://en.wikipedia.org/api/rest_v1/page/summary"
LFM_ROOT = "https://ws.audioscrobbler.com/2.0/"
TIMEOUT = 20.0
_MAX_GENRES = 6


def user_agent() -> str:
    email = os.environ.get("GATEWAY_CONTACT_EMAIL", "").strip()
    return f"DLNAGateway/1.0 ( {email} )" if email else "DLNAGateway/1.0"


# ── parsing (pure) ───────────────────────────────────────────────

def parse_mb_artist(doc) -> dict:
    """MusicBrainz artist document -> the columns `artist_meta` holds.

    `life-span` is reused for a band's inception/dissolution, so the
    same two columns serve Born/Died and Formed/Ended. Which pair of
    LABELS to print is the UI's decision, taken from `mb_type` — a band
    has no date of birth. Keeping one pair of columns avoids a second
    pair that is NULL on half the rows."""
    doc = doc or {}
    span = doc.get("life-span") or {}
    genres = sorted((doc.get("genres") or []),
                    key=lambda g: (-(g.get("count") or 0), g.get("name") or ""))
    return {
        "mb_type": doc.get("type") or "",
        "gender": doc.get("gender") or "",
        "born": span.get("begin") or "",
        "died": span.get("end") or "",
        "birth_place": (doc.get("begin-area") or {}).get("name") or "",
        "country": doc.get("country") or "",
        "disambiguation": doc.get("disambiguation") or "",
        "genres": ", ".join(g["name"] for g in genres[:_MAX_GENRES]
                            if g.get("name")),
    }


def parse_mb_members(doc, bands: bool = False) -> list[dict]:
    """The band's line-up — or, with `bands=True`, the bands a person
    played in.

    ⚠ `member of band` is ONE relation type pointing BOTH ways.
    On a Group, `direction='backward'` names the members; on a Person,
    `direction='forward'` names the bands they joined. Reading it
    without checking direction puts Tin Machine and The Konrads in
    David Bowie's line-up — which is what happened the first time this
    data was fetched for real."""
    want = "forward" if bands else "backward"
    out = []
    for rel in ((doc or {}).get("relations") or []):
        if rel.get("type") != "member of band":
            continue
        if rel.get("direction") != want:
            continue
        artist = rel.get("artist") or {}
        name = (artist.get("name") or "").strip()
        if not name:
            continue
        out.append({
            "name": name,
            "mbid": artist.get("id") or "",
            "instruments": ", ".join(rel.get("attributes") or []),
            "begin": rel.get("begin") or "",
            "end": rel.get("end") or "",
        })
    return out


def parse_wikipedia(doc) -> dict:
    """Wikipedia REST summary -> bio, its URL, and a thumbnail.

    **A bio without its URL is dropped.** The text is CC BY-SA and the
    link is the attribution, so they travel together or not at all —
    storing the paragraph alone would put us in breach the moment it is
    displayed.

    A disambiguation stub ('Nirvana may refer to:') is refused for a
    different reason: it is a wrong answer dressed as a right one."""
    doc = doc or {}
    url = ((doc.get("content_urls") or {}).get("desktop") or {}).get("page", "")
    extract = (doc.get("extract") or "").strip()
    if doc.get("type") == "disambiguation":
        extract = ""
    if not url:
        extract = ""
    return {
        "bio": extract,
        "bio_url": url if extract else "",
        "image_url": (doc.get("thumbnail") or {}).get("source", ""),
    }


# An edition suffix Last.fm carries in the track NAME: "Army Dreamers -
# 2018 Remaster", "Babooshka (2018 Remastered Version)". The panel says
# "Best known for", which is about the song, not the master you heard.
# "Live" is deliberately NOT matched — 'Live and Let Die' and 'Live
# Forever' are song titles, and a live recording is a fair entry.
_EDITION = re.compile(
    r"\s*[-–—(\[]\s*(?:\d{4}\s+)?remaster(?:ed)?"
    r"(?:\s+version)?(?:\s+\d{4})?\s*[)\]]?\s*$", re.I)


def _song_title(name: str) -> str:
    return _EDITION.sub("", (name or "").strip()).strip()


def parse_lastfm_top(doc, limit: int = 5) -> list[str]:
    """Last.fm artist.getTopTracks -> distinct song titles, best first.

    Live and remastered versions are separate entries there, so the
    same title can repeat; the panel wants distinct songs. Last.fm also
    collapses a one-element array into a bare object, and answers
    errors with a JSON body rather than a status code."""
    doc = doc or {}
    tracks = ((doc.get("toptracks") or {}).get("track")) or []
    if isinstance(tracks, dict):
        tracks = [tracks]
    out: list[str] = []
    for t in tracks:
        name = _song_title(t.get("name"))
        if name and name not in out:
            out.append(name)
        if len(out) >= limit:
            break
    return out


# ── fetching (network) ───────────────────────────────────────────

def _get(url: str, headers: dict | None = None):
    req = urllib.request.Request(url, headers=headers or
                                 {"User-Agent": user_agent()})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return json.load(r)


def fetch_mb_artist(mbid: str):
    """One request carries identity, genres, members and the Wikidata
    link — `inc` is why this is not four round-trips."""
    return _get(f"{MB_ROOT}/artist/{urllib.parse.quote(mbid)}"
                f"?inc=artist-rels+genres+url-rels&fmt=json")


def fetch_wikipedia(title: str):
    return _get(f"{WP_ROOT}/{urllib.parse.quote(title.replace(' ', '_'))}")


def fetch_lastfm_top(artist: str, api_key: str):
    """Absent key -> None, and the caller omits the block. Last.fm
    reports errors in a 200 body, which parse_lastfm_top tolerates."""
    if not api_key:
        return None
    q = urllib.parse.urlencode({
        "method": "artist.getTopTracks", "artist": artist,
        "api_key": api_key, "format": "json", "limit": 10, "autocorrect": 1})
    return _get(f"{LFM_ROOT}?{q}")
