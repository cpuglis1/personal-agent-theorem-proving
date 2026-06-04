"""
google_bootstrap.py — One-time Google OAuth setup

Purpose:
    Runs the OAuth 2.0 consent flow for the Google Workspace tool and writes
    the resulting refresh token to ~/.config/agent-google/token.json.
    Only needs to be run once (or again if the token is revoked).

Usage:
    1. Download your OAuth client JSON from Google Cloud Console
       (APIs & Services → Credentials → OAuth client ID → Desktop app)
       and save it as ~/.config/agent-google/credentials.json.
    2. Run:  python google_bootstrap.py
    3. A browser window opens; sign in and grant the requested scopes.
    4. token.json is written to ~/.config/agent-google/ with mode 0600.

The open-webui container reads token.json from its read-only bind-mount at
/secrets/google. Refresh tokens are long-lived; the tool refreshes the
access token in memory without needing write access to the mounted file.

Role in the system:
    Part of the ``agents/ai-workflow-agent`` project. This is a standalone,
    operator-run bootstrap helper (not invoked at request time). It exists
    purely to mint the long-lived Google credentials that the Google Workspace
    tool/OWUI integration later consumes. Because it opens a local browser for
    the consent flow, it is meant to be run interactively on a developer/admin
    machine, not inside a headless container.

Design notes:
    - Uses ``InstalledAppFlow`` (the "Desktop app" OAuth client type), which
      spins up a transient local web server to catch the OAuth redirect.
    - Unlike the rest of the ecosystem (which routes LLM calls through the
      LiteLLM proxy), this script talks directly to Google's OAuth endpoints —
      that convention applies to LLM calls, not to auth/identity flows.
    - token.json is written with mode 0600 so the refresh token is readable
      only by the owning user before it is bind-mounted into the container.
"""

from pathlib import Path

from google_auth_oauthlib.flow import InstalledAppFlow

# OAuth scopes requested during the consent flow. These are the minimum grants
# the Google Workspace tool needs: full Drive access plus Gmail read/modify and
# send. Changing this list requires re-running the bootstrap to obtain a token
# covering the new scopes (existing tokens are scoped at consent time).
SCOPES = [
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.send",
]

# Per-user config directory holding both the input client secrets and the
# output token. Kept under ~/.config so it is outside the repo and gitignored
# by virtue of living in the home directory.
CFG = Path.home() / ".config" / "agent-google"
CREDS = CFG / "credentials.json"  # input: OAuth client JSON from Google Cloud Console
TOKEN = CFG / "token.json"        # output: refresh/access token written by this script

def main() -> None:
    """Run the interactive OAuth consent flow and persist the resulting token.

    Reads the OAuth client secrets from ``CREDS`` (``credentials.json``), opens
    a local browser window for the user to sign in and grant ``SCOPES``, then
    writes the obtained credentials (including the long-lived refresh token) to
    ``TOKEN`` (``token.json``) with restrictive 0600 permissions.

    Side effects:
        - Starts a transient local web server on an ephemeral port to receive
          the OAuth redirect (``run_local_server(port=0)``).
        - Opens the system default browser for interactive consent.
        - Writes/overwrites ``TOKEN`` on disk and chmods it to 0600.
        - Prints the written token path to stdout.

    Raises:
        SystemExit: If ``CREDS`` (the OAuth client JSON) does not exist yet,
            with a message instructing where to place it.
    """
    # Fail fast with a friendly message if the operator hasn't supplied the
    # OAuth client secrets yet — nothing downstream can work without them.
    if not CREDS.exists():
        raise SystemExit(f"Place your OAuth client JSON at {CREDS} first.")
    flow = InstalledAppFlow.from_client_secrets_file(str(CREDS), SCOPES)
    creds = flow.run_local_server(port=0)
    TOKEN.write_text(creds.to_json())
    TOKEN.chmod(0o600)
    print(f"Wrote {TOKEN}")

if __name__ == "__main__":
    main()
