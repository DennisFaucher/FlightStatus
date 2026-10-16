"""Outbound SMTP alerts (Gmail app password friendly).

Credentials come from the environment (.env) and are never logged.
"""

import logging
import os
import re
import smtplib
import ssl
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr

log = logging.getLogger("flightstatus.mailer")

DEFAULT_FROM_NAME = "Flight Status"


class MailError(Exception):
    """Raised when an alert could not be handed to the SMTP server."""


def _cfg(name, default=""):
    return (os.environ.get(name) or default).strip()


def configured():
    return bool(_cfg("MAIL_HOST") and _cfg("MAIL_USERNAME") and _cfg("MAIL_PASSWORD"))


def port():
    try:
        return int(_cfg("MAIL_PORT", "587") or 587)
    except ValueError:
        return 587


def from_address():
    frm = _cfg("MAIL_FROM") or _cfg("MAIL_USERNAME")
    if not frm:
        return None
    return formataddr((_cfg("MAIL_FROM_NAME", DEFAULT_FROM_NAME), frm))


def default_recipient():
    """Where alerts go when the tracked flight's owner has no address on file."""
    return _cfg("MAIL_TO") or _cfg("MAIL_FROM") or _cfg("MAIL_USERNAME") or None


def send(to_addr, subject, text_body, html_body=None):
    if not configured():
        raise MailError("SMTP is not configured (MAIL_HOST / MAIL_USERNAME / MAIL_PASSWORD)")
    if not to_addr or "@" not in to_addr:
        raise MailError("no valid recipient address")

    host = _cfg("MAIL_HOST")
    smtp_port = port()
    user = _cfg("MAIL_USERNAME")
    # Google prints app passwords in groups of four; it ignores the spaces, so
    # normalize them away in case the value is pasted with them (or as a stray
    # tab/newline).
    password = re.sub(r"\s+", "", _cfg("MAIL_PASSWORD"))

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = from_address()
    msg["To"] = to_addr
    msg.attach(MIMEText(text_body, "plain", "utf-8"))
    if html_body:
        msg.attach(MIMEText(html_body, "html", "utf-8"))

    context = ssl.create_default_context()
    try:
        if smtp_port == 465:
            with smtplib.SMTP_SSL(host, smtp_port, timeout=30, context=context) as smtp:
                smtp.login(user, password)
                smtp.send_message(msg)
        else:
            with smtplib.SMTP(host, smtp_port, timeout=30) as smtp:
                smtp.ehlo()
                smtp.starttls(context=context)
                smtp.ehlo()
                smtp.login(user, password)
                smtp.send_message(msg)
    except smtplib.SMTPAuthenticationError as exc:
        # Gmail returns 535 for a bad/expired app password or 2FA turned off.
        raise MailError(
            "SMTP authentication failed (check the 16-char app password and "
            "that 2-Step Verification is enabled)"
        ) from exc
    except (smtplib.SMTPException, OSError) as exc:
        raise MailError(f"SMTP error: {exc}") from exc

    log.info("alert sent to %s: %s", to_addr, subject)
