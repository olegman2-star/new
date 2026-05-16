"""
Run this ONCE on your local machine to get a YouTube refresh token.

Steps:
  1. Install dependencies:  pip install google-auth-oauthlib
  2. Set your credentials:
       export YOUTUBE_CLIENT_ID=your_client_id
       export YOUTUBE_CLIENT_SECRET=your_client_secret
  3. Run:  python scripts/get_youtube_token.py
  4. A browser window opens — sign in and grant access.
  5. Copy the printed REFRESH TOKEN and save it as a GitHub secret:
       YOUTUBE_REFRESH_TOKEN=<printed value>
"""

import os
from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]

client_id = os.environ.get("YOUTUBE_CLIENT_ID")
client_secret = os.environ.get("YOUTUBE_CLIENT_SECRET")

if not client_id or not client_secret:
    raise SystemExit("Set YOUTUBE_CLIENT_ID and YOUTUBE_CLIENT_SECRET env vars first.")

client_config = {
    "installed": {
        "client_id": client_id,
        "client_secret": client_secret,
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": "https://oauth2.googleapis.com/token",
        "redirect_uris": ["urn:ietf:wg:oauth:2.0:oob", "http://localhost"],
    }
}

flow = InstalledAppFlow.from_client_config(client_config, SCOPES)
creds = flow.run_local_server(port=0)

print("\n" + "=" * 60)
print("REFRESH TOKEN (save this as GitHub secret YOUTUBE_REFRESH_TOKEN):")
print(creds.refresh_token)
print("=" * 60 + "\n")
