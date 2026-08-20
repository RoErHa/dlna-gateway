#!/usr/bin/env python3
"""
api_subsonic_proto.py — the Subsonic wire protocol: authentication,
the response envelope, and the JSON→XML serialiser.

Split out of api_subsonic.py on 2026-08-20, when that module reached
1,174 lines covering auth, wire format, id codecs, and 33 endpoint handlers.

    api_subsonic_proto.py      auth + response wrapping + the XML serialiser
    api_subsonic_ids.py        id codecs, udn resolution, Subsonic object builders
    api_subsonic_browse.py     ping/artists/albums/search/genres endpoints
    api_subsonic_playlists.py  playlists, starring, scrobble
    api_subsonic_media.py      stream + cover art (the byte endpoints)
    api_subsonic_extras.py     internet radio + audiobook bookmarks
    api_subsonic.py            the _METHODS table, param parsing, handle()

api_subsonic re-exports every public name, so `import api_subsonic` and
`api_subsonic.<anything>` behave exactly as before for callers and tests.

⚠ THIS MODULE OWNS THE FAMILY'S INJECTABLE STATE — `DB`, `SERVERS`
and `SUBSONIC_PASSWORD_OVERRIDE` are bound HERE and nowhere else. Siblings
reach them as `_proto.DB` / `_proto.SERVERS`, an attribute lookup resolved at
CALL time, so a test patch actually lands. Inject with:

    patch.object(api_subsonic_proto, "DB", tmp_db)
    patch.object(api_subsonic_proto, "SERVERS", fake_registry)
    api_subsonic_proto.SUBSONIC_PASSWORD_OVERRIDE = "pw"

Binding any of these in a second module would leave THAT module pointed at
the real library.db while the tests still passed — a false pass, which is
much worse than a crash. tests/test_subsonic.py asserts there is exactly one
binding site.

Two protocol details that clients genuinely depend on:
  * XML is the spec DEFAULT. Amperfy sends no `f=` at all and expects XML;
    answering JSON there breaks it silently.
  * A logical error is still HTTP 200 with the code inside the envelope.
    Only "password not configured" uses a real 503, deliberately, so a
    misconfigured deploy cannot accidentally serve data with an empty-password
    match.
"""
import hashlib
import hmac
import logging
import os

from dlna_discovery import SERVERS  # noqa: F401 — family state, see docstring
from dlna_library import DB  # noqa: F401 — family state, see docstring

log = logging.getLogger("dlna.api.subsonic")


# ── Config / auth ────────────────────────────────────────────────

# Read at call time (not import time) so a `launchctl setenv` change is
# picked up without a gateway restart, and so tests can monkey-patch
# os.environ directly. Module-level fall-backs only used by tests
# that prefer to set the password via attribute (see test_subsonic.py).
SUBSONIC_USER_DEFAULT     = "user"


SUBSONIC_PASSWORD_OVERRIDE: str | None = None  # tests may set


def _subsonic_user() -> str:
    return os.environ.get("SUBSONIC_USER", SUBSONIC_USER_DEFAULT)


def _subsonic_password() -> str:
    if SUBSONIC_PASSWORD_OVERRIDE is not None:
        return SUBSONIC_PASSWORD_OVERRIDE
    return os.environ.get("SUBSONIC_PASSWORD", "")


# Subsonic API version we advertise. 1.16.1 is the modern baseline that
# every contemporary client handles; we don't actually implement all of
# its surface (~15 of 60+ endpoints) but version negotiation is by
# string match in clients.
API_VERSION = "1.16.1"


# clients (notably Amperfy) appear to whitelist server types and
# silently reject unknown ones. Nautiline + other tested clients
# happily accept either; navidrome is the safer Subsonic-flavoured
# identifier for compatibility across the iOS client ecosystem.
SERVER_TYPE = "navidrome"


SERVER_VERSION = "1.0.0"


# ── Subsonic error codes (from the spec) ─────────────────────────
ERR_GENERIC          = 0


ERR_MISSING_PARAM    = 10


ERR_VERSION_INCOMPAT = 30  # client > server


ERR_WRONG_AUTH       = 40


ERR_TOKEN_AUTH_NA    = 41   # ldap-only servers; not us


ERR_NOT_AUTHORIZED   = 50


ERR_NOT_FOUND        = 70


def _check_auth(params: dict) -> bool:
    """Validate Subsonic-flavoured auth params. Accepts:
        ?u=&t=MD5(password+salt)&s=<salt>     (token+salt; modern)
        ?u=&p=<password>                       (plaintext legacy)
        ?u=&p=enc:<hex(password)>             (hex-encoded legacy)
    Returns True on success, False otherwise."""
    pwd = _subsonic_password()
    if not pwd:
        return False  # explicit refuse-all when env not set
    if params.get("u", "") != _subsonic_user():
        return False
    # Modern token+salt. MD5 is mandated by the Subsonic protocol, not a
    # choice we can revisit here — but the COMPARISON is ours, so it is
    # constant-time. `==` on a digest leaks, through timing, how many leading
    # characters matched, which over enough samples reconstructs the token.
    # Network jitter makes that impractical remotely; compare_digest costs
    # nothing and removes the question.
    t = params.get("t", "")
    s = params.get("s", "")
    if t and s:
        expected = hashlib.md5((pwd + s).encode("utf-8")).hexdigest()
        return hmac.compare_digest(t.lower(), expected.lower())
    # Legacy plaintext / hex (also reached when t is present but s isn't)
    p = params.get("p", "")
    if p.startswith("enc:"):
        try:
            p = bytes.fromhex(p[4:]).decode("utf-8")
        except (ValueError, UnicodeDecodeError) as e:
            log.debug(f"Subsonic auth: malformed enc: password ({e})")
            return False
    return bool(p) and hmac.compare_digest(p, pwd)


# ── Response helpers ─────────────────────────────────────────────

def _wrap(payload: dict) -> dict:
    out = {
        "status":        "ok",
        "version":       API_VERSION,
        "type":          SERVER_TYPE,
        "serverVersion": SERVER_VERSION,
        # OpenSubsonic compatibility hint — clients like Amperfy use
        # this to decide "this is a real Subsonic-flavoured server"
        # rather than rejecting unknown server types.
        "openSubsonic":  True,
    }
    out.update(payload)
    return {"subsonic-response": out}


def _wrap_error(code: int, message: str) -> dict:
    return {"subsonic-response": {
        "status":        "failed",
        "version":       API_VERSION,
        "type":          SERVER_TYPE,
        "serverVersion": SERVER_VERSION,
        "openSubsonic":  True,
        "error":         {"code": code, "message": message},
    }}


def _ok(h, payload: dict, http_code: int = 200) -> None:
    _send_response(h, _wrap(payload), http_code)


def _fail(h, code: int, message: str, http_code: int = 200) -> None:
    # Subsonic clients want HTTP 200 even on logical errors; the
    # error code is in the wrapper. Use http_code=503 only for the
    # "password not configured" hard-failure.
    _send_response(h, _wrap_error(code, message), http_code)


def _send_response(h, full_payload: dict, http_code: int) -> None:
    """Dispatch JSON or XML based on the f= param. Subsonic spec
    defaults to XML; clients must ask for JSON. Amperfy notably does
    NOT send f=json — it expects XML. Tested clients (Nautiline,
    substreamer) send f=json and prefer that."""
    fmt = getattr(h, "_subsonic_format", "xml")
    if fmt in ("json", "jsonp"):
        # f=jsonp would need callback wrapping; we don't ship it.
        # JSON works for every modern client and is what our tests use.
        h._json(http_code, full_payload)
        return
    # Default / explicit f=xml
    body = _to_xml_doc(full_payload).encode("utf-8")
    h._xml_response(http_code, body)


# ── JSON → Subsonic XML serialiser ──────────────────────────────
# Subsonic XML pattern:
#   - The wrapper is <subsonic-response xmlns="..." status="..." .../>
#   - Inside any element: scalar properties become attributes;
#     nested dicts become child elements with the property name;
#     arrays of objects become repeated child elements with the
#     property name (so JSON {"playlist": [...]} → <playlist .../>
#     elements, no <playlists><playlist/>... unless that nesting is
#     already in the JSON shape — which it is in our handlers).
# This mechanical conversion works because we already JSON-shape
# responses the way Subsonic XML expects.

def _xml_escape(s) -> str:
    return (str(s).replace("&", "&amp;")
                  .replace("<", "&lt;")
                  .replace(">", "&gt;")
                  .replace('"', "&quot;")
                  .replace("'", "&apos;"))


def _xml_scalar(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if v is None:
        return ""
    return str(v)


def _to_xml(payload, tag: str, with_ns: bool) -> str:
    if isinstance(payload, dict):
        attrs:    list[tuple[str, str]] = []
        children: list[tuple[str, object]] = []
        for k, v in payload.items():
            if isinstance(v, (dict, list)):
                children.append((k, v))
            else:
                attrs.append((k, _xml_scalar(v)))
        if with_ns:
            attrs.insert(0, ("xmlns", "http://subsonic.org/restapi"))
        attr_str = "".join(f' {k}="{_xml_escape(v)}"' for k, v in attrs)
        if not children:
            return f"<{tag}{attr_str}/>"
        inner = "".join(_to_xml(v, k, False) for k, v in children)
        return f"<{tag}{attr_str}>{inner}</{tag}>"
    if isinstance(payload, list):
        # Each item gets its own element with the same tag.
        return "".join(_to_xml(item, tag, False) for item in payload)
    # Bare scalar — shouldn't happen at the response level; emit as text.
    return f"<{tag}>{_xml_escape(_xml_scalar(payload))}</{tag}>"


def _to_xml_doc(wrapped: dict) -> str:
    """`wrapped` is {"subsonic-response": {...}}. Emit XML with the
    namespace on the root element."""
    inner = wrapped.get("subsonic-response", {})
    return ('<?xml version="1.0" encoding="UTF-8"?>'
            + _to_xml(inner, "subsonic-response", with_ns=True))
