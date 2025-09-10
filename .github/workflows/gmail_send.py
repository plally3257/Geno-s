import base64, os, pathlib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

SEND_MODE = os.getenv("SEND_MODE", "draft")  # 'draft' or 'send'
FROM_EMAIL = os.environ["FROM_EMAIL"]
RECIPIENTS_CSV = os.getenv("RECIPIENTS")

CLIENT_ID = os.environ["GMAIL_CLIENT_ID"]
CLIENT_SECRET = os.environ["GMAIL_CLIENT_SECRET"]
REFRESH_TOKEN = os.environ["GMAIL_REFRESH_TOKEN"]

def get_creds():
    return Credentials(
        None,
        refresh_token=REFRESH_TOKEN,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=CLIENT_ID,
        client_secret=CLIENT_SECRET,
        scopes=["https://www.googleapis.com/auth/gmail.send","https://www.googleapis.com/auth/gmail.compose"]
    )

def load_content():
    subject = pathlib.Path("out/subject.txt").read_text(encoding="utf-8").strip()
    html = pathlib.Path("out/body.html").read_text(encoding="utf-8")
    return subject, html

def load_recipients():
    if RECIPIENTS_CSV:
        return [e.strip() for e in RECIPIENTS_CSV.split(",") if e.strip()]
    p = pathlib.Path("recipients.txt")
    if p.exists():
        return [l.strip() for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]
    return []

def build_message(subject, html, recipients):
    msg = MIMEMultipart("alternative")
    msg["From"] = FROM_EMAIL
    msg["To"] = ", ".join(recipients) if recipients else FROM_EMAIL
    msg["Subject"] = subject
    msg.attach(MIMEText(html, "html"))
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    return {"raw": raw}

def main():
    subject, html = load_content()
    recipients = load_recipients()
    service = build("gmail", "v1", credentials=get_creds())

    if SEND_MODE.lower() == "draft":
        subject = f"[DRAFT] {subject}"

    message = build_message(subject, html, recipients)

    if SEND_MODE.lower() == "draft":
        draft = service.users().drafts().create(userId="me", body={"message": message}).execute()
        print(f"Created Gmail draft id: {draft.get('id')}")
    else:
        sent = service.users().messages().send(userId="me", body=message).execute()
        print(f"Sent message id: {sent.get('id')}")

if __name__ == "__main__":
    main()
