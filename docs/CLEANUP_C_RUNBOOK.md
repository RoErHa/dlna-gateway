# Cleanup C — fold `/gw/*` into the ASGI app (runbook)

Retire the separate plain-HTTP **device server** (`dlna_server.py` on `:8770`)
by serving the Naim-facing `/gw/*` UPnP surface **from the ASGI app itself** on
the plain `:8765` bind. End state: Hypercorn is the **only** server; `:8770` +
`GATEWAY_PORT` + `dlna_server.py` + `run-2.0.sh` are gone.

> Prepared 2026-06-11 (recon current as of this date). Test-first; the **Naim
> verification (step B6) is the hard gate** before the Phase-C deletions.

---

## End state

| | now | after |
|---|---|---|
| PWA + `/api` + `/rest` | ASGI `:8765` (plain) + `:8443` (TLS h2) | unchanged |
| `/gw/*` (UPnP, Naim) | separate `dlna_server.DeviceHandler` on **`:8770`** | **ASGI app on `:8765`** (plain; `:8443` also serves it but the Naim can't TLS) |
| SSDP advert + `device.xml` URLBase | `http://<ip>:8770/gw/device.xml` | `http://<ip>:8765/gw/device.xml` |
| Servers running | Hypercorn + the device `ThreadedHTTPServer` | **Hypercorn only** |

The Naim only ever requests `/gw/*` paths; they coexist with `/api`/PWA on
`:8765` by path. The `device.xml` URLBase **MUST stay plain `http://…:8765`**
(the Naim can't do HTTPS).

---

## Current state (facts, file:line — verified 2026-06-11)

**The 4 `/gw` routes** (all in `api_upnp.py`, mapped in `dlna_routes.py`
`GET_ROUTES`/`POST_ROUTES`), no real GENA eventing:
- `GET /gw/device.xml` → `device_xml(h, params)` — `port = h.server.server_address[1]` (**:8770**), `lan_ip = _get_lan_ip()`, returns `_gw_device_xml(lan_ip, port)`.
- `GET /gw/cd/desc.xml` → `cd_desc_xml(h, params)` — returns `_gw_cd_desc_xml()` (static).
- `GET /gw/cd/events` → `cd_events(h, params)` — **stub** (`send_response(200)`, no body; no `SUBSCRIBE`).
- `POST /gw/cd/control` → `cd_control(h, body)` — parses the SOAP body, calls `_gw_browse(obj_id, flag, start, count)` + `_gw_browse_response(...)`, returns via `h._xml_response`. **Pure helpers already exist** (`_gw_device_xml` / `_gw_cd_desc_xml` / `_gw_browse` / `_gw_browse_response`); only the SOAP-parse glue lives in the handler.

**ASGI exclusion** (`dlna_asgi.py`): `_bridgeable()` (~line 628) excludes
`/gw/*`; the bridged-POST loop (~646) also `not path.startswith("/gw/")`.

**Lifespan** (`dlna_asgi.py` ~105-135): with services on,
`start_background_services(lan_ip, port)` (spawns the `gw_ssdp_announcer` thread,
`dlna_gateway.py:129`) **and** `dlna_server.start_device_server("0.0.0.0", port)`
— both with `port = GATEWAY_PORT` (`8770`). Shutdown calls
`gw_ssdp_byebye(lan_ip, 8770)`.

**Imports of `dlna_server`** (the module to delete): `dlna_asgi.py:41` (`import
dlna_server` — for `start_device_server`); `dlna_gateway.py:29-30` (`GW_UDN,
ThreadedHTTPServer, GatewayHandler, gw_ssdp_announcer, gw_ssdp_byebye`). Note
`dlna_server.py:30` only **re-exports** `GW_UDN`/`gw_ssdp_announcer`/
`gw_ssdp_byebye` **from `api_upnp`** — so importers can get them from `api_upnp`
directly. `ThreadedHTTPServer`/`GatewayHandler` are used **only** by
`dlna_gateway.main()` (the stdlib server). Docstring-only mentions:
`api_subsonic.py:1010`, `dlna_routes.py:5`. Tests: `tests/test_asgi.py:994,999`
(`import dlna_server`) + the `GATEWAY_PORT` lifespan tests at 639/674/684.

**Stdlib run path:** `run-2.0.sh` → `exec ./setup.sh --run --port 8766` →
`setup.sh` runs `dlna_gateway.py` (`setup.sh:35`) → `dlna_gateway.main()` (the
stdlib `GatewayHandler` server). This whole path retires.

---

## Phase B — fold (test-first)

**B1 — make the SOAP control + device.xml port pure (`api_upnp.py`).**
- Extract `cd_control_soap(body: bytes) -> bytes` from `cd_control(h, body)` (the
  parse → `_gw_browse` → `_gw_browse_response`), returning the response XML
  bytes. Keep `cd_control(h, body)` as a thin wrapper for now (it's dropped in C
  with the legacy server).
- `device_xml` currently reads the port from `h.server.server_address[1]`. A
  native route has no `h.server` — so the native route passes the **plain port
  (8765)** explicitly into `_gw_device_xml(lan_ip, 8765)`. (Add a `PLAIN_PORT`
  constant, e.g. in `dlna_asgi`, default `8765`, overridable.)

**B2 — native `/gw` routes in `dlna_asgi.py`** (served on both binds; the Naim
uses `:8765`). Reuse the pure `api_upnp` helpers so the SOAP is **byte-identical**:
```python
@app.get("/gw/device.xml", include_in_schema=False)
async def gw_device_xml():
    lan_ip = await run_in_threadpool(dlna_gateway.get_lan_ip)
    return Response(api_upnp._gw_device_xml(lan_ip, PLAIN_PORT),
                    media_type='text/xml; charset="utf-8"')

@app.get("/gw/cd/desc.xml", include_in_schema=False)
async def gw_cd_desc():
    return Response(api_upnp._gw_cd_desc_xml(), media_type='text/xml; charset="utf-8"')

@app.api_route("/gw/cd/events", methods=["GET","SUBSCRIBE"], include_in_schema=False)
async def gw_cd_events():
    return Response(status_code=200)            # stub — no GENA eventing

@app.post("/gw/cd/control", include_in_schema=False)
async def gw_cd_control(request: Request):
    body = await request.body()
    xml  = await run_in_threadpool(api_upnp.cd_control_soap, body)
    return Response(xml, media_type='text/xml; charset="utf-8"')
```
Then **remove the `/gw/*` exclusion** from `_bridgeable()` + the bridged-POST
loop (it's native now, not bridged).

**B3 — repoint SSDP + `device.xml` to `:8765`** (`dlna_asgi.py` lifespan):
- Call `start_background_services(lan_ip, PLAIN_PORT)` (8765) — the SSDP advert
  now points at the ASGI plain port. **Drop** the `dlna_server.start_device_server`
  call + the `device_server.shutdown()`. `gw_ssdp_byebye(lan_ip, PLAIN_PORT)`.
- Remove `import dlna_server` from `dlna_asgi.py`.

**B4 — `dlna_gateway.py` imports:** change
`from dlna_server import (GW_UDN, ThreadedHTTPServer, GatewayHandler, gw_ssdp_announcer, gw_ssdp_byebye)`
→ `from api_upnp import GW_UDN, gw_ssdp_announcer, gw_ssdp_byebye`.
(`ThreadedHTTPServer`/`GatewayHandler` drop with `main()` in Phase C.)

**B5 — tests (write first)** in `tests/test_asgi.py`:
- `GET /gw/device.xml` → valid XML, URLBase contains `:8765`, `<UDN>uuid:dlna-gateway-iina-8765</UDN>`, ContentDirectory service.
- `GET /gw/cd/desc.xml` → SCPD with `Browse`.
- `POST /gw/cd/control` Browse `ObjectID=playlists` → DIDL-Lite lists the playlist containers; Browse a `pl:<id>` → items carry `<res>` stream URLs. (Seed a playlist via the stub DB.)
- `GET /gw/cd/events` → 200.
- Update the `GATEWAY_PORT`/device-server lifespan tests (639/674/684/994/999): no device server now; SSDP advert on the plain port; `dlna_server` no longer imported.

**B6 — gate + commit + DEPLOY + 🔴 NAIM VERIFY (gate before Phase C).**
- Full gate (`run_all.py --offline` + `pytest tests/frontend`). Commit on `2.0`, merge to `main`.
- Restart: `launchctl kickstart -k gui/$(id -u)/com.roha.dlna-gateway`.
- **Naim (user):** the "DLNA Gateway (IINA)" MediaServer re-discovers at `:8765`; browse **Playlists** (incl. the 3 Billboard ones) + **Favourite Albums**; **play one track** (gapless if a segued album). If the Naim cached `:8770`, re-select the server / re-browse (no GENA push). **Only proceed to C once this passes.**

---

## Phase C — retire the stdlib server (after the Naim check)

1. **Delete** `dlna_server.py` + `run-2.0.sh`.
2. **Neuter `dlna_gateway.main()`** — keep `start_background_services` /
   `get_lan_ip` (the ASGI lifespan needs them); remove the
   `ThreadedHTTPServer`/`GatewayHandler` server loop. Make `setup.sh --run`
   launch the ASGI stack (`exec ./run-2.0-asgi.sh "$@"`, or hypercorn directly)
   instead of `dlna_gateway.py`.
3. **Remove `GATEWAY_PORT` plumbing:** `com.roha.dlna-gateway.cutover.plist`
   (the key @82 + comment @21) and the **installed** plist; `run-2.0-asgi.sh`
   (@15/59/71/110); `dlna_asgi.py` (@79/82/89/111/131 — use `PLAIN_PORT`);
   `tests/test_asgi.py` (@639/674/684).
4. **Docstrings:** `api_subsonic.py:1010`, `dlna_routes.py:5` ("called from
   `dlna_server`" → "the ASGI app").
5. **Docs:** CLAUDE.md module table (drop `dlna_server.py` row; note `/gw/*` is
   in `dlna_asgi`); README run section; `tools/gen_architecture_pdf.py` (drop
   `P/g2 dlna_server`, fold `/gw` under `P/g27`, remove `:8770` mentions) →
   **regen the PDF**.
6. Full gate → commit → merge.

---

## Risks / gotchas

- **SOAP byte-fidelity — LOW risk:** the native routes call the **same**
  `_gw_*` helpers, so the Naim sees byte-identical UPnP. The one real risk is the
  **`:8770 → :8765` move** → the Naim re-discovers at the new location (SSDP
  re-announce). That's exactly what **B6** verifies — don't skip it.
- **`device.xml` URLBase must be `http://<ip>:8765`** (plain), never `https`/
  `:8443` — the Naim can't TLS. The native route passes `PLAIN_PORT`.
- **`cd_control` raw body:** the native route reads `await request.body()` (raw
  bytes) and passes to `cd_control_soap` — unlike the param-based POST handlers.
- **`:8443` also serves `/gw`** (same app) over TLS — harmless; the Naim just
  uses `:8765`. Don't let the URLBase advertise the TLS port.
- **Simpler-but-rejected alternative — the bridge:** removing the `/gw`
  exclusion would bridge the handlers, but `device_xml` reads
  `h.server.server_address` which the bridge's fake `h` doesn't have → it'd
  crash. So `device_xml` needs the port refactor regardless; native is the clean
  path.
