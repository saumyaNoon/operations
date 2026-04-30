"""
nim-agents-ops api/lib/gmail_client.py

two modes via GMAIL_MODE env (mirrors nim-agents-sc):
  oauth (default)  — gmail api drafts via oauth, drops into "matrix" label
  file             — writes .eml files under drafts/<date>/, useful when
                     gmail api enable is blocked by workspace admin

ops-ds and sc both write to the same "matrix" label, but are distinguishable
by subject prefix (`[ops-...]` vs `[exec/struct/fnv/...]`).
"""
import os, base64, re
from datetime import datetime
from email.mime.text import MIMEText

LIB_DIR = os.path.dirname(__file__)
ROOT = os.path.normpath(os.path.join(LIB_DIR, "..", ".."))
TOKEN_PATH = os.path.join(ROOT, "credentials", "token.json")
CREDS_PATH = os.path.join(ROOT, "credentials", "gmail_oauth.json")
DRAFTS_DIR = os.path.join(ROOT, "drafts")

MODE = os.environ.get("GMAIL_MODE", "oauth").lower()

SCOPES = [
    "https://www.googleapis.com/auth/gmail.compose",
    "https://www.googleapis.com/auth/gmail.labels",
]

_LABEL_CACHE = None
_SERVICE_CACHE = None


def _get_service():
    global _SERVICE_CACHE
    if _SERVICE_CACHE:
        return _SERVICE_CACHE

    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build

    creds = None
    if os.path.exists(TOKEN_PATH):
        creds = Credentials.from_authorized_user_file(TOKEN_PATH, SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not os.path.exists(CREDS_PATH):
                raise FileNotFoundError(
                    f"{CREDS_PATH} missing. download oauth client id (desktop) "
                    f"from google cloud console and save as gmail_oauth.json, "
                    f"or set GMAIL_MODE=file to dump drafts as .eml files."
                )
            flow = InstalledAppFlow.from_client_secrets_file(CREDS_PATH, SCOPES)
            creds = flow.run_local_server(port=0)
        with open(TOKEN_PATH, "w") as f:
            f.write(creds.to_json())

    _SERVICE_CACHE = build("gmail", "v1", credentials=creds, cache_discovery=False)
    return _SERVICE_CACHE


def _get_matrix_label_id():
    global _LABEL_CACHE
    if _LABEL_CACHE:
        return _LABEL_CACHE
    service = _get_service()
    labels = service.users().labels().list(userId="me").execute().get("labels", [])
    for lbl in labels:
        if lbl["name"].lower() == "matrix":
            _LABEL_CACHE = lbl["id"]
            return lbl["id"]
    created = service.users().labels().create(
        userId="me",
        body={"name": "matrix", "labelListVisibility": "labelShow",
              "messageListVisibility": "show"}
    ).execute()
    _LABEL_CACHE = created["id"]
    return created["id"]


def _create_draft_oauth(to_list, cc_list, subject, body_html):
    service = _get_service()
    label_id = _get_matrix_label_id()

    msg = MIMEText(body_html, "html", "utf-8")
    msg["to"] = ", ".join([a for a in to_list if a])
    if cc_list:
        msg["cc"] = ", ".join([a for a in cc_list if a])
    msg["subject"] = subject

    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    draft = service.users().drafts().create(
        userId="me",
        body={"message": {"raw": raw, "labelIds": [label_id, "DRAFT"]}}
    ).execute()
    return draft["id"]


def _slug(s, n=60):
    s = re.sub(r"[^A-Za-z0-9._-]+", "_", str(s))
    return s[:n].strip("_") or "draft"


def _create_draft_file(to_list, cc_list, subject, body_html):
    today = datetime.now().strftime("%Y-%m-%d")
    day_dir = os.path.join(DRAFTS_DIR, today)
    os.makedirs(day_dir, exist_ok=True)

    msg = MIMEText(body_html, "html", "utf-8")
    msg["to"] = ", ".join([a for a in to_list if a])
    if cc_list:
        msg["cc"] = ", ".join([a for a in cc_list if a])
    msg["subject"] = subject

    ts = datetime.now().strftime("%H%M%S")
    fname = f"{ts}_{_slug(subject)}.eml"
    path = os.path.join(day_dir, fname)
    with open(path, "w", encoding="utf-8") as f:
        f.write(msg.as_string())
    return f"file::{today}/{fname}"


def create_draft_in_matrix(to_list, cc_list, subject, body_html):
    """create a gmail draft (oauth) or .eml file (file mode)."""
    if MODE == "file":
        return _create_draft_file(to_list, cc_list, subject, body_html)
    return _create_draft_oauth(to_list, cc_list, subject, body_html)


def list_matrix_drafts(query_prefix="[ops-", limit=100):
    """list drafts in the matrix label whose subject starts with query_prefix.
    file-mode: scans drafts/ and returns recent .eml files."""
    if MODE == "file":
        return _list_matrix_drafts_file(query_prefix, limit)
    service = _get_service()
    label_id = _get_matrix_label_id()
    q = f'subject:"{query_prefix}" label:matrix in:draft'
    res = service.users().drafts().list(userId="me", q=q, maxResults=limit).execute()
    drafts = res.get("drafts", []) or []
    out = []
    for d in drafts:
        full = service.users().drafts().get(
            userId="me", id=d["id"], format="metadata"
        ).execute()
        headers = {h["name"].lower(): h["value"]
                   for h in full.get("message", {}).get("payload", {}).get("headers", [])}
        out.append({
            "draft_id": d["id"],
            "subject": headers.get("subject", ""),
            "to": headers.get("to", ""),
            "cc": headers.get("cc", ""),
            "date": headers.get("date", ""),
        })
    return out


def _list_matrix_drafts_file(query_prefix, limit):
    if not os.path.isdir(DRAFTS_DIR):
        return []
    out = []
    for day in sorted(os.listdir(DRAFTS_DIR), reverse=True):
        day_path = os.path.join(DRAFTS_DIR, day)
        if not os.path.isdir(day_path):
            continue
        for fname in sorted(os.listdir(day_path), reverse=True):
            if not fname.endswith(".eml"):
                continue
            fpath = os.path.join(day_path, fname)
            with open(fpath, encoding="utf-8") as fh:
                head = fh.read(2048)
            subj = ""
            for line in head.splitlines():
                if line.lower().startswith("subject:"):
                    subj = line.split(":", 1)[1].strip()
                    break
            if query_prefix and not subj.startswith(query_prefix):
                continue
            out.append({
                "draft_id": f"file::{day}/{fname}",
                "subject": subj,
                "date": day,
                "path": fpath,
            })
            if len(out) >= limit:
                return out
    return out
