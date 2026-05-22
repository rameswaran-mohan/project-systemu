#!/usr/bin/env python3
"""send_email — Send an email with an optional file attachment using SMTP.

Parameters (via run() kwargs):
  recipient (str, required): Email address of the recipient.
  subject (str, required): Subject line of the email.
  body (str, required): Text content of the email.
  attachment_path (str, optional): Path to the file to attach.

Returns (dict):
  success (bool): True if the operation succeeded.
  error (str|None): Error message on failure, None on success.
"""
from __future__ import annotations
import smtplib
import os
import mimetypes
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email import encoders
from pathlib import Path

TOOL_META = {
    "name": "send_email",
    "tool_type": "api_call",
    "dependencies": [],
}


def run(recipient: str, subject: str, body: str, attachment_path: str = "") -> dict:
    """Send an email via SMTP_SSL with optional attachment."""
    if not recipient or not subject or not body:
        return {"success": False, "error": "recipient, subject, and body are required"}

    # Configuration would typically come from environment variables
    smtp_server = os.getenv("SMTP_SERVER", "smtp.gmail.com")
    smtp_port = int(os.getenv("SMTP_PORT", 465))
    sender_email = os.getenv("SMTP_USER")
    sender_password = os.getenv("SMTP_PASSWORD")

    if not all([sender_email, sender_password]):
        return {"success": False, "error": "SMTP credentials not configured"}

    try:
        msg = MIMEMultipart()
        msg["From"] = sender_email
        msg["To"] = recipient
        msg["Subject"] = subject
        msg.attach(MIMEText(body, "plain"))

        if attachment_path:
            path = Path(attachment_path)
            if not path.exists() or not path.is_file():
                return {"success": False, "error": f"Attachment not found: {attachment_path}"}
            
            ctype, encoding = mimetypes.guess_type(attachment_path)
            if ctype is None or encoding is not None:
                ctype = "application/octet-stream"
            maintype, subtype = ctype.split("/", 1)

            with open(attachment_path, "rb") as f:
                part = MIMEBase(maintype, subtype)
                part.set_payload(f.read())
            
            encoders.encode_base64(part)
            part.add_header("Content-Disposition", f"attachment; filename={path.name}")
            msg.attach(part)

        with smtplib.SMTP_SSL(smtp_server, smtp_port, timeout=30) as server:
            server.login(sender_email, sender_password)
            server.sendmail(sender_email, recipient, msg.as_string())

        return {"success": True, "error": None}
    except smtplib.SMTPException as exc:
        return {"success": False, "error": f"SMTP error: {str(exc)}"}
    except Exception as exc:
        return {"success": False, "error": str(exc)}