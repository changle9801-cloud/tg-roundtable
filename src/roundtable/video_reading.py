"""Read actual video-tool receipts without treating page metadata as a viewing."""

import json
from urllib.parse import urlparse


def is_video_url(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return any(host == domain or host.endswith("." + domain)
               for domain in ("youtube.com", "youtu.be", "bilibili.com", "b23.tv"))


def video_receipt(result, url: str) -> dict:
    if not getattr(result, "isError", False):
        for block in result.content:
            if block.type != "text":
                continue
            try:
                body = json.loads(block.text)
            except (ValueError, TypeError):
                continue
            if not isinstance(body, dict) or body.get("status") != "ok":
                continue
            content = body.get("content")
            if isinstance(content, str) and content.strip() and body.get("read_level"):
                return {"status": "ok", "url": url, "title": str(body.get("title") or "视频"),
                        "content": content, "read_level": str(body["read_level"])}
    return {"status": "unavailable", "reason": "视频工具没有取得可阅读的语音内容；不把标题、简介当作看过视频"}
