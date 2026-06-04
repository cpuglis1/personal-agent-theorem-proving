"""
title: Google Workspace
author: Charlie Tolleson
version: 0.2.0
license: MIT
required_open_webui_version: 0.4.0
requirements: google-auth==2.35.0, google-auth-oauthlib==1.2.1, google-api-python-client==2.149.0
description: Search and read Gmail messages and Google Drive files; create Drive files. Email sending is off by default and requires admin opt-in via Valves.
"""

from __future__ import annotations

import base64
import json
import os
from email.message import EmailMessage
from pathlib import Path
from typing import Optional

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaIoBaseDownload, MediaInMemoryUpload
from pydantic import BaseModel, Field


"""
Google Workspace — Open WebUI tool

Purpose:
    Gives chat models read/write access to the user's Gmail and Google Drive
    via a personal OAuth token. Supports Drive search, text-file reads (with
    auto-export for Google Docs/Sheets), Drive file creation, Gmail search,
    full message reads, and (optionally) sending plain-text email.

Implementation:
    - Uses OAuth 2.0 user credentials (not a service account), which is required
      for personal Gmail access. The refresh token lives in token.json, written
      to ~/.config/agent-google/ on the host by google_bootstrap.py.
    - The token directory is bind-mounted read-only into the container at
      /secrets/google. Token refresh happens in process memory so the container
      never needs write access to the credentials file.
    - Credentials are loaded and refreshed lazily on first use and cached for
      the lifetime of the Tools instance.
    - Drive read auto-exports Google Docs to plain text and Sheets to CSV using
      the Drive export API; binary formats are refused with a clear error.
    - send_email is gated behind the allow_send_email Valve (default False) so
      the model cannot send mail until an admin explicitly enables it.
    - All three Google API client objects are built with cache_discovery=False
      to avoid a file-system cache that causes warnings inside the container.
"""


# OAuth scopes requested for the user credentials. These must match (or be a
# subset of) the scopes granted when token.json was minted by
# google_bootstrap.py, otherwise refresh/calls fail with a scope error.
#   - drive:        full read/write to Drive (search, read, create files)
#   - gmail.modify: read/search Gmail (also allows label changes, unused here)
#   - gmail.send:   required for send_email (gated behind a Valve)
SCOPES = [
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.send",
]

# Hard cap (bytes) on files drive_read will pull into memory. Guards against
# blowing up the chat context / process memory on large documents.
_MAX_DOC_BYTES = 1_000_000


class Tools:
    """Open WebUI tool class exposing Gmail and Google Drive operations.

    Open WebUI discovers each public method on this class as a callable tool and
    surfaces it to the chat model. Methods return JSON strings (or ``ERROR: ...``
    strings) so results are model-friendly and never raise out of the tool layer.

    Authentication uses cached OAuth user credentials loaded lazily on first use
    (see :meth:`_credentials`). Configuration lives on the nested :class:`Valves`
    model, which Open WebUI renders as admin-editable settings.
    """

    class Valves(BaseModel):
        """Admin-configurable settings rendered by Open WebUI.

        Attributes:
            secrets_dir: Container path to the read-only directory holding
                ``token.json`` (and ``credentials.json``). Defaults to the
                ``GOOGLE_SECRETS_DIR`` env var or ``/secrets/google``.
            send_from: Optional ``From`` header for outgoing mail; empty means
                use the authenticated user's address.
            allow_send_email: Master kill-switch for :meth:`send_email`. Off by
                default so the model cannot send mail without explicit opt-in.
        """

        secrets_dir: str = Field(
            default=os.environ.get("GOOGLE_SECRETS_DIR", "/secrets/google"),
            description="Container path of the read-only directory holding token.json (and credentials.json).",
        )
        send_from: str = Field(
            default="",
            description="Optional 'From' header for Gmail sends. Defaults to the authenticated user.",
        )
        allow_send_email: bool = Field(
            default=False,
            description="Master switch for send_email. Defaults to OFF so the model cannot send mail "
                        "until you explicitly enable it.",
        )

    def __init__(self) -> None:
        """Initialize tool state.

        Sets default Valves, enables Open WebUI citation rendering, and primes
        the credential cache to empty (credentials are loaded lazily on first
        API call rather than at construction time).
        """
        self.valves = self.Valves()
        self.citation = True
        # Cached OAuth credentials; populated on first _credentials() call.
        self._creds: Optional[Credentials] = None

    # ------------------------------------------------------------------ auth

    def _credentials(self) -> Credentials:
        """Return valid OAuth user credentials, loading/refreshing as needed.

        Credentials are cached on the instance and reused while valid. On a cold
        call (or when the cache is invalid) the refresh token in ``token.json``
        is loaded and refreshed in process memory.

        Returns:
            Valid :class:`google.oauth2.credentials.Credentials`.

        Raises:
            RuntimeError: If ``token.json`` is missing from ``secrets_dir``.

        Side effects:
            Populates ``self._creds`` and may perform a network token refresh.
        """
        # Fast path: return cached creds if still valid.
        if self._creds and self._creds.valid:
            return self._creds
        token_path = Path(self.valves.secrets_dir) / "token.json"
        if not token_path.exists():
            raise RuntimeError(
                f"token.json not found at {token_path}. Run google_bootstrap.py on the host first."
            )
        creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())            # in-memory refresh; mount is RO by design
        self._creds = creds
        return creds

    def _drive(self):
        """Build an authenticated Drive v3 client.

        ``cache_discovery=False`` disables the on-disk discovery cache, which is
        unwritable / noisy inside the container.

        Returns:
            A googleapiclient Drive v3 service resource.
        """
        return build("drive", "v3", credentials=self._credentials(), cache_discovery=False)

    def _gmail(self):
        """Build an authenticated Gmail v1 client.

        ``cache_discovery=False`` disables the on-disk discovery cache, which is
        unwritable / noisy inside the container.

        Returns:
            A googleapiclient Gmail v1 service resource.
        """
        return build("gmail", "v1", credentials=self._credentials(), cache_discovery=False)

    # ------------------------------------------------------------------ Drive

    def drive_search(self, query: str, max_results: int = 10) -> str:
        """
        Search Google Drive using Drive's native query syntax.

        :param query: Drive query, e.g. "name contains 'report'" or "mimeType='application/pdf'".
        :param max_results: Max files to return (1–50).
        :return: JSON array of {"id", "name", "mimeType", "modifiedTime", "webViewLink"}.
        """
        max_results = max(1, min(50, max_results))
        try:
            res = self._drive().files().list(
                q=query, pageSize=max_results,
                fields="files(id, name, mimeType, modifiedTime, webViewLink)",
            ).execute()
            return json.dumps(res.get("files", []), indent=2)
        except HttpError as e:
            return f"ERROR: drive API: {e}"
        except Exception as e:
            return f"ERROR: drive_search failed: {e!r}"

    def drive_read(self, file_id: str) -> str:
        """
        Read the text content of a Drive file. Google Docs are exported to plain text;
        text/* files are returned as UTF-8; binary files are refused.

        :param file_id: Drive file ID.
        :return: File contents as text, or an ERROR message.
        """
        try:
            drive = self._drive()
            meta = drive.files().get(fileId=file_id, fields="id, name, mimeType, size").execute()
            mime = meta.get("mimeType", "")
            size = int(meta.get("size") or 0)
            if size and size > _MAX_DOC_BYTES:
                return f"ERROR: file is {size} bytes; max is {_MAX_DOC_BYTES}."

            # Native Google formats have no raw bytes, so they must be exported.
            # Plain files can be downloaded directly via get_media. Anything else
            # (PDF, images, binary) is refused rather than returning garbage.
            if mime == "application/vnd.google-apps.document":
                request = drive.files().export_media(fileId=file_id, mimeType="text/plain")
            elif mime.startswith("text/") or mime in ("application/json", "application/xml"):
                request = drive.files().get_media(fileId=file_id)
            elif mime == "application/vnd.google-apps.spreadsheet":
                request = drive.files().export_media(fileId=file_id, mimeType="text/csv")
            else:
                return f"ERROR: unsupported mimeType '{mime}' for text read."

            from io import BytesIO
            buf = BytesIO()
            downloader = MediaIoBaseDownload(buf, request)
            done = False
            while not done:
                _, done = downloader.next_chunk()
            return buf.getvalue().decode("utf-8", errors="replace")
        except HttpError as e:
            return f"ERROR: drive API: {e}"
        except Exception as e:
            return f"ERROR: drive_read failed: {e!r}"

    def drive_write(self, name: str, content: str, parent_folder_id: Optional[str] = None,
                    mime_type: str = "text/plain") -> str:
        """
        Create a new file in Google Drive with the given text content.

        :param name: File name (including extension, e.g. "summary.md").
        :param content: UTF-8 text content.
        :param parent_folder_id: Optional Drive folder ID. If omitted, the file is placed at root of "My Drive".
        :param mime_type: MIME type to record on the file (default text/plain).
        :return: JSON {"id", "name", "webViewLink"}.
        """
        try:
            metadata = {"name": name}
            if parent_folder_id:
                metadata["parents"] = [parent_folder_id]
            media = MediaInMemoryUpload(content.encode("utf-8"), mimetype=mime_type, resumable=False)
            file = self._drive().files().create(
                body=metadata, media_body=media,
                fields="id, name, webViewLink",
            ).execute()
            return json.dumps(file)
        except HttpError as e:
            return f"ERROR: drive API: {e}"
        except Exception as e:
            return f"ERROR: drive_write failed: {e!r}"

    # ------------------------------------------------------------------ Gmail

    def gmail_search(self, query: str, max_results: int = 10) -> str:
        """
        Search Gmail using standard Gmail search syntax.

        :param query: Gmail query, e.g. "from:boss@example.com is:unread newer_than:7d".
        :param max_results: Max messages to return (1–25).
        :return: JSON array of {"id", "threadId", "subject", "from", "date", "snippet"}.
        """
        max_results = max(1, min(25, max_results))
        try:
            gmail = self._gmail()
            res = gmail.users().messages().list(
                userId="me", q=query, maxResults=max_results,
            ).execute()
            out = []
            for m in res.get("messages", []):
                full = gmail.users().messages().get(
                    userId="me", id=m["id"], format="metadata",
                    metadataHeaders=["Subject", "From", "Date"],
                ).execute()
                headers = {h["name"]: h["value"] for h in full.get("payload", {}).get("headers", [])}
                out.append({
                    "id": full["id"],
                    "threadId": full["threadId"],
                    "subject": headers.get("Subject", ""),
                    "from": headers.get("From", ""),
                    "date": headers.get("Date", ""),
                    "snippet": full.get("snippet", ""),
                })
            return json.dumps(out, indent=2)
        except HttpError as e:
            return f"ERROR: gmail API: {e}"
        except Exception as e:
            return f"ERROR: gmail_search failed: {e!r}"

    def gmail_read(self, message_id: str) -> str:
        """
        Read the plain-text body of a Gmail message.

        :param message_id: Gmail message ID.
        :return: JSON {"subject", "from", "to", "date", "body"} or an ERROR message.
        """
        try:
            msg = self._gmail().users().messages().get(
                userId="me", id=message_id, format="full",
            ).execute()
            headers = {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}

            def walk(part) -> str:
                """Depth-first search a MIME part tree for the first text/plain body.

                Gmail messages are nested MIME trees (e.g. multipart/alternative
                wrapping text/plain + text/html). This recurses into child parts
                and returns the first base64url-decoded text/plain payload found.

                Args:
                    part: A Gmail message payload part dict.

                Returns:
                    Decoded plain-text body, or "" if no text/plain part exists.
                """
                if part.get("mimeType") == "text/plain" and part.get("body", {}).get("data"):
                    return base64.urlsafe_b64decode(part["body"]["data"]).decode("utf-8", errors="replace")
                # Recurse into nested parts (multipart messages).
                for child in part.get("parts", []) or []:
                    text = walk(child)
                    if text:
                        return text
                return ""

            # Fall back to the API-provided snippet if no plain-text body found.
            body = walk(msg["payload"]) or msg.get("snippet", "")
            return json.dumps({
                "subject": headers.get("Subject", ""),
                "from": headers.get("From", ""),
                "to": headers.get("To", ""),
                "date": headers.get("Date", ""),
                "body": body,
            })
        except HttpError as e:
            return f"ERROR: gmail API: {e}"
        except Exception as e:
            return f"ERROR: gmail_read failed: {e!r}"

    def send_email(self, to: str, subject: str, body: str, cc: Optional[str] = None) -> str:
        """
        Send a plain-text email from the authenticated Gmail account.
        Disabled by default — admin must flip `allow_send_email` valve to True.

        :param to: Comma-separated list of recipient addresses.
        :param subject: Email subject.
        :param body: Plain-text body.
        :param cc: Optional comma-separated CC list.
        :return: JSON {"id", "threadId"} or an ERROR message.
        """
        # Safety gate: refuse to send unless an admin explicitly opted in.
        if not self.valves.allow_send_email:
            return "ERROR: sending email is disabled. Enable the 'allow_send_email' valve to use this tool."
        try:
            mime = EmailMessage()
            mime["To"] = to
            if cc:
                mime["Cc"] = cc
            mime["Subject"] = subject
            if self.valves.send_from:
                mime["From"] = self.valves.send_from
            mime.set_content(body)

            raw = base64.urlsafe_b64encode(mime.as_bytes()).decode("utf-8")
            sent = self._gmail().users().messages().send(
                userId="me", body={"raw": raw},
            ).execute()
            return json.dumps({"id": sent["id"], "threadId": sent["threadId"]})
        except HttpError as e:
            return f"ERROR: gmail API: {e}"
        except Exception as e:
            return f"ERROR: send_email failed: {e!r}"
