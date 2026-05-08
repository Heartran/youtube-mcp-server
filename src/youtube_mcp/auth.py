"""OAuth 2.0 authentication for YouTube APIs — multi-channel token storage.

Token layout under config_dir (default: ~/.youtube-mcp/):
  tokens/_default.json         → {"channel_id": "UCxxxxxx"}
  tokens/{channel_id}.json     → Credentials JSON (mode 0600)
  token.json                   → legacy single-token file (auto-migrated on first run)
"""

import json
import os
import threading
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

# All scopes we need across all phases
SCOPES = [
    "https://www.googleapis.com/auth/youtube.readonly",
    "https://www.googleapis.com/auth/youtube",
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/yt-analytics.readonly",
    "https://www.googleapis.com/auth/yt-analytics-monetary.readonly",
]

DEFAULT_CONFIG_DIR = Path.home() / ".youtube-mcp"
TOKEN_FILE = "token.json"        # legacy single-token filename
TOKENS_SUBDIR = "tokens"         # multi-token subdirectory


class AuthError(Exception):
    pass


class YouTubeAuth:
    """Manages OAuth 2.0 credentials and builds API service clients.

    Supports multi-channel storage: each channel's refresh token lives in
    ``tokens/{channel_id}.json``. The active default is tracked in
    ``tokens/_default.json``. On first startup the old ``token.json`` is
    migrated automatically.
    """

    def __init__(
        self,
        client_secret_path: str | Path | None = None,
        config_dir: str | Path | None = None,
        api_key: str | None = None,
    ):
        self.config_dir = Path(config_dir) if config_dir else DEFAULT_CONFIG_DIR
        self.token_path = self.config_dir / TOKEN_FILE   # legacy path, kept for compat
        self.tokens_dir = self.config_dir / TOKENS_SUBDIR
        self._credentials_cache: dict[str, Credentials] = {}
        self._lock = threading.Lock()

        # Resolve client_secret.json path
        if client_secret_path:
            self.client_secret_path = Path(client_secret_path)
        else:
            env_path = os.environ.get("YOUTUBE_MCP_CLIENT_SECRET")
            if env_path:
                self.client_secret_path = Path(env_path)
            else:
                self.client_secret_path = self.config_dir / "client_secret.json"

        # API key fallback for public-only operations
        self.api_key = api_key or os.environ.get("YOUTUBE_API_KEY")

        # One-time migration of legacy token.json → tokens/{channel_id}.json
        self._try_migrate_legacy_token()

    # ── Internal paths ────────────────────────────────────────────────────────

    def _token_path_for(self, channel_id: str) -> Path:
        return self.tokens_dir / f"{channel_id}.json"

    @property
    def _default_path(self) -> Path:
        return self.tokens_dir / "_default.json"

    # ── Default channel ───────────────────────────────────────────────────────

    def get_default_channel_id(self) -> str | None:
        """Return the channel_id marked as default, or None."""
        with self._lock:
            if not self._default_path.exists():
                return None
            try:
                data = json.loads(self._default_path.read_text())
                return data.get("channel_id") or None
            except Exception:
                return None

    def _write_default(self, channel_id: str | None) -> None:
        """Persist _default.json. Acquires the lock internally."""
        with self._lock:
            self.tokens_dir.mkdir(parents=True, exist_ok=True)
            self._default_path.write_text(
                json.dumps({"channel_id": channel_id or ""})
            )

    def set_default_channel_id(self, channel_id: str) -> None:
        """Set the default channel. Raises AuthError if not authorized."""
        if not self._token_path_for(channel_id).exists():
            raise AuthError(
                f"Channel '{channel_id}' is not authorized. Run add_channel first."
            )
        self._write_default(channel_id)

    # ── Per-channel credential storage ───────────────────────────────────────

    def _save_channel_token_nolock(self, channel_id: str, creds: Credentials) -> None:
        """Write tokens/{channel_id}.json. Caller must already hold self._lock."""
        self.tokens_dir.mkdir(parents=True, exist_ok=True)
        path = self._token_path_for(channel_id)
        path.write_text(creds.to_json())
        try:
            path.chmod(0o600)
        except Exception:
            pass  # Windows: chmod is effectively a no-op; home dir is user-only anyway

    def _save_channel_token(self, channel_id: str, creds: Credentials) -> None:
        with self._lock:
            self._save_channel_token_nolock(channel_id, creds)

    def load_credentials(self, channel_id: str) -> Credentials:
        """Load (and refresh if expired) credentials for the given channel_id."""
        with self._lock:
            cached = self._credentials_cache.get(channel_id)
            if cached and cached.valid:
                return cached

        path = self._token_path_for(channel_id)
        if not path.exists():
            raise AuthError(
                f"No token found for channel '{channel_id}'. "
                "Use add_channel to authorize it."
            )

        try:
            creds = Credentials.from_authorized_user_file(str(path), SCOPES)
        except Exception as e:
            raise AuthError(f"Failed to load credentials for '{channel_id}': {e}") from e

        if creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())  # network call — do NOT hold lock here
                with self._lock:
                    self._save_channel_token_nolock(channel_id, creds)
            except Exception as e:
                raise AuthError(
                    f"Token refresh failed for '{channel_id}': {e}"
                ) from e

        if not creds.valid:
            raise AuthError(
                f"Credentials for channel '{channel_id}' are invalid. Re-run add_channel."
            )

        with self._lock:
            self._credentials_cache[channel_id] = creds
        return creds

    def load_default_credentials(self) -> Credentials:
        """Load credentials for the default channel.

        Falls back to the legacy token.json if no multi-channel default is set.
        """
        channel_id = self.get_default_channel_id()
        if channel_id:
            return self.load_credentials(channel_id)

        # Backward compat: try legacy token.json
        creds = self._load_token()
        if creds:
            if creds.expired and creds.refresh_token:
                try:
                    creds.refresh(Request())
                    self._save_token(creds)
                except Exception:
                    pass
            if creds.valid:
                return creds

        raise AuthError(
            "No default channel configured. Use add_channel to authorize a YouTube channel."
        )

    def get_authorized_channel_ids(self) -> list[str]:
        """Return sorted list of all authorized channel IDs."""
        if not self.tokens_dir.exists():
            return []
        return sorted(p.stem for p in self.tokens_dir.glob("UC*.json"))

    # ── Migration ─────────────────────────────────────────────────────────────

    def _try_migrate_legacy_token(self) -> None:
        """Migrate old token.json → tokens/{channel_id}.json (best-effort, runs once)."""
        if self.tokens_dir.exists() or not self.token_path.exists():
            return
        try:
            creds = self._load_token()
            if not creds:
                return
            if creds.expired and creds.refresh_token:
                creds.refresh(Request())
            yt = build("youtube", "v3", credentials=creds)
            resp = yt.channels().list(part="id", mine=True).execute()
            items = resp.get("items", [])
            if not items:
                return
            channel_id = items[0]["id"]
            with self._lock:
                self._save_channel_token_nolock(channel_id, creds)
            self._write_default(channel_id)
        except Exception:
            pass  # best-effort; legacy auth path still works as fallback

    # ── OAuth flow (used by add_channel tool) ─────────────────────────────────

    def run_add_channel_flow(self, set_as_default: bool = False) -> dict:
        """Run browser OAuth consent flow, save the new channel token.

        Returns a dict with channel_id, title, and set_as_default.
        Raises AuthError if client_secret is missing, flow fails, or the
        channel is already authorized.
        """
        if not self.client_secret_path.exists():
            raise AuthError(
                f"client_secret.json not found at {self.client_secret_path}. "
                "Download it from Google Cloud Console "
                "(APIs & Services > Credentials > OAuth 2.0 Client IDs)."
            )
        flow = InstalledAppFlow.from_client_secrets_file(
            str(self.client_secret_path), SCOPES
        )
        try:
            creds = flow.run_local_server(port=0)
        except Exception as e:
            raise AuthError(f"OAuth flow failed: {e}") from e

        # Identify the authorised channel
        yt = build("youtube", "v3", credentials=creds)
        resp = yt.channels().list(part="id,snippet", mine=True).execute()
        items = resp.get("items", [])
        if not items:
            raise AuthError(
                "OAuth succeeded but no channel was found for this Google account."
            )

        channel = items[0]
        channel_id: str = channel["id"]
        title: str = channel.get("snippet", {}).get("title", "")

        # Reject duplicates
        if self._token_path_for(channel_id).exists():
            raise AuthError(
                f"Channel '{channel_id}' ({title}) is already authorized. "
                "Remove it first with remove_channel if you want to refresh the token."
            )

        self._save_channel_token(channel_id, creds)

        # Set as default if first channel or explicitly requested
        is_first = not self.get_default_channel_id()
        made_default = set_as_default or is_first
        if made_default:
            self._write_default(channel_id)

        return {"channel_id": channel_id, "title": title, "set_as_default": made_default}

    def remove_channel_token(self, channel_id: str) -> None:
        """Delete tokens/{channel_id}.json. Updates default if it was the removed channel."""
        path = self._token_path_for(channel_id)
        if not path.exists():
            raise AuthError(f"Channel '{channel_id}' is not authorized.")

        with self._lock:
            path.unlink()
            self._credentials_cache.pop(channel_id, None)

        current_default = self.get_default_channel_id()
        if current_default == channel_id:
            remaining = self.get_authorized_channel_ids()
            self._write_default(remaining[0] if remaining else None)

    # ── Legacy single-token methods (backward compat) ─────────────────────────

    def _load_token(self) -> Credentials | None:
        """Load saved credentials from legacy token.json."""
        if not self.token_path.exists():
            return None
        try:
            return Credentials.from_authorized_user_file(str(self.token_path), SCOPES)
        except Exception:
            return None

    def _save_token(self, creds: Credentials) -> None:
        """Save credentials to legacy token.json."""
        self.config_dir.mkdir(parents=True, exist_ok=True)
        self.token_path.write_text(creds.to_json())

    def authenticate(self) -> Credentials:
        """Get valid credentials, running OAuth flow if needed.

        Tries the multi-channel default first, then falls back to a fresh
        InstalledAppFlow (legacy single-channel behaviour).
        """
        try:
            return self.load_default_credentials()
        except AuthError:
            pass

        if not self.client_secret_path.exists():
            raise AuthError(
                f"client_secret.json not found at {self.client_secret_path}. "
                "Download it from Google Cloud Console "
                "(APIs & Services > Credentials > OAuth 2.0 Client IDs) "
                "and place it at this path, or set YOUTUBE_MCP_CLIENT_SECRET env var."
            )
        try:
            flow = InstalledAppFlow.from_client_secrets_file(
                str(self.client_secret_path), SCOPES
            )
            creds = flow.run_local_server(port=0)
            self._save_token(creds)
            return creds
        except Exception as e:
            raise AuthError(f"OAuth flow failed: {e}") from e

    @property
    def credentials(self) -> Credentials:
        """Backward-compat property: returns default channel credentials."""
        return self.load_default_credentials()

    # ── Service builders ──────────────────────────────────────────────────────

    def build_youtube_service(self, channel_id: str | None = None):
        """Build a YouTube Data API v3 service client."""
        creds = (
            self.load_credentials(channel_id) if channel_id
            else self.load_default_credentials()
        )
        return build("youtube", "v3", credentials=creds)

    def build_youtube_analytics_service(self, channel_id: str | None = None):
        """Build a YouTube Analytics API service client."""
        creds = (
            self.load_credentials(channel_id) if channel_id
            else self.load_default_credentials()
        )
        return build("youtubeAnalytics", "v2", credentials=creds)

    def build_youtube_reporting_service(self):
        """Build a YouTube Reporting API service client."""
        return build("youtubereporting", "v1", credentials=self.load_default_credentials())

    def build_public_youtube_service(self):
        """Build a YouTube Data API client using API key only (public data)."""
        if not self.api_key:
            raise AuthError(
                "No API key available. Set YOUTUBE_API_KEY env var for public-only access."
            )
        return build("youtube", "v3", developerKey=self.api_key)

    # ── Status ────────────────────────────────────────────────────────────────

    def status(self) -> dict:
        """Return current auth status."""
        channels = self.get_authorized_channel_ids()
        default = self.get_default_channel_id()

        if channels:
            return {
                "authenticated": True,
                "channels": channels,
                "default_channel": default,
                "token_dir": str(self.tokens_dir),
            }

        # Fallback: report legacy token state
        creds = self._load_token()
        if creds and creds.valid:
            return {
                "authenticated": True,
                "scopes": list(creds.scopes or []),
                "token_path": str(self.token_path),
                "expired": False,
            }
        if creds and creds.expired:
            return {
                "authenticated": False,
                "expired": True,
                "has_refresh_token": bool(creds.refresh_token),
                "token_path": str(self.token_path),
            }
        return {
            "authenticated": False,
            "token_exists": self.token_path.exists(),
            "client_secret_exists": self.client_secret_path.exists(),
            "client_secret_path": str(self.client_secret_path),
        }
