#!/usr/bin/env python3
"""
dlna_alerts_sms.py — the text-message channel for dlna_alerts: this Mac's own
Messages app sends the alert to the owner's phone.

WHY MESSAGES (2026-11-04, the owner's choice): it stays local. No SMS
provider, no account, no third party holding the messages. Messages sends a
real SMS through the owner's iPhone (Text Message Forwarding), or an
iMessage. The one thing it cannot report is this Mac being down, and the
owner accepted that: a dead Mac announces itself the moment you try to use
the gateway.

  ALERT_SMS_TO       the phone number (or Apple ID) to text. Unset = off.
  ALERT_SMS_SERVICE  "SMS" (default) or "iMessage" — tried first; the other
                     is the fallback, so a missing SMS relay still delivers.

THE ONE RULE HERE: the number and the text are passed to AppleScript as
ARGUMENTS (`on run argv`), never pasted into the script source. An alert
message can contain text a stranger chose (a refused request's Host header),
and string-building AppleScript would let it run as code.

macOS asks once for permission to let the gateway control Messages (System
Settings → Privacy & Security → Automation). Until that is granted the send
fails with error -1743, which is logged.

Guarded by tests/test_alerts.py::TestSms.
"""
import logging
import os
import subprocess

log = logging.getLogger("dlna.alerts")

_TIMEOUT = 30.0
_MAX_CHARS = 600            # a few SMS segments; the details are in the log

# Fixed source. Everything variable arrives through argv.
APPLESCRIPT = '''on run argv
    set theNumber to item 1 of argv
    set theText to item 2 of argv
    set firstType to item 3 of argv
    tell application "Messages"
        if firstType is "iMessage" then
            set order to {iMessage, SMS}
        else
            set order to {SMS, iMessage}
        end if
        set lastError to "no Messages account for SMS or iMessage"
        repeat with svcType in order
            try
                set svc to 1st account whose service type = (contents of svcType)
                send theText to participant theNumber of svc
                return "sent via " & ((contents of svcType) as text)
            on error errMsg number errNum
                set lastError to errMsg & " (" & errNum & ")"
            end try
        end repeat
        error lastError
    end tell
end run
'''


def configured(env=None) -> bool:
    return bool(((os.environ if env is None else env).get("ALERT_SMS_TO") or "").strip())


def sms_text(title: str, message: str) -> str:
    text = f"DLNA Gateway: {title}\n{message}"
    return text if len(text) <= _MAX_CHARS else text[:_MAX_CHARS - 1] + "…"


def command(number: str, text: str, service: str = "SMS") -> list[str]:
    """The osascript invocation. Pure, so the argv-not-source rule is tested."""
    first = "iMessage" if service.strip().lower() == "imessage" else "SMS"
    return ["/usr/bin/osascript", "-e", APPLESCRIPT, number, text, first]


def send(title: str, message: str, env=None, run=subprocess.run) -> None:
    """Text the alert. Raises on failure (dlna_alerts logs it)."""
    env = os.environ if env is None else env
    number = (env.get("ALERT_SMS_TO") or "").strip()
    service = (env.get("ALERT_SMS_SERVICE") or "SMS").strip()
    r = run(command(number, sms_text(title, message), service),
            capture_output=True, text=True, timeout=_TIMEOUT)
    if r.returncode != 0:
        # The number is left out of the message on purpose — and scrubbed
        # from Messages' own error text, which can quote the participant.
        # The weekly jobs write this text to their logs.
        detail = (r.stderr or r.stdout).strip()
        if number:
            detail = detail.replace(number, "<number>")
        raise RuntimeError(f"Messages refused: {detail[:300]}")
    log.debug(f"alert sms: {r.stdout.strip()}")
