import base64
import os
import pathlib
import sys
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError


def fail(msg: str, code: int = 1):
    print(f"[ERROR] {msg}", file=sys.stderr)
    sys.exit(code)


# ---- Required env vars (set in GitHub Actions 'env:' via secrets) ----
REQUIRED_VARS = [
    "FROM_EMAIL",
    "GMAIL_CLIENT_ID",
    "GMAIL_CLIENT_SECRET",
    "GMAIL_REFRESH_TOKEN",
]
for k in REQUIRED_VARS:
    if not os.environ.get(k):
        fail(f"Missing env var {k}. Check your GitHub Secrets.")

SEND_MODE = os.getenv("SEND_MODE", "draft").strip().lower()  # 'draft' or 'send'
FROM_EMAIL = os.environ["FROM_EMAIL"].strip()
CLIENT_ID = os.environ["GMAIL_CLIENT_ID"].strip()
CLIENT_SECRET = os.environ["GMAIL_CLIENT_SECRET"].strip()
REFRESH_TOKEN = os.environ["GMAIL_REFRESH_TOKEN"].strip()

# Optional recipients (comma-separated). If empty, the email goes to FROM_EMAIL only.
RECIPIENTS_CSV = os.getenv("RECIPIENTS", "").strip()


def get_service():
    """Builds an authenticated Gmail API client using a refresh token."""
    creds = Credentials(
        None,
        refresh_token=REFRESH_TOKEN,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=CLIENT_ID,
        client_secret=CLIENT_SECRET,
        scopes=[
            "https://www.googleapis.com/auth/gmail.send",
            "https://www.googleapis.com/auth/gmail.compose",
        ],
    )
    try:
        return build("gmail", "v1", credentials=creds)
    except Exception as e:
        fail(f"Could not build Gmail service client: {e}")


def load_content():
    """Reads the subject/body produced by compose_email.py."""
    subj_path = pathlib.Path("out/subject.txt")
    body_path = pathlib.Path("out/body.html")
    if not subj_path.exists() or not body_path.exists():
        fail(
            "Email content not found. Expected files 'out/subject.txt' and 'out/body.html'. "
            "Make sure the 'Compose email from ESPN' step ran successfully."
        )
    subject = subj_path.read_text(encoding="utf-8").strip()
    html = body_path.read_text(encoding="utf-8")
    return subject, html


def determine_recipients():
    """Builds the recipient list from RECIPIENTS or falls back to FROM_EMAIL."""
    if RECIPIENTS_CSV:
        recipients = [e.strip() for e in RECIPIENTS_CSV.split(",") if e.strip()]
    else:
        recipients = [FROM_EMAIL]

    if not recipients:
        fail("No recipients found. Set the RECIPIENTS secret or rely on FROM_EMAIL fallback.")
    return recipients


def build_message(subject: str, html: str, recipients: list[str]):
    """Creates a base64-encoded RFC 2822 email object for Gmail API."""
    # Add a [DRAFT] tag to the subject when drafting
    final_subject = f"[DRAFT] {subject}" if SEND_MODE == "draft" else subject

    msg = MIMEMultipart("alternative")
    msg["From"] = FROM_EMAIL
    msg["To"] = ", ".join(recipients)
    msg["Subject"] = final_subject
    msg.attach(MIMEText(html, "html"))

    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    return {"raw": raw}


def send_or_draft(service, message):
    """Sends or creates a draft depending on SEND_MODE."""
    try:
        if SEND_MODE == "draft":
            draft = service.users().drafts().create(userId="me", body={"message": message}).execute()
            print(f"[INFO] Created Gmail draft id: {draft.get('id')}")
        elif SEND_MODE == "send":
            sent = service.users().messages().send(userId="me", body=message).execute()
            print(f"[INFO] Sent message id: {sent.get('id')}")
        else:
            fail(f"Unsupported SEND_MODE='{SEND_MODE}'. Use 'draft' or 'send'.")
    except HttpError as he:
        # Common causes: invalid_grant (refresh token revoked), insufficient permissions (wrong scopes)
        print("[ERROR] Gmail API HttpError:", file=sys.stderr)
        print(str(he), file=sys.stderr)
        print(
            "[HINT] If you see 'invalid_grant' or 'insufficient permissions':\n"
            " - Recreate the refresh token in OAuth Playground with these scopes (one per line):\n"
            "   https://www.googleapis.com/auth/gmail.send\n"
            "   https://www.googleapis.com/auth/gmail.compose\n"
            " - Ensure FROM_EMAIL matches the account you authorized.\n",
            file=sys.stderr,
        )
        raise
    except Exception as e:
        fail(f"Gmail API call failed: {e}")


def main():
    print(f"[INFO] SEND_MODE={SEND_MODE}")
    if SEND_MODE not in ("draft", "send"):
        fail("SEND_MODE must be 'draft' or 'send'.")

    subject, html = load_content()
    recipients = determine_recipients()

    # Basic visibility in logs (don’t print full addresses)
    print(f"[INFO] Subject: {subject}")
    print(f"[INFO] Recipients: {len(recipients)} (first: {recipients[0]})")

    message = build_message(subject, html, recipients)
    service = get_service()
    send_or_draft(service, message)
    print("[INFO] Gmail step completed.")


if __name__ == "__main__":
    main()
