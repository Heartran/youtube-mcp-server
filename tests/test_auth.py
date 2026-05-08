"""Tests for auth module — single-channel backward compat and multi-channel."""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from youtube_mcp.auth import AuthError, YouTubeAuth


# ── Helpers ───────────────────────────────────────────────────────────────────

FAKE_CREDS_JSON = json.dumps({
    "token": "test-token",
    "refresh_token": "test-refresh",
    "client_id": "test-client-id",
    "client_secret": "test-client-secret",
    "token_uri": "https://oauth2.googleapis.com/token",
    "scopes": ["https://www.googleapis.com/auth/youtube.readonly"],
})

VALID_CHANNEL_ID = "UC" + "x" * 22  # 24 chars, starts with UC


def _make_auth(tmp_path, **kwargs) -> YouTubeAuth:
    """Create a YouTubeAuth instance pointing at tmp_path (no migration side-effects)."""
    return YouTubeAuth(config_dir=tmp_path, **kwargs)


def _write_channel_token(auth: YouTubeAuth, channel_id: str, creds_json: str = FAKE_CREDS_JSON):
    """Write a fake token file for the given channel_id."""
    auth.tokens_dir.mkdir(parents=True, exist_ok=True)
    path = auth._token_path_for(channel_id)
    path.write_text(creds_json)


# ── Legacy / backward-compat tests ───────────────────────────────────────────

def test_default_config_dir():
    yt_auth = YouTubeAuth()
    assert yt_auth.config_dir == Path.home() / ".youtube-mcp"
    assert yt_auth.token_path == Path.home() / ".youtube-mcp" / "token.json"


def test_custom_config_dir(tmp_path):
    yt_auth = _make_auth(tmp_path)
    assert yt_auth.config_dir == tmp_path
    assert yt_auth.token_path == tmp_path / "token.json"


def test_client_secret_from_env(tmp_path, monkeypatch):
    secret_path = tmp_path / "my_secret.json"
    monkeypatch.setenv("YOUTUBE_MCP_CLIENT_SECRET", str(secret_path))
    yt_auth = YouTubeAuth()
    assert yt_auth.client_secret_path == secret_path


def test_client_secret_explicit_overrides_env(tmp_path, monkeypatch):
    monkeypatch.setenv("YOUTUBE_MCP_CLIENT_SECRET", "/env/path.json")
    explicit = tmp_path / "explicit.json"
    yt_auth = YouTubeAuth(client_secret_path=explicit)
    assert yt_auth.client_secret_path == explicit


def test_api_key_from_env(monkeypatch):
    monkeypatch.setenv("YOUTUBE_API_KEY", "test-key-123")
    yt_auth = YouTubeAuth()
    assert yt_auth.api_key == "test-key-123"


def test_api_key_explicit_overrides_env(monkeypatch):
    monkeypatch.setenv("YOUTUBE_API_KEY", "env-key")
    yt_auth = YouTubeAuth(api_key="explicit-key")
    assert yt_auth.api_key == "explicit-key"


def test_status_no_token(tmp_path):
    yt_auth = _make_auth(tmp_path)
    status = yt_auth.status()
    assert status["authenticated"] is False
    assert status["token_exists"] is False


def test_authenticate_missing_client_secret(tmp_path):
    yt_auth = YouTubeAuth(
        config_dir=tmp_path,
        client_secret_path=tmp_path / "nonexistent.json",
    )
    with pytest.raises(AuthError, match="client_secret.json not found"):
        yt_auth.authenticate()


def test_build_public_youtube_service_no_key(tmp_path):
    yt_auth = _make_auth(tmp_path, api_key=None)
    with patch.dict("os.environ", {}, clear=True):
        yt_auth.api_key = None
        with pytest.raises(AuthError, match="No API key available"):
            yt_auth.build_public_youtube_service()


def test_load_and_save_token(tmp_path):
    yt_auth = _make_auth(tmp_path)

    mock_creds = MagicMock()
    mock_creds.to_json.return_value = FAKE_CREDS_JSON

    yt_auth._save_token(mock_creds)
    assert (tmp_path / "token.json").exists()


# ── Default channel ───────────────────────────────────────────────────────────

def test_get_default_channel_id_none_when_missing(tmp_path):
    yt_auth = _make_auth(tmp_path)
    assert yt_auth.get_default_channel_id() is None


def test_write_and_read_default(tmp_path):
    yt_auth = _make_auth(tmp_path)
    yt_auth._write_default(VALID_CHANNEL_ID)
    assert yt_auth.get_default_channel_id() == VALID_CHANNEL_ID


def test_write_default_none_clears(tmp_path):
    yt_auth = _make_auth(tmp_path)
    yt_auth._write_default(VALID_CHANNEL_ID)
    yt_auth._write_default(None)
    assert yt_auth.get_default_channel_id() is None


def test_set_default_channel_id_ok(tmp_path):
    yt_auth = _make_auth(tmp_path)
    _write_channel_token(yt_auth, VALID_CHANNEL_ID)
    yt_auth._write_default(None)
    yt_auth.set_default_channel_id(VALID_CHANNEL_ID)
    assert yt_auth.get_default_channel_id() == VALID_CHANNEL_ID


def test_set_default_channel_id_missing_raises(tmp_path):
    yt_auth = _make_auth(tmp_path)
    with pytest.raises(AuthError, match="not authorized"):
        yt_auth.set_default_channel_id(VALID_CHANNEL_ID)


# ── Per-channel token storage ─────────────────────────────────────────────────

def test_save_channel_token_creates_file(tmp_path):
    yt_auth = _make_auth(tmp_path)
    mock_creds = MagicMock()
    mock_creds.to_json.return_value = FAKE_CREDS_JSON
    yt_auth._save_channel_token(VALID_CHANNEL_ID, mock_creds)
    assert yt_auth._token_path_for(VALID_CHANNEL_ID).exists()


def test_get_authorized_channel_ids_empty(tmp_path):
    yt_auth = _make_auth(tmp_path)
    assert yt_auth.get_authorized_channel_ids() == []


def test_get_authorized_channel_ids_finds_tokens(tmp_path):
    yt_auth = _make_auth(tmp_path)
    cid1 = "UC" + "a" * 22
    cid2 = "UC" + "b" * 22
    _write_channel_token(yt_auth, cid1)
    _write_channel_token(yt_auth, cid2)
    ids = yt_auth.get_authorized_channel_ids()
    assert set(ids) == {cid1, cid2}
    assert ids == sorted(ids)  # must be sorted


# ── load_credentials ──────────────────────────────────────────────────────────

def test_load_credentials_missing_raises(tmp_path):
    yt_auth = _make_auth(tmp_path)
    with pytest.raises(AuthError, match="No token found"):
        yt_auth.load_credentials(VALID_CHANNEL_ID)


@patch("youtube_mcp.auth.Credentials.from_authorized_user_file")
def test_load_credentials_valid(mock_from_file, tmp_path):
    yt_auth = _make_auth(tmp_path)
    _write_channel_token(yt_auth, VALID_CHANNEL_ID)

    mock_creds = MagicMock()
    mock_creds.valid = True
    mock_creds.expired = False
    mock_from_file.return_value = mock_creds

    result = yt_auth.load_credentials(VALID_CHANNEL_ID)
    assert result is mock_creds


@patch("youtube_mcp.auth.Credentials.from_authorized_user_file")
def test_load_credentials_uses_cache(mock_from_file, tmp_path):
    yt_auth = _make_auth(tmp_path)
    _write_channel_token(yt_auth, VALID_CHANNEL_ID)

    mock_creds = MagicMock()
    mock_creds.valid = True
    mock_creds.expired = False
    mock_from_file.return_value = mock_creds

    yt_auth.load_credentials(VALID_CHANNEL_ID)
    yt_auth.load_credentials(VALID_CHANNEL_ID)

    # from_authorized_user_file should only be called once (cache hit on second call)
    assert mock_from_file.call_count == 1


# ── remove_channel_token ──────────────────────────────────────────────────────

def test_remove_channel_token_ok(tmp_path):
    yt_auth = _make_auth(tmp_path)
    _write_channel_token(yt_auth, VALID_CHANNEL_ID)
    yt_auth.remove_channel_token(VALID_CHANNEL_ID)
    assert not yt_auth._token_path_for(VALID_CHANNEL_ID).exists()


def test_remove_channel_token_missing_raises(tmp_path):
    yt_auth = _make_auth(tmp_path)
    with pytest.raises(AuthError, match="not authorized"):
        yt_auth.remove_channel_token(VALID_CHANNEL_ID)


def test_remove_channel_updates_default_to_next(tmp_path):
    yt_auth = _make_auth(tmp_path)
    cid1 = "UC" + "a" * 22
    cid2 = "UC" + "b" * 22
    _write_channel_token(yt_auth, cid1)
    _write_channel_token(yt_auth, cid2)
    yt_auth._write_default(cid1)

    yt_auth.remove_channel_token(cid1)

    new_default = yt_auth.get_default_channel_id()
    assert new_default == cid2  # first remaining channel becomes default


def test_remove_channel_clears_default_when_last(tmp_path):
    yt_auth = _make_auth(tmp_path)
    _write_channel_token(yt_auth, VALID_CHANNEL_ID)
    yt_auth._write_default(VALID_CHANNEL_ID)

    yt_auth.remove_channel_token(VALID_CHANNEL_ID)

    assert yt_auth.get_default_channel_id() is None


# ── Migration ─────────────────────────────────────────────────────────────────

@patch("youtube_mcp.auth.build")
@patch("youtube_mcp.auth.Credentials.from_authorized_user_file")
def test_migration_creates_tokens_dir(mock_from_file, mock_build, tmp_path):
    """Legacy token.json is migrated to tokens/{channel_id}.json on init."""
    # Create a legacy token.json
    legacy = tmp_path / "token.json"
    legacy.write_text(FAKE_CREDS_JSON)

    # Mock valid credentials
    mock_creds = MagicMock()
    mock_creds.valid = True
    mock_creds.expired = False
    mock_creds.to_json.return_value = FAKE_CREDS_JSON
    mock_from_file.return_value = mock_creds

    # Mock youtube service returning a channel
    mock_yt = MagicMock()
    mock_build.return_value = mock_yt
    mock_yt.channels().list().execute.return_value = {
        "items": [{"id": VALID_CHANNEL_ID}]
    }

    yt_auth = YouTubeAuth(config_dir=tmp_path)

    assert yt_auth.tokens_dir.exists()
    assert yt_auth._token_path_for(VALID_CHANNEL_ID).exists()
    assert yt_auth.get_default_channel_id() == VALID_CHANNEL_ID


@patch("youtube_mcp.auth.build")
@patch("youtube_mcp.auth.Credentials.from_authorized_user_file")
def test_migration_skipped_when_tokens_dir_exists(mock_from_file, mock_build, tmp_path):
    """Migration is skipped if tokens/ already exists."""
    legacy = tmp_path / "token.json"
    legacy.write_text(FAKE_CREDS_JSON)

    # Pre-create tokens dir
    (tmp_path / "tokens").mkdir()

    YouTubeAuth(config_dir=tmp_path)

    # build should NOT have been called (no migration needed)
    mock_build.assert_not_called()


# ── status ────────────────────────────────────────────────────────────────────

def test_status_with_channels(tmp_path):
    yt_auth = _make_auth(tmp_path)
    _write_channel_token(yt_auth, VALID_CHANNEL_ID)
    yt_auth._write_default(VALID_CHANNEL_ID)

    status = yt_auth.status()
    assert status["authenticated"] is True
    assert VALID_CHANNEL_ID in status["channels"]
    assert status["default_channel"] == VALID_CHANNEL_ID
