"""
Sends transactional email (currently just password-reset links) via Gmail
SMTP using an App Password — see .env: GMAIL_ADDRESS, GMAIL_APP_PASSWORD.
"""
import os, smtplib
from email.mime.text import MIMEText

GMAIL_ADDRESS = os.environ.get("GMAIL_ADDRESS", "")
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD", "")


def send_email(to: str, subject: str, body: str):
    """Raises on failure — callers decide how much of that to surface."""
    if not GMAIL_ADDRESS or not GMAIL_APP_PASSWORD:
        raise RuntimeError(
            "Email not configured — set GMAIL_ADDRESS and GMAIL_APP_PASSWORD in .env"
        )
    msg = MIMEText(body)
    msg["Subject"] = subject
    msg["From"] = GMAIL_ADDRESS
    msg["To"] = to

    with smtplib.SMTP("smtp.gmail.com", 587) as server:
        server.starttls()
        server.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
        server.sendmail(GMAIL_ADDRESS, [to], msg.as_string())
