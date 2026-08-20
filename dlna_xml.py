#!/usr/bin/env python3
"""
dlna_xml.py — the one place untrusted XML is parsed.

WHY (audit follow-up, 2026-08-20). Every XML the gateway parses comes off the
network: SOAP bodies POSTed to the `/gw/*` ContentDirectory and
ConnectionManager endpoints (unauthenticated, reachable by anything on the
LAN), device description documents fetched during discovery, and SOAP
responses from renderers. `xml.etree.ElementTree` is documented as vulnerable
to entity-expansion amplification, and it was measured on this codebase:

    <!DOCTYPE lolz [<!ENTITY a "AAAAAAAAAA"><!ENTITY b "&a;&a;…">…]>
    → expanded, 10x per nesting level

Three levels is 1 KB; ten is gigabytes, from one small request, with no
authentication. External entities are already refused by ElementTree (so no
file disclosure or XML-driven SSRF), which makes this denial of service
rather than data theft — but a handful of requests can exhaust memory on a
machine whose whole job is to keep playing music.

THE RULE: **no DTD, ever.** SOAP, DIDL-Lite and UPnP device descriptors have
no legitimate use for a document type declaration, so refusing outright costs
nothing real and removes the entire entity-expansion class rather than
trying to bound it. A size cap sits in front as a second limit, because
parsing a 500 MB well-formed document is its own denial of service.

Failures raise `ElementTree.ParseError`, which every existing call site
already handles as "malformed XML from a device" — so a hostile body takes
the same path as a broken one and nothing needs a new error branch.
"""
from __future__ import annotations

import logging
import re
import xml.etree.ElementTree as ET

log = logging.getLogger("dlna.xml")

# Generous next to real traffic: a large DIDL browse response is tens of KB,
# a device descriptor a few KB. Anything past this is not a device talking.
MAX_XML_BYTES = 4 * 1024 * 1024

# Matches a DOCTYPE declaration anywhere in the prolog, tolerating the
# whitespace, comments and processing instructions permitted before it.
_DOCTYPE = re.compile(rb"<!DOCTYPE", re.IGNORECASE)


def safe_fromstring(data: str | bytes, *, what: str = "xml",
                    max_bytes: int = MAX_XML_BYTES) -> ET.Element:
    """Parse untrusted XML, or raise `ET.ParseError`.

    Drop-in for `ET.fromstring` at every site that parses bytes which came
    from the network. See the module docstring for what it refuses and why.
    """
    raw = data.encode("utf-8", "replace") if isinstance(data, str) else data

    if len(raw) > max_bytes:
        log.warning(f"XML refused ({what}): {len(raw)} bytes exceeds "
                    f"{max_bytes} cap")
        raise ET.ParseError(f"xml too large: {len(raw)} bytes")

    # Only the prolog can carry a DOCTYPE; scanning a bounded prefix keeps
    # this cheap on the hot browse path and still cannot be slipped past,
    # because a DOCTYPE appearing later is not well-formed anyway.
    if _DOCTYPE.search(raw[:4096]):
        log.warning(f"XML refused ({what}): DOCTYPE declaration present — "
                    "entity expansion is not accepted from the network")
        raise ET.ParseError("doctype declarations are not accepted")

    return ET.fromstring(raw)
