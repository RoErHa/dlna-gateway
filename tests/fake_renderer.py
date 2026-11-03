#!/usr/bin/env python3
"""
tests/fake_renderer.py — a UPnP MediaRenderer that makes no sound.

For `tests/chaos.py --fake-renderer`: the playback half of the chaos run
(queue take-overs, 10k-track queues, next/prev/stop/volume) needs a renderer,
and the real ones are a Naim and a TV in someone's living room.

It is registered with the LIVE gateway through real discovery, so the run
exercises the same path a real device takes:

  1. an HTTP server on 127.0.0.1 serves a device description and answers
     AVTransport + RenderingControl SOAP;
  2. an SSDP NOTIFY is sent UNICAST to 127.0.0.1:1900 — never to the
     multicast group, so the Naim, the TV and anything else on the LAN never
     hear of it. The gateway's SSDP rule (LOCATION host must be the datagram
     source) holds: both are 127.0.0.1;
  3. it is taken away again with `POST /api/renderers/forget` (loopback-only),
     because the gateway otherwise never forgets a renderer.

Playback is simulated in TIME, not with a timer thread: each request first
advances the state, so a track "plays" for `track_sec` and then either moves
to the URI set by SetNextAVTransportURI (gapless — the gateway sees TrackURI
change without a STOPPED) or stops, which is what makes the gateway's queue
monitor advance. Unknown actions get a UPnP 401 "Invalid Action" fault, as a
real renderer would answer.

Usable on its own:  .venv/bin/python tests/fake_renderer.py   (Ctrl-C to quit)
"""
from __future__ import annotations

import html
import json
import re
import socket
import threading
import time
import urllib.error
import urllib.request
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

UDN = "uuid:c4a05000-fa6e-4e4d-8e00-0000000000c5"
NAME = "Chaos test (fake)"

_DESC = f"""<?xml version="1.0" encoding="utf-8"?>
<root xmlns="urn:schemas-upnp-org:device-1-0">
  <specVersion><major>1</major><minor>0</minor></specVersion>
  <device>
    <deviceType>urn:schemas-upnp-org:device:MediaRenderer:1</deviceType>
    <friendlyName>{NAME}</friendlyName>
    <manufacturer>dlna-gateway tests</manufacturer>
    <modelName>fake_renderer.py</modelName>
    <UDN>{UDN}</UDN>
    <serviceList>
      <service>
        <serviceType>urn:schemas-upnp-org:service:AVTransport:1</serviceType>
        <serviceId>urn:upnp-org:serviceId:AVTransport</serviceId>
        <controlURL>/AVTransport/control</controlURL>
        <eventSubURL>/AVTransport/event</eventSubURL>
        <SCPDURL>/AVTransport/scpd.xml</SCPDURL>
      </service>
      <service>
        <serviceType>urn:schemas-upnp-org:service:RenderingControl:1</serviceType>
        <serviceId>urn:upnp-org:serviceId:RenderingControl</serviceId>
        <controlURL>/RenderingControl/control</controlURL>
        <eventSubURL>/RenderingControl/event</eventSubURL>
        <SCPDURL>/RenderingControl/scpd.xml</SCPDURL>
      </service>
    </serviceList>
  </device>
</root>"""

_ENVELOPE = ('<?xml version="1.0" encoding="utf-8"?>'
             '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
             's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
             '<s:Body>{}</s:Body></s:Envelope>')

_FAULT = ('<s:Fault><faultcode>s:Client</faultcode><faultstring>UPnPError'
          '</faultstring><detail><UPnPError xmlns="urn:schemas-upnp-org:'
          'control-1-0"><errorCode>401</errorCode><errorDescription>Invalid '
          'Action</errorDescription></UPnPError></detail></s:Fault>')

_AVT = {"SetAVTransportURI", "SetNextAVTransportURI", "Play", "Pause", "Stop",
        "Seek", "Next", "Previous", "GetTransportInfo", "GetPositionInfo",
        "GetMediaInfo"}
_RC = {"SetVolume", "GetVolume", "SetMute", "GetMute"}


def _hms(sec: float) -> str:
    sec = max(0, int(sec))
    return f"{sec // 3600}:{sec % 3600 // 60:02d}:{sec % 60:02d}"


def _arg(body: str, tag: str) -> str:
    m = re.search(rf"<{tag}>(.*?)</{tag}>", body, re.S)
    return html.unescape(m.group(1)).strip() if m else ""


class FakeRenderer:
    def __init__(self, track_sec: float = 3.0, host: str = "127.0.0.1"):
        self.track_sec = track_sec
        self.host = host
        self.udn = UDN
        self.calls: Counter = Counter()
        self.volume = 0
        self._lock = threading.Lock()
        self._state = "NO_MEDIA_PRESENT"
        self._uri = ""
        self._next = ""
        self._pos = 0.0          # position while not PLAYING
        self._t0 = 0.0           # monotonic start of PLAYING, minus position
        self._httpd: ThreadingHTTPServer | None = None

    # ── lifecycle ─────────────────────────────────────────────────
    @property
    def location(self) -> str:
        return f"http://{self.host}:{self._httpd.server_port}/desc.xml"

    def start(self) -> FakeRenderer:
        fake = self

        class _H(BaseHTTPRequestHandler):
            def log_message(self, *a):           # quiet
                pass

            def _send(self, code, body: str, ctype="text/xml; charset=utf-8"):
                data = body.encode()
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                if self.path == "/desc.xml":
                    self._send(200, _DESC)
                else:
                    self._send(404, "")

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(min(n, 1 << 20)).decode("utf-8", "replace")
                action = (self.headers.get("SOAPAction") or "").strip('"').split("#")[-1]
                code, inner = fake.handle(self.path, action, body)
                self._send(code, _ENVELOPE.format(inner))

        self._httpd = ThreadingHTTPServer((self.host, 0), _H)
        self._httpd.daemon_threads = True
        # A short poll so stop() returns at once (the default 0.5 s is
        # paid by every test that starts one).
        threading.Thread(target=self._httpd.serve_forever, args=(0.05,),
                         daemon=True, name="fake-renderer").start()
        return self

    def stop(self) -> None:
        if self._httpd:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None

    # ── the simulated transport ───────────────────────────────────
    def _advance(self, now: float) -> None:
        """Bring the state up to `now` (called with the lock held)."""
        while self._state == "PLAYING" and now - self._t0 >= self.track_sec:
            if self._next:                       # gapless hand-over
                self._uri, self._next = self._next, ""
                self._t0 += self.track_sec
            else:
                self._state, self._pos = "STOPPED", 0.0

    def _position(self, now: float) -> float:
        return now - self._t0 if self._state == "PLAYING" else self._pos

    def handle(self, path: str, action: str, body: str) -> tuple[int, str]:
        service = ("AVTransport" if path.startswith("/AVTransport/") else
                   "RenderingControl" if path.startswith("/RenderingControl/")
                   else "")
        known = _AVT if service == "AVTransport" else _RC if service else set()
        self.calls[action or "?"] += 1
        if action not in known:
            return 500, _FAULT
        ns = f"urn:schemas-upnp-org:service:{service}:1"
        out = ""
        now = time.monotonic()
        with self._lock:
            self._advance(now)
            if action == "SetAVTransportURI":
                self._uri, self._next = _arg(body, "CurrentURI"), ""
                self._state, self._pos = "STOPPED", 0.0
            elif action == "SetNextAVTransportURI":
                self._next = _arg(body, "NextURI")
            elif action == "Play" and self._uri:
                if self._state != "PLAYING":
                    self._t0 = now - self._pos
                    self._state = "PLAYING"
            elif action == "Pause" and self._state == "PLAYING":
                self._pos, self._state = now - self._t0, "PAUSED_PLAYBACK"
            elif action == "Stop":
                self._state = "STOPPED" if self._uri else "NO_MEDIA_PRESENT"
                self._pos = 0.0
            elif action == "Seek":
                parts = [float(p) for p in _arg(body, "Target").split(":")
                         if p.replace(".", "", 1).isdigit()]
                sec = sum(v * 60 ** i for i, v in enumerate(reversed(parts)))
                self._pos = min(sec, self.track_sec)
                if self._state == "PLAYING":
                    self._t0 = now - self._pos
            elif action == "GetTransportInfo":
                out = (f"<CurrentTransportState>{self._state}</CurrentTransportState>"
                       "<CurrentTransportStatus>OK</CurrentTransportStatus>"
                       "<CurrentSpeed>1</CurrentSpeed>")
            elif action == "GetPositionInfo":
                out = (f"<Track>{1 if self._uri else 0}</Track>"
                       f"<TrackDuration>{_hms(self.track_sec)}</TrackDuration>"
                       "<TrackMetaData></TrackMetaData>"
                       f"<TrackURI>{html.escape(self._uri)}</TrackURI>"
                       f"<RelTime>{_hms(self._position(now))}</RelTime>"
                       f"<AbsTime>{_hms(self._position(now))}</AbsTime>"
                       "<RelCount>2147483647</RelCount>"
                       "<AbsCount>2147483647</AbsCount>")
            elif action == "SetVolume":
                v = _arg(body, "DesiredVolume")
                self.volume = int(v) if v.isdigit() else self.volume
            elif action == "GetVolume":
                out = f"<CurrentVolume>{self.volume}</CurrentVolume>"
            elif action == "GetMute":
                out = "<CurrentMute>0</CurrentMute>"
        return 200, f'<u:{action}Response xmlns:u="{ns}">{out}</u:{action}Response>'

    @property
    def state(self) -> str:
        with self._lock:
            self._advance(time.monotonic())
            return self._state

    # ── registering with a gateway ────────────────────────────────
    def announce(self, ssdp_host: str = "127.0.0.1") -> None:
        """One ssdp:alive NOTIFY, UNICAST — never the multicast group."""
        msg = ("NOTIFY * HTTP/1.1\r\n"
               "HOST: 239.255.255.250:1900\r\n"
               "CACHE-CONTROL: max-age=1800\r\n"
               f"LOCATION: {self.location}\r\n"
               "NT: urn:schemas-upnp-org:device:MediaRenderer:1\r\n"
               "NTS: ssdp:alive\r\n"
               "SERVER: macOS UPnP/1.0 fake_renderer/1.0\r\n"
               f"USN: {self.udn}::urn:schemas-upnp-org:device:MediaRenderer:1\r\n"
               "\r\n").encode()
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.sendto(msg, (ssdp_host, 1900))

    def register(self, gateway: str, opener=None, timeout: float = 20.0) -> bool:
        """Announce until the gateway lists us in /api/renderers. The gateway
        waits 1.5 s before fetching a new device and UDP may drop, so this
        re-announces every 3 s."""
        get = (opener or urllib.request.build_opener()).open
        deadline = time.monotonic() + timeout
        next_announce = 0.0
        while time.monotonic() < deadline:
            if time.monotonic() >= next_announce:
                self.announce()
                next_announce = time.monotonic() + 3.0
            try:
                with get(f"{gateway.rstrip('/')}/api/renderers", timeout=5) as r:
                    if any(d.get("udn") == self.udn for d in json.load(r)):
                        return True
            except Exception:                    # noqa: BLE001 — keep polling
                pass
            time.sleep(0.5)
        return False

    def forget(self, gateway: str, opener=None) -> int:
        """`POST /api/renderers/forget` → the HTTP status (0 = no answer)."""
        req = urllib.request.Request(
            f"{gateway.rstrip('/')}/api/renderers/forget", method="POST",
            data=json.dumps({"udn": self.udn}).encode(),
            headers={"Content-Type": "application/json"})
        try:
            with (opener or urllib.request.build_opener()).open(req, timeout=15) as r:
                return r.status
        except urllib.error.HTTPError as e:
            return e.code
        except Exception:                        # noqa: BLE001
            return 0


if __name__ == "__main__":
    fr = FakeRenderer().start()
    print(f"{NAME} at {fr.location} — Ctrl-C to quit")
    try:
        while True:
            time.sleep(5)
            print(f"  state={fr.state}  calls={dict(fr.calls)}")
    except KeyboardInterrupt:
        fr.stop()
