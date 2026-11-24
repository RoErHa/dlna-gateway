# Gateway contract: renderers, live state and video (rAudio V3.0)

What the endpoints behind rAudio's E6 (remote control) and E7 (video) really do.
Nothing here is a new endpoint. Measured 2026-11-24 on gateway 2.3.2 (the parts new
in **2.4.0** are marked, and measured there), the renderer half against `tests/fake_renderer.py` (so not on a real Naim), video and
HTTPS over the tailnet name. *(code)* marks what was read, not measured. Where this differs from rAudio's plan it is marked ⚠.

| Story | Endpoint | Verdict |
|---|---|---|
| RA-60 output picker | `GET /api/renderers` | works |
| RA-61 send an album | `POST /api/render_queue` | works |
| RA-62 live state | `GET /api/events` + `GET /api/renderer_state` | works (2.4.0); before it, ⚠ polling only |
| RA-63 volume, transport | `POST /api/control` | works (2.4.0): trim, read back, and seek; ⚠ no absolute volume |
| RA-64 take over | `POST /api/render_queue` → 409 | works |
| RA-70…72 video | `GET /api/videos`, `/video/…`, `/video_hls/…` | works; ⚠ no MKV to test with |

Needs 2.4.0. Before 2.3.2, malformed `tracks` broke a renderer's queue, and an unknown
`/api/control` action answered `{"ok": true}` and did nothing.

## Renderers

`GET /api/renderers` → `[{udn, name, location, av_url, rc_url}]`. Use `udn` and
`name`; the rest are LAN addresses rAudio must not call. **The TV is in the list
too.** It is empty for a short while after a gateway restart.

## Queue

`POST /api/render_queue`, JSON:

| Field | |
|---|---|
| `udn` | the renderer |
| `tracks` | list of objects. `url`: required text, the track's own url as the gateway gave it (the renderer fetches it; NOT a `/stream` url). `title`, `artist`, `album`, `art`, `mime`: text, may be missing or null. `duration`: `"H:MM:SS.mmm"` or seconds, may be missing or null. Other fields pass through (`is_stream: true` marks radio) |
| `force` | `true` takes over a busy renderer |
| `book` | `true` saves the audiobook bookmark while it plays, about every 15 s *(code)* |
| `start_at_sec` | start inside the first track. The seek follows once it plays |

| Status | Body | When |
|---|---|---|
| 200 | `{"ok": true, "tracks": n}` | accepted. The queue starts AFTERWARDS: a renderer that is off is still a 200, so read the state *(code)* |
| 400 | `{"error": …}` | not JSON, no tracks, or a track that breaks the rules above; nothing is touched |
| 404 | `{"error": …}` | no such renderer |
| 405 | | not a POST |
| 409 | `{"error": "renderer_busy", "message", "busy_with": {"title", "artist", "renderer"}}` | it is playing OR PAUSED with another queue |

A 409 comes back for rAudio's own queue too. A new queue sets the volume to the
gateway's startup level once, and the trim to 0, also on a take-over *(code)*.

## State

`GET /api/renderer_state?udn=…` → always 200:

```json
{"state": "playing", "alive": true, "paused": false, "renderer": "Naim",
 "title": "…", "artist": "…", "album": "…", "art": "…", "uri": "…",
 "media_title": "…", "position": 2.0, "duration": 14.0,
 "queue_pos": 1, "queue_len": 4, "trim_db": 0.0}
```

- `state`: `playing`, `paused`, `stopped`. `alive` is true for playing and paused.
- A renderer that never had a queue, or an unknown `udn`, has ONLY `state`,
  `alive`, `renderer`, `queue_pos`, `queue_len`: the rest must be optional.
- `position` is seconds, and `null` at 0:00 and when the renderer gives none.
  `duration` comes from the renderer.
- After the last track `state` is `stopped` while `title` still names it.
- Cached for 0.5 s; a renderer that stopped answering is not asked again for 30 s
  *(code)*.
- Without `udn`: the first one alive, plus `queues` by `udn`.
- ⚠ It answers for the GATEWAY's queue *(code)*. Music started from the Naim's own app
  before any queue was sent reads as `stopped` and is not "busy". After one, the
  renderer's real state is shown with the OLD queue's artist, album and cover, and it
  does count as busy.
- `trim_db` (2.4.0) is what the gateway last set for this queue; a volume change on the
  Naim itself is not in it, and neither is the absolute volume.

## Control

`POST /api/control` `{"device": "upnp:<udn>", "action": …, "value": …}`

| `action` | Does |
|---|---|
| `pause` | a TOGGLE: pauses, and resumes. Look at `paused` first |
| `stop` | stops and ends the queue's playback; harmless when stopped |
| `next`, `prev` | moves; at either end it does nothing and still answers ok |
| `trim_db` | `value` −5…+5 dB around the startup level; outside is clamped |
| `seek` (2.4.0) | `value` seconds from where it is now, e.g. `-30` |
| `seek_abs` (2.4.0) | `value` seconds from the start of the track |

A seek answers `{"ok": true, "position": <target>}`. It stays inside the track: never
before 0, never closer than 3 s to the end, so +30 near the end does not skip. Show
the answered `position` until the state agrees with it: for seconds after a seek the
Naim reports none, or the OLD one, and it can take 4 s to answer (measured on the Naim
2026-11-24). Quick relative presses add up.

| Status | `error` | When |
|---|---|---|
| 400 | a sentence | `value` is missing or not a finite number (also for `trim_db`; there a missing value counts as 0) |
| 409 | `not_seekable` | radio, nothing of ours playing, the queue is over, still starting, or the position is unknown |
| 502 | `seek_failed` | the renderer refused |

Any other action is a 400. `{"ok": true}` means the gateway took the command, not that
the renderer obeyed. ⚠ There is **no mute, no absolute volume and no jump to a queue
position**.

Trim measured with startup level 22: +3 → 28, −5 → 12, +99 → 32 (clamped).

## Events

`GET /api/events` (SSE): `hello` first, a `: keepalive` comment every 45 s, at most
64 streams (503 + `Retry-After` beyond). `state`, `devices`, `index`, `libraries`.

A `state` event is a nudge: `{"type": "state", "udn": "…"}`, then read that renderer's
state. Reading it after the event is never answered from the 0.5 s cache.

| Change | `state` event, 2.4.0 | before |
|---|---|---|
| queue started, next, prev, stop, take-over | at once | yes, without `udn` |
| pause and resume, trim, seek, through the gateway | at once | **no** |
| gapless change to the next track | within about 2 s | **no** |
| paused or resumed on the device itself | within about 2 s | **no** |
| the last track ends, or the queue is given up | within about 2 s | **no** |
| stopped on the device: the gateway starts the NEXT track | within about 2 s | yes |
| the position moving on | no: count it yourself | no |

"Within 2 s" is the monitor's own poll; it starts 4 s into a track. Expect the same
event twice now and then (the command, then the device confirming it). After the queue
is over nobody watches the device, so music started from its own app sends none.

So RA-62 can be events, plus a slow poll as the safety net; "updates within
1 s" holds for anything done through the gateway. The stopped-on-the-device row is worth
a warning in the app: **Stop on the Naim itself skips to the next track**, because the
gateway can't tell it from a track that ended. Only `stop` here ends a queue. Measured
on the fake: verify on the Naim in Sprint 16, with seek.

## Video

- `GET /api/videos`: everything in one reply (3,055 clips, 1.4 MB), newest first, no
  paging. `{id, title, folder, duration (s), width, height, vcodec, acodec, container,
  mime, created, location_name, country, people[], playUrl, transcodeUrl, hlsUrl,
  posterUrl}`. `posterUrl` can be `""`. Urls are paths on the same host.
- `GET /api/video_meta?id=`: one of them, 404 when unknown.
- `GET /video/{id}`: the original. Ranges answer 206 with the WHOLE range asked for
  (`bytes=0-` gives the whole file): the 8 MB slices of `/stream` do not apply, so
  AVPlayer should need no loader. `HEAD` is a 405. 404 also when its drive is away *(code)*.
- `GET /video_hls/{id}/index.m3u8`: VOD playlist of 6 s H.264/AAC segments, made when
  asked for. First segment 1.4 s and 11 MB for the newest HEVC clip: fine on Wi-Fi, heavy
  on 4G. 503 without ffmpeg *(code)*. A segment past the end is a 200 without video, not a 404.
- Codecs today: HEVC 1,657, H.264 1,380, ProRes 16, MJPEG 2, all in MP4 or QuickTime.
  ⚠ **There is no MKV**, so RA-71's "an MKV clip falls back" can't be shown. The two
  MJPEG clips are the nearest thing to try.

## Over HTTPS

`:8443` on the tailnet name answers all of this over HTTP/2, and the request guard
accepts a POST without `Origin`.
