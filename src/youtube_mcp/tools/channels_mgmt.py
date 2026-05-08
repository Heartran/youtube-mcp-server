"""Multi-channel management tools.

These tools let you add, list, remove, and select YouTube channels whose
analytics tokens are stored on the server. Each channel has its own OAuth
refresh token in tokens/{channel_id}.json.
"""

from youtube_mcp.server import auth, mcp


@mcp.tool()
def add_channel(set_as_default: bool = False) -> dict:
    """Authorize a new YouTube channel via browser OAuth flow.

    Opens a browser on the server machine for Google OAuth consent. After
    the user completes consent, the channel's refresh token is stored in
    tokens/{channel_id}.json and the channel becomes available to all
    analytics tools.

    The new channel is automatically set as the default if it is the first
    one added, or if set_as_default is True.

    Args:
        set_as_default: If True, make this channel the default for analytics
            tools called without an explicit channel_id.

    Returns:
        channel_id, title, and whether the channel was set as default.
    """
    try:
        return auth.run_add_channel_flow(set_as_default=set_as_default)
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
def list_channels() -> dict:
    """List all authorized YouTube channels with their metadata.

    Returns channel IDs, titles, handles, subscriber counts, video counts,
    and which channel is the current default for analytics tools.
    """
    channel_ids = auth.get_authorized_channel_ids()
    default_id = auth.get_default_channel_id()

    if not channel_ids:
        return {"channels": [], "total": 0, "default_channel": default_id}

    # Fetch public metadata for all channels in one batched API call.
    # Any valid token can read public channel info, so we use the default.
    by_id: dict = {}
    try:
        yt = auth.build_youtube_service()
        resp = yt.channels().list(
            part="snippet,statistics",
            id=",".join(channel_ids),
        ).execute()
        by_id = {item["id"]: item for item in resp.get("items", [])}
    except Exception:
        pass  # degrade gracefully: return ids without metadata

    channels = []
    for cid in channel_ids:
        item = by_id.get(cid, {})
        snippet = item.get("snippet", {})
        stats = item.get("statistics", {})
        channels.append({
            "channel_id": cid,
            "title": snippet.get("title", ""),
            "handle": snippet.get("customUrl", ""),
            "subscribers": (
                int(stats["subscriberCount"]) if stats.get("subscriberCount") else None
            ),
            "video_count": (
                int(stats["videoCount"]) if stats.get("videoCount") else None
            ),
            "is_default": cid == default_id,
        })

    return {"channels": channels, "total": len(channels), "default_channel": default_id}


@mcp.tool()
def remove_channel(channel_id: str) -> dict:
    """Remove an authorized YouTube channel and its stored token.

    If the removed channel was the default, the first remaining channel
    becomes the new default. If no channels remain, there is no default.

    Args:
        channel_id: The YouTube channel ID to remove (e.g. UCxxxxxxxxxxxxxxxxxxxxxx).
    """
    try:
        auth.remove_channel_token(channel_id)
        new_default = auth.get_default_channel_id()
        return {
            "removed": channel_id,
            "new_default_channel": new_default,
        }
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
def set_default_channel(channel_id: str) -> dict:
    """Set the default channel used by analytics tools.

    Analytics tools called without an explicit channel_id will use this
    channel. The channel must already be authorized via add_channel.

    Args:
        channel_id: The YouTube channel ID to set as default
            (e.g. UCxxxxxxxxxxxxxxxxxxxxxx).
    """
    try:
        auth.set_default_channel_id(channel_id)
        return {"default_channel": channel_id}
    except Exception as e:
        return {"error": str(e)}
