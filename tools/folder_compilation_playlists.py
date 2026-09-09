#!/usr/bin/env python3
"""Create playlists for compilation FOLDERS.

The sibling tool `compilation_playlists.py` handles the *scattered* case:
one album TAG spread across many folders. This one is the opposite, and
the folders it finds look like

    Various Artists - 70s HITS 100 Greatest Songs of the 1970s (2023) …
    VA - 100 Greatest Jazz Icons (2020) …
    Blue Note Trip - Somethin' Old

— a single directory holding one track each by dozens of different
performers. Folder-album grouping already renders these as one
Various-Artists album, so they ARE browsable; what they are not is
*named*. They sort under their leading digit among 200-odd other
digit-albums, they never appear on any performer's artist page (each
track carries its own artist, so nothing files them under "Various
Artists"), and on the Naim they are reachable only by walking the album
letter index. A playlist gives each one a name in the one list that is
surfaced first on every surface — the PWA playlist panel, the Naim's
"Playlists" container, and Amperfy/CarPlay.

WHAT COUNTS AS A COMPILATION FOLDER
-----------------------------------
The risk runs one way. A missed compilation stays exactly as browsable
as it is today; a false positive puts a junk playlist in a list the
owner curates by hand and has to delete. So evidence is required, and
"lots of artists" alone is never enough — measured on the reference
library, `Unknown Artist/Unknown Album` holds 126 performers and is a
junk drawer, while `Santana - Supernatural` holds 9 because the record
is full of guests.

A folder qualifies when it makes an album claim (see below) and then
either:

  * **it says so** — a `Various Artists` / `VA` / `V.A.` head, or a
    collection phrase ("greatest", "the best", "hits", "top 40",
    "collection", "anthology", "essentials", the Dutch "beste"), in the
    folder path OR in the album tag; with >= --min-artists performers
    and a performer-per-track ratio >= --named-density; or

  * **it is shaped like one** — no such phrase anywhere, but the tracks
    are near enough to one-per-performer to leave little else it could
    be (>= --struct-artists performers at >= --struct-density, or a
    perfect >= --tight-density spread across >= --tight-artists).

Two of those details are load-bearing and were each learned from the
data rather than guessed:

  * **The album tag is searched, not just the folder name.** Three
    folders here are called `Wembley`, `Grrl` and `Speed Ticket` and
    carry the tag `Best Of JMFH's Choice 2004-2007 - <name>`. The tin
    is blank; the tracks are not.

  * **A folder whose tracks declare NO album at all never qualifies**,
    whatever its performer count. That is the junk-drawer rule
    `_localfs_album_group` already enforces for browsing, and it is the
    single check that keeps the 126-artist drawer out. A real
    compilation names itself.

The structural tier is deliberately the narrower one: it is what catches
`Blue Note Trip - Sunset`, `De Pre Historie 1964` and `Motown
Chartbusters 3`, which announce nothing but are one-artist-per-track
throughout.

NAMING
------
The playlist is named after the album tag when the folder's tracks agree
on one, else after the folder — skipping generic disc wrappers, because
a 40-CD box here nests every disc inside a directory literally called
`CD` and tags all 40 with the same string. Names that would collide fall
back to the distinguishing folder segment, so that box becomes forty
playlists called `Greatest Hits Collection - 50's Cd1 …Cd40` rather than
forty called `CD`.

Tracks are added in FILE-PATH order, which is the running order the
compiler chose (and keeps `CD 1/` before `CD 2/`), not artist→title:
on a compilation the sequence is the curation.

SAFETY
------
Nothing here writes to `tracks`, touches a file, or edits an existing
playlist. A candidate whose name already matches a playlist
(case-insensitively) is skipped, so re-running after new rips only adds
what is new. DRY-RUN by default; `--apply` mutates.

    python3 tools/folder_compilation_playlists.py            # preview
    python3 tools/folder_compilation_playlists.py -v         # + rejects
    python3 tools/folder_compilation_playlists.py --apply
"""
import argparse
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT = os.path.dirname(HERE)
sys.path.insert(0, PROJECT)

# ---------------------------------------------------------------- defaults
MIN_TRACKS      = 6      # below this a "compilation" is an EP or a stray
MIN_ARTISTS     = 4      # named tier: enough performers to not be a band
NAMED_DENSITY   = 0.45   # named tier: performers per track
STRUCT_ARTISTS  = 8      # structural tier, no lexical evidence at all
STRUCT_DENSITY  = 0.65
TIGHT_ARTISTS   = 5      # structural tier for a perfect one-each spread
TIGHT_DENSITY   = 0.95

# A head that declares the folder holds many performers. Kept in step
# with dlna_artist_infer._NOT_A_PERFORMER, which encodes the same idea
# for the artist-tag question; the punctuated spellings ("V.A.") are
# here because folder names use them and tags do not.
_VA_ABBREVIATIONS = {"va", "v.a", "v/a", "vv.aa"}
_VA_HEADS = _VA_ABBREVIATIONS | {
    "various artists", "various", "v a",
    "diverse", "verschillende artiesten", "artistes divers", "sundry",
}

# Phrases that name a COLLECTION. Matched on the folder path AND the
# album tag. Dutch entries are here because this library is half Dutch
# ("Het Beste Uit De Mega Top 50", "28 Vlaamse voltreffers").
_COLLECTION_RE = re.compile(
    r"\bgreatest\b|\bbest\s+of\b|\bthe\s+best\b|\bbest\s+hits\b|\bhits\b"
    r"|\btop\s*\d+\b|\bcompilation\b|\bcollection\b|\banthology\b"
    r"|\bessentials?\b|\bsampler\b|\bmixtape\b|\bultimate\b|\bchart\w*\b"
    r"|\bnow\s+that'?s\b|\bmegamix\b|\bclassics\b|\bsongs\s+of\b"
    r"|\bbeste\b|\bgrootste\b|\bvoltreffers\b",
    re.I)

# Folder segments that carry no identity — a disc wrapper. A 40-CD box
# in the reference library nests every disc in a directory called `CD`,
# so naming by the leaf would produce forty playlists called "CD".
_DISC_SEGMENT_RE = re.compile(
    r"^(cd|disc|disk|dis|schijf)[\s._-]*\d*$|^\d{1,2}$", re.I)


def _segments(album_key):
    """Path segments of a folder identity, '' entries dropped."""
    return [s for s in (album_key or "").replace("\\", "/").split("/") if s]


def declares_various_artists(album_key, album_tags=()):
    """Does the folder or its tags say outright that it holds many
    performers? The head is what is read — the part before the first
    ' - ' or bracket — so `VA - 2016 - 100 Hits` counts and a band
    called `Various Comforts` does not."""
    heads = []
    for seg in _segments(album_key):
        heads.append(re.split(r" - |\(|\[|\{", seg)[0])
        # Scene folders often skip the separator entirely: `VA The Very
        # Best Of Smooth Jazz`. Only the ABBREVIATIONS are read as a
        # leading token — "Various" is an ordinary English word and
        # would claim a band called `Various Comforts`, which is
        # exactly the false positive the whole-head match avoids.
        first = seg.split()[0] if seg.split() else ""
        if first.strip(" -_.,").casefold() in _VA_ABBREVIATIONS:
            return True
    heads.extend(album_tags or ())
    for h in heads:
        if re.sub(r"\s+", " ", h or "").strip(" -_.,").casefold() in _VA_HEADS:
            return True
    return False


def names_a_collection(album_key, album_tags=()):
    """Is there a collection phrase anywhere in the folder path or the
    album tags? Searching the TAGS as well is what catches the folders
    called `Wembley` and `Grrl` whose tag is `Best Of JMFH's Choice …`."""
    hay = " / ".join(_segments(album_key)) + " || " + " | ".join(
        t for t in (album_tags or ()) if t)
    return bool(_COLLECTION_RE.search(hay))


# Release-group / format cruft that belongs to the scene, not the record:
# `[24Bit-FLAC]`, `(FLAC Songs)`, `Mp3 320kbps`, `[PMEDIA]`, `[h33t]`. A
# bare year is deliberately NOT in here — `(2023)` tells the owner which
# edition this is, and a year is one of the things that marks a
# compilation in the first place.
_CRUFT_INNER_RE = re.compile(
    r"^\s*(?:\d{1,2}\s*bit|24\s*bit|16\s*bit|flac|mp3|wav|aac|ape|alac"
    r"|lossless|web|vinyl|rip|eac|cue|dr\s*\d+|log|scans?"
    r"|\d{2,3}\s*kbps|\d{1,2}[-\s]?\d{2,3}(?:\.\d)?\s*khz?"
    r"|pmedia|h33t|nlt[-\s]?release|vtwin\w*|kitlope|oan\b|mjn\b"
    r"|[\w.-]*\b(?:flac|mp3|bit|kbps|khz)\b[\w\s.-]*)\s*$", re.I)
_CRUFT_TOKEN_RE = re.compile(
    r"\b(?:mp3|flac|wav)\s*\d{2,3}\s*kbps\b|\b\d{2,3}\s*kbps\b"
    r"|\b24\s*bit\b|\b16\s*bit\b|\bflac\b|\bmp3\b", re.I)
_LEADING_VA_RE = re.compile(
    r"^\s*(?:various\s+artists|various|v\s*\.?\s*a\.?|v/a|vv\.aa|diverse)"
    r"\s*(?:[-–—:]\s*|\s+)", re.I)


def tidy_name(name):
    """Make a FOLDER name fit to read on a remote.

    Folder names in this library carry the release group's markings —
    `VA - Hi-Res Masters 50 Britpop Tracks To Test Your Speakers
    [24Bit-FLAC] [PMEDIA]-` is one real example, and it has to fit on a
    Naim's screen and in a CarPlay list. Only bracketed groups whose
    CONTENT is format/scene noise are dropped, so `(2023)` and
    `(Disc 1)` survive: a year is information, and the disc number is
    the only thing telling two discs apart.

    Applied to folder-derived names only. An album tag the tracks agree
    on is already a title someone chose."""
    out = re.sub(r"[\[(\{]([^\[\](){}]*)[\])}]",
                 lambda m: "" if _CRUFT_INNER_RE.match(m.group(1)) else m.group(0),
                 name or "")
    out = _CRUFT_TOKEN_RE.sub("", out)
    out = _LEADING_VA_RE.sub("", out)
    # A dangling opener is left behind by a truncated directory name
    # ("Het Beste Uit De Mega Top 50 Van '93 (Di").
    out = re.sub(r"[\[(\{][^\[\](){}]*$", "", out)
    out = re.sub(r"[\s\u2b50\ufe0f\u2014\u2013_-]+$", "", out)
    out = re.sub(r"\s{2,}", " ", out).strip(" -_.,")
    return out or (name or "").strip()


def meaningful_segment(album_key):
    """The deepest folder segment that identifies anything — skipping
    disc wrappers, so `…/Greatest Hits Collection - 50's Cd1/CD` reads
    as `Greatest Hits Collection - 50's Cd1`."""
    segs = _segments(album_key)
    for seg in reversed(segs):
        if not _DISC_SEGMENT_RE.match(seg.strip()):
            return seg.strip()
    return segs[-1].strip() if segs else ""


def classify(album_key, album_tags, n_tracks, n_artists, *,
             min_tracks=MIN_TRACKS, min_artists=MIN_ARTISTS,
             named_density=NAMED_DENSITY,
             struct_artists=STRUCT_ARTISTS, struct_density=STRUCT_DENSITY,
             tight_artists=TIGHT_ARTISTS, tight_density=TIGHT_DENSITY):
    """(tier, reason) for one folder — tier is 'named', 'structural' or
    '' when the folder is not a compilation.

    `album_tags` is every distinct NON-BLANK album tag in the folder.
    Pure: this is the whole decision, and it is the only thing that can
    put a wrong playlist in front of the owner."""
    tags = [t for t in (album_tags or ()) if (t or "").strip()]
    if n_tracks < min_tracks:
        return "", f"only {n_tracks} tracks"
    if n_artists < 3:
        return "", f"only {n_artists} performer(s)"
    if not tags:
        # The junk-drawer rule. A folder where nothing declares an album
        # has claimed nothing; the 126-artist `Unknown Artist/Unknown
        # Album` drawer is the case this exists for.
        return "", "no album tag — unclaimed folder"

    density = n_artists / n_tracks
    evidence = []
    if declares_various_artists(album_key, tags):
        evidence.append("says Various Artists")
    if names_a_collection(album_key, tags):
        evidence.append("names a collection")

    if evidence and n_artists >= min_artists and density >= named_density:
        return "named", "; ".join(evidence)
    if n_artists >= struct_artists and density >= struct_density:
        return "structural", (f"{n_artists} performers over {n_tracks} "
                              f"tracks ({density:.0%})")
    if n_artists >= tight_artists and density >= tight_density:
        return "structural", (f"one track each for {n_artists} of "
                              f"{n_tracks}")
    why = "; ".join(evidence) if evidence else "no collection phrase"
    return "", f"{why} — {n_artists}/{n_tracks} performers ({density:.0%})"


def playlist_name(album_key, album_tags):
    """The name this folder wants. The album tag when its tracks agree
    on one, else the folder. Collisions are resolved by the caller."""
    tags = {t.strip() for t in (album_tags or ()) if (t or "").strip()}
    if len(tags) == 1:
        return next(iter(tags))
    return tidy_name(meaningful_segment(album_key))


def resolve_names(candidates):
    """Give every candidate a UNIQUE name, in place.

    A shared album tag is common in box sets — forty discs here carry
    the identical tag — so a colliding candidate falls back to its own
    folder segment, and only then to a numeric suffix. Order-stable so
    a re-run names things the same way."""
    for c in candidates:
        c["name"] = playlist_name(c["album_key"], c["album_tags"])
    counts = {}
    for c in candidates:
        counts[c["name"].casefold()] = counts.get(c["name"].casefold(), 0) + 1
    for c in candidates:
        if counts[c["name"].casefold()] > 1:
            seg = tidy_name(meaningful_segment(c["album_key"]))
            if seg:
                c["name"] = seg
    seen = {}
    for c in candidates:
        key = c["name"].casefold()
        if key in seen:
            seen[key] += 1
            c["name"] = f"{c['name']} ({seen[key]})"
        else:
            seen[key] = 1
    return candidates


# ------------------------------------------------------------------- data
FOLDER_SQL = """
SELECT album_key,
       COUNT(*)               AS n,
       COUNT(DISTINCT artist) AS artists
  FROM tracks
 WHERE udn = ? AND album_key != ''
 GROUP BY album_key
"""


def scan_folders(db, udn):
    """Every folder with its stats and its distinct non-blank album
    tags. Pure read."""
    with db._pool.read() as conn:
        rows = conn.execute(FOLDER_SQL, (udn,)).fetchall()
        out = []
        for r in rows:
            tags = [t[0] for t in conn.execute(
                "SELECT DISTINCT album FROM tracks "
                "WHERE udn=? AND album_key=? AND album IS NOT NULL "
                "AND album != ''", (udn, r["album_key"])).fetchall()]
            out.append({"album_key": r["album_key"], "n": r["n"],
                        "artists": r["artists"], "album_tags": tags})
    return out


def find_candidates(db, udn, **kw):
    """(candidates, rejects) — candidates carry a unique `name`."""
    cands, rejects = [], []
    for f in scan_folders(db, udn):
        tier, why = classify(f["album_key"], f["album_tags"],
                             f["n"], f["artists"], **kw)
        rec = dict(f, tier=tier, why=why)
        (cands if tier else rejects).append(rec)
    cands.sort(key=lambda c: (-c["artists"], c["album_key"]))
    return resolve_names(cands), rejects


def split_existing(db, candidates):
    """Partition into (new, already-a-playlist) by case-insensitive name."""
    existing = {p["name"].strip().lower() for p in db.pl_list()}
    new, skipped = [], []
    for c in candidates:
        (skipped if c["name"].strip().lower() in existing
         else new).append(c)
    return new, skipped


def folder_tracks(db, udn, album_key):
    """The folder's tracks in RUNNING order — file_path, which keeps the
    compiler's sequence and orders `CD 1/` before `CD 2/`."""
    with db._pool.read() as conn:
        rows = conn.execute(
            "SELECT url, title, artist, album, duration, art FROM tracks "
            "WHERE udn=? AND album_key=? "
            "ORDER BY file_path COLLATE NOCASE, title COLLATE NOCASE",
            (udn, album_key)).fetchall()
    return [dict(r) for r in rows]


def create_playlist(db, udn, cand):
    """Create the playlist and add every track. Returns (pl_id, added)."""
    pid = db.pl_create(cand["name"])
    added = sum(1 for t in folder_tracks(db, udn, cand["album_key"])
                if db.pl_add_track(pid, t))
    return pid, added


# ------------------------------------------------------------------- main
def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Create one playlist per compilation FOLDER "
                    "(dry-run by default).")
    ap.add_argument("--db", default=os.path.join(PROJECT, "library.db"))
    ap.add_argument("--apply", action="store_true",
                    help="actually create the playlists")
    ap.add_argument("-n", "--dry-run", action="store_true",
                    help="preview only (the default)")
    ap.add_argument("-v", "--verbose", action="store_true",
                    help="also list the folders that were rejected")
    ap.add_argument("--named-only", action="store_true",
                    help="skip the structural tier — only take folders "
                         "that name themselves a compilation")
    ap.add_argument("--min-tracks", type=int, default=MIN_TRACKS)
    ap.add_argument("--min-artists", type=int, default=MIN_ARTISTS)
    ap.add_argument("--struct-artists", type=int, default=STRUCT_ARTISTS)
    ap.add_argument("--udn", default=None,
                    help="source to scan (default: the primary music UDN)")
    args = ap.parse_args(argv)
    if args.dry_run and args.apply:
        ap.error("--apply and --dry-run are mutually exclusive")

    os.environ.setdefault("GATEWAY_NO_SERVICES", "1")
    from dlna_library import LibraryDB
    db = LibraryDB(db_file=args.db)
    udn = args.udn or db.primary_udn()
    if not udn:
        print("no tracks in the library — nothing to do")
        return 0

    cands, rejects = find_candidates(
        db, udn, min_tracks=args.min_tracks, min_artists=args.min_artists,
        struct_artists=args.struct_artists)
    if args.named_only:
        cands = [c for c in cands if c["tier"] == "named"]
    new, skipped = split_existing(db, cands)

    if args.verbose:
        for r in sorted(rejects, key=lambda r: -r["artists"])[:40]:
            if r["artists"] >= 4:
                print(f"  reject  {r['artists']:3d}a/{r['n']:4d}t  "
                      f"{r['album_key'][:58]:58}  {r['why']}")
        print()
    for c in skipped:
        print(f"skip (playlist exists): {c['name'][:70]}")
    if not new:
        print("no new compilation folders found")
        return 0

    named = sum(1 for c in new if c["tier"] == "named")
    for c in new:
        verb = "create" if args.apply else "would create"
        print(f"{verb} [{c['tier']:10}] {c['name'][:62]!r}\n"
              f"    {c['n']} tracks, {c['artists']} performers — {c['why']}")
        if args.apply:
            pid, added = create_playlist(db, udn, c)
            print(f"    → playlist {pid} with {added} tracks")

    tail = (f"{len(new)} playlist(s)  ({named} named, "
            f"{len(new) - named} structural)")
    print(f"\nDRY-RUN: would create {tail} — re-run with --apply"
          if not args.apply else f"\ncreated {tail}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
