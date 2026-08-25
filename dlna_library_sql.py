#!/usr/bin/env python3
"""
dlna_library_sql.py — pure helpers shared by the LibraryDB mixins.

Extracted from dlna_library.py (2026-08-20) when that 2,912-line module
was split into mixins. These live in their own dependency-free module so
the mixins can import them WITHOUT importing dlna_library — which would
be circular, and which would also trigger the `DB = LibraryDB()`
module-level singleton (and therefore every pending migration against
the live library.db) as a side effect of importing a helper.

Everything here is a pure function of its arguments: string
normalisation, URL parsing, and SQL-fragment builders. No I/O, no DB
handle, no global state beyond compiled regexes.

`dlna_library` re-exports all of these, so the historical
`from dlna_library import _dedup_clause, _parse_audio_params` import
form still works unchanged.
"""
import re

FAVOURITES_ID = "__favourites__"


# AssetUPnP encodes the source file's bit depth and sample rate in
# the URL path (e.g. `/c2/b16/f44100/...` or `/c2/b24/f96000/...`).
# We parse these out at index time to populate tracks.bit_depth and
# tracks.sample_rate, which participate in the UNIQUE constraint so
# 16-bit and 24-bit copies of the same (artist, album, title)
# coexist as distinct rows. Browse-side queries then prefer the
# higher-quality version. Other UPnP MediaServers usually don't
# embed these in the URL; for them we leave both columns NULL.
_AUDIO_PARAMS_RE = re.compile(r"/b(\d+)/f(\d+)/")
_D_ID_RE         = re.compile(r"/(d-?\d+)-co")


def _parse_audio_params(url: str):
    """Return (bit_depth, sample_rate) parsed from an AssetUPnP-style
    URL, or (None, None) if the pattern doesn't match. Both values
    are integers when present (bit_depth in bits, sample_rate in Hz)."""
    if not url:
        return None, None
    m = _AUDIO_PARAMS_RE.search(url)
    if not m:
        return None, None
    try:
        return int(m.group(1)), int(m.group(2))
    except (ValueError, TypeError):
        return None, None


def _d_id(url: str):
    """Extract the d-id portion of an AssetUPnP URL, or None for
    non-AssetUPnP URLs. Used as one half of the (d_id, lower(title))
    dedup key in upsert_tracks — see the 'AssetUPnP virtual albums'
    note in the docstring for upsert_tracks for the why."""
    if not url:
        return None
    m = _D_ID_RE.search(url)
    return m.group(1) if m else None


# Lazy unicodedata import keeps module load cheap on the hot path.
_unicodedata = None

def _norm_title(s):
    """Normalise a track title for dedup keying.

    Strips combining marks, replaces curly typographic apostrophes /
    quote marks with ASCII equivalents, collapses whitespace,
    lowercases. Same song with different typographic renderings
    (e.g. "Art for Art's Sake" with ASCII apostrophe vs the same
    string with curly U+2019) maps to one key.

    Does NOT strip bracketed annotations — "Wiggle It" and "Wiggle It
    (club mix)" stay distinct because they're genuinely different
    recordings."""
    global _unicodedata
    if not s:
        return ""
    if _unicodedata is None:
        import unicodedata as _unicodedata
    s = _unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not _unicodedata.combining(c))
    # Curly apostrophes/quotes → ASCII. The bytes that bit us live in
    # b"\xe2\x80\x99" (U+2019) and friends; doing this after NFKD
    # because NFKD does NOT decompose the smart-quote characters.
    s = (s.replace("‘", "'").replace("’", "'")
          .replace("‚", "'").replace("‛", "'")
          .replace("“", '"').replace("”", '"')
          .replace("´", "'").replace("`", "'"))
    s = re.sub(r"\s+", " ", s).strip().lower()
    return s


def _dedup_clause(outer_alias: str = "t") -> str:
    """SQL fragment that filters out lower-quality duplicates from
    a tracks-table query.

    Excludes the current row when a same-(udn,artist,album,title) row
    with strictly higher (bit_depth, sample_rate) exists. NULL values
    are treated as 0 (lowest), so any non-NULL beats a NULL — giving
    AssetUPnP-served tracks a clean prefer-24-bit, prefer-higher-rate
    ordering, without affecting non-AssetUPnP servers (all NULL → all
    treated as equal → all survive).

    Use only in BROWSE views — listings the user sees in the UI.
    The AcoustID worker, playlists, and radio scans should NOT dedup
    (they need to process / play every URL the user has)."""
    a = outer_alias
    return f"""NOT EXISTS (
        SELECT 1 FROM tracks _hq
         WHERE _hq.udn    = {a}.udn
           AND _hq.artist = {a}.artist
           AND _hq.album  = {a}.album
           AND _hq.title  = {a}.title
           AND (   COALESCE(_hq.bit_depth, 0)   >  COALESCE({a}.bit_depth, 0)
                OR (    COALESCE(_hq.bit_depth, 0)   = COALESCE({a}.bit_depth, 0)
                    AND COALESCE(_hq.sample_rate, 0) > COALESCE({a}.sample_rate, 0)))
    )"""


def _is_localfs(udn: str) -> bool:
    """LocalFs sources own a `file_path` per row and a populated
    `album_key`, so their album browse groups by FOLDER. Everything else
    (UPnP / Subsonic-fed) keeps the legacy (artist, album) grouping."""
    return udn.startswith("uuid:localfs-")


VARIOUS_ARTISTS = "Various Artists"

# The hand-editing worklist for tracks the indexer could not
# attribute. Named with leading punctuation so it sorts to the top
# of the playlist list, where the work is visible.
UNKNOWN_ARTISTS_PLAYLIST = "- Unknown Artists -"


def _localfs_album_leaf(a: str = "t") -> str:
    """SQL: the folder's own leaf name — the segment of `album_key` after
    the last '/'. `rtrim(path, replace(path,'/',''))` strips back to and
    including the last slash, which `replace` then removes."""
    return (f"replace({a}.album_key, "
            f"rtrim({a}.album_key, replace({a}.album_key,'/','')), '')")


def _localfs_album_name(a: str = "t") -> str:
    """SQL aggregate expression for a folder-grouped album's DISPLAY name:
    the album tag when the folder is tag-consistent (normal albums), else
    the folder's own leaf name (Various-Artists comps where every track
    carries its original album tag).

    A BLANK album tag falls through to the leaf too. It used to satisfy
    `COUNT(DISTINCT album)=1` and win, so a folder where nothing declares
    an album name rendered as an album with no name at all."""
    return (f"CASE WHEN COUNT(DISTINCT {a}.album)=1 "
            f"          AND COALESCE(MAX({a}.album),'')<>'' "
            f"     THEN MAX({a}.album) "
            f"     ELSE {_localfs_album_leaf(a)} END")


def _localfs_album_artist(a: str = "t") -> str:
    """SQL aggregate: 'Various Artists' when a folder spans >1 performer,
    else the single performer."""
    return (f"CASE WHEN COUNT(DISTINCT {a}.artist)>1 THEN '{VARIOUS_ARTISTS}' "
            f"ELSE MAX({a}.artist) END")


def _localfs_album_group(a: str = "t") -> str:
    """GROUP BY expression for folder-albums — the FOLDER, plus the artist
    when the folder's tracks declare no album at all.

    A folder is the album identity because that is what reunites a
    Various-Artists compilation whose every track carries its own performer.
    That inference only holds while the folder is *claimed* by something: a
    real compilation still names itself in the album tag. A folder where
    every album tag is blank has made no such claim, and treating it as one
    album turns a junk drawer into a single enormous record — measured here
    at 247 tracks by 43 unrelated artists, so playing one Marsh & Quinn song
    queued Rio Verde Social Club behind it.

    So blank-album folders group by performer instead. Everything else is
    byte-identical to grouping by `album_key` alone: rows carrying a real
    album tag all collapse to the same '' branch.

    The artist branch is PREFIXED so the two branches can never collide:
    without it a blank-album/blank-artist track keys on '' — the same key
    the album branch uses — and the untagged strays merge into whatever
    properly-tagged album shares their folder, which is how the junk
    folder first rendered as one 190-track 'Various Artists' record."""
    return (f"{a}.album_key, "
            f"CASE WHEN COALESCE({a}.album,'')='' "
            f"     THEN 'a:'||COALESCE({a}.artist,'') ELSE '' END")


def _dur_to_secs(dur: str) -> int:
    """'H:MM:SS' → integer seconds, -1 if unparseable."""
    try:
        parts = [float(x) for x in dur.split(":")]
        if len(parts) == 3:
            return int(parts[0] * 3600 + parts[1] * 60 + parts[2])
        if len(parts) == 2:
            return int(parts[0] * 60 + parts[1])
    except (ValueError, TypeError, AttributeError):
        pass        # documented contract: unparseable → -1
    return -1
