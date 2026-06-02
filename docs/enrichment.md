# Metadata Enrichment Stage (beets + Picard)

Additive layer on top of the completed six-phase backend. It does **not** touch the
`LibraryProvider` protocol or the serve path. It is an *upstream batch stage* that writes
clean tags **into the files**, so the files remain the single source of truth that the
watchdog + mutagen indexer reads. No live dependency on any external server.

---

## 1. Position in the architecture

```
                         ENRICHMENT (offline / batch)          LIVE PIPELINE (unchanged)
                         ------------------------------         --------------------------
  MusicBrainz  ─┐
  AcoustID     ─┼─► beets import  ──► writes tags + MBIDs ──┐
  (Chromaprint) │   (bulk, automated)   into the files      │
                │                                            ▼
  hard albums  ─┘   Picard (manual)  ──► writes tags ──►  music folder  ──► watchdog
  (prog/segued)                          into the files     (on disk)        + mutagen
                                                                   │            indexer
                                                                   ▼              │
                                                            override table ◄──────┘
                                                            (manual, wins)
                                                                   │
                                                                   ▼
                                                            SQLite (canonical)
                                                                   │
                                                            LibraryProvider ──► serve
```

Key point: Jellyfin / Plex stay **out** of this diagram. They never become a metadata
authority. Routing the serve path at Jellyfin's DB would re-create the AssetUPnP
dual-source-of-truth problem. If you switch Plex → Jellyfin, do it purely for the
playback/performance win and keep it decoupled from tagging.

---

## 2. Resolution precedence (enforce in the indexer)

Read order at index time, last writer wins per-field:

```
1. file tags (mutagen)        # baseline, now enriched by beets/Picard
2. override table             # manual corrections, wins field-by-field
   → resolved = {**file_tags, **{k: v for k, v in override.items() if v is not None}}
```

**Improvement to the override design:** key the override row on the **recording MBID**
(`mb_trackid`, written by beets) when present, falling back to normalized path. Keying on
the MBID means overrides survive both a re-tag *and* a file move/rename — the path-keyed
version only survived re-index.

```python
key = tags.get("mb_trackid") or normalize_path(track.path)
override = overrides.get(key)
```

---

## 3. beets config (prog-tuned, tag-in-place)

`~/.config/beets/config.yaml` — verified key names against beets docs.

```yaml
directory: /Volumes/Music          # never used while copy/move are off; harmless
library:   ~/.config/beets/library.db

plugins: chroma fetchart embedart info missing duplicates

import:
  write: yes        # write tags INTO the files  (this is the whole point)
  copy: no          # leave files where they are…
  move: no          # …do not move them either  → tag-in-place
  resume: ask
  incremental: yes  # record done dirs, skip on re-run (re-runnable batch)
  timid: yes        # prompt before applying — essential for prog (see §5)
  duplicate_action: skip

# release selection — bias toward the *original* release, not a random reissue
original_date: yes
original_year: yes
per_disc_numbering: no   # continuous track numbers across discs of one release
                         # (set yes + add $disc to paths only if you let beets rename)

match:
  preferred:
    media: ['CD', 'Digital Media|File', 'Vinyl']
    # countries: ['GB|UK', 'NL', 'XE']   # uncomment if you want regional bias
  strong_rec_thresh: 0.90   # auto-accept only confident matches; rest go to review

chroma:
  auto: yes         # AcoustID fingerprint fallback when tags are missing/wrong

fetchart:
  auto: yes
embedart:
  auto: yes
```

What beets writes that matters to you:

- **MBIDs as stable join keys:** `mb_trackid` (recording), `mb_albumid` (release),
  `mb_releasegroupid`, `mb_artistid`, `mb_albumartistid`. These land in the file tags and
  are richer than what Jellyfin exposes (Jellyfin only carries a Track MBID, not the
  Recording MBID).
- **Gapless grouping prerequisites:** consistent `album` / `albumartist`, correct
  `disc` / `disctotal` / `track` / `tracktotal`. (Actual gapless *playback* is still a
  renderer/encoder concern — this just guarantees the album presents and orders as one
  unit.)

**Do NOT enable the `scrub` plugin** in this setup — it strips existing tags before
writing and can wipe metadata your override logic or hand-tagging depends on.

---

## 4. CLI commands

```bash
# one-time deps (Mac)
brew install chromaprint           # provides fpcalc for the chroma plugin
pip3 install beets pyacoustid

# preview/interactive a single album — review each match before applying
# (beets has no true dry-run for import; timid prompts you and you can quit
#  before accepting, leaving files untouched)
beet import --timid /Volumes/Music/incoming/SomeAlbum

# tag in place, interactive (recommended for the first passes)
beet import /Volumes/Music

# re-run later: incremental:yes makes this skip already-done dirs
beet import /Volumes/Music

# inspect what got written
beet list -f '$albumartist - $album - $track $title [$mb_trackid]' artist:Focus
```

Trigger a dlna-gateway re-index after a batch (your existing index entrypoint), e.g.:

```bash
curl -s http://192.168.1.125:<port>/api/reindex    # or whatever your route is
```

---

## 5. Picard for the hard releases

beets in `timid` mode will kick anything below `strong_rec_thresh` to manual review.
For segued prog with multiple reissues / different track splits, do those albums in
Picard instead — its cluster view + interactive release picking is better at choosing the
correct release among many. Picard writes tags into the same files; the indexer reads them
identically. The two are complementary: beets for the automated bulk, Picard for judgment
calls. Don't try to embed or run Picard headless — it's GUI-coupled and not a library.

---

## 6. DSD / high-res caveat

- **Fingerprinting does not work on DSD** (`.dsf` / `.dff`): Chromaprint/fpcalc has no
  decode path, so the `chroma` plugin can't identify them. beets can still tag DSD **by
  existing-tag metadata match** against MusicBrainz; if that fails, fall back to Picard /
  manual / the override table.
- High-res PCM (24/96, 24/192 FLAC/WAV) fingerprints and tags normally.

---

## 7. Gotchas

- `copy:no move:no` is what keeps files in place — verify both before the first real run.
  `move:yes` is destructive-ish (it relocates files); avoid it here.
- `incremental:yes` skips previously-imported directories. If you re-tag an album and want
  beets to revisit it, use `beet import -I <dir>` or clear it from the incremental log.
- Keep a git-tracked backup or snapshot of the library before the first bulk `write:yes`
  run — tag writes are in-place and not trivially reversible.
- The override table stays the final authority (§2). beets/Picard fill the baseline; your
  manual corrections always win.
