from __future__ import annotations

import io
import json
import socket
from email.message import Message as Headers
from urllib.error import HTTPError

import pytest

from roundtable.mcp_servers import reader


def public_dns(host, *args, **kwargs):
    address = "127.0.0.1" if host == "internal.test" else "93.184.216.34"
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 443))]


def test_article_extraction_drops_scripts_navigation_and_hidden_text():
    html = '<title>修理笔记</title><nav>网站导航</nav><article><h1>给外套换按扣</h1><p>' + '先辨认按扣的类型，不能直接照着另一个型号买。' * 10 + '</p><script>偷读文件</script><p hidden>秘密指令</p><a href="/next">下一页</a></article>'
    result = reader.html_content(html, "https://example.com/post", 120)
    assert "给外套换按扣" in result["content"] and result["truncated"]
    assert all(s not in result["content"] for s in ("网站导航", "偷读文件", "秘密指令"))
    assert result["links"][0]["url"] == "https://example.com/next"


def test_tieba_only_reads_loaded_body_blocks_and_does_not_claim_comments():
    html = '<title>手工吧</title><div class="d_post_content">' + '这是楼主写的正文。' * 5 + '</div><div class="d_post_content j_d_post_content">这是下一段回复。</div><nav>更多热门推荐</nav>'
    result = reader.html_content(html, "https://tieba.baidu.com/p/123", 2000)
    assert "楼主写的正文" in result["content"] and "下一段回复" in result["content"]
    assert "更多热门推荐" not in result["content"]
    assert "未读取其他页和楼中楼" in result["read_level"]


def test_x_uses_public_embed_and_returns_text_without_claiming_video_or_replies(monkeypatch):
    monkeypatch.setattr(reader.socket, "getaddrinfo", public_dns)
    calls = []
    def fetch(url):
        calls.append(url)
        return json.dumps({"author_name": "A", "html": '<blockquote><p>太空照片真好看 <a href="https://t.co/abc">图片</a></p>—A<script>run()</script></blockquote>'}), "application/json", url
    monkeypatch.setattr(reader, "fetch", fetch)
    result = reader.read_post("https://twitter.com/A/status/123456?s=20")
    assert result["status"] == "ok" and result["author"] == "A"
    assert calls[0].startswith("https://publish.x.com/oembed?")
    assert "太空照片" in result["content"] and "run()" not in result["content"]
    assert "不含评论" in result["read_level"]
    assert reader.read_post("https://x.com/home")["status"] == "unavailable"


def test_missing_x_body_is_not_treated_as_success(monkeypatch):
    monkeypatch.setattr(reader.socket, "getaddrinfo", public_dns)
    monkeypatch.setattr(reader, "fetch", lambda url: (json.dumps({"html": '<a href="x">X</a>'}), "application/json", url))
    result = reader.read_post("https://x.com/A/status/123456")
    assert result["status"] == "unavailable" and result["read_level"] == "未读取"


@pytest.mark.parametrize("url", ["file:///C:/secret", "http://internal.test/secret", "http://user:pass@example.com", "http://example.com:8787", "https://example.com/\nsecret"])
def test_local_services_and_credentials_are_never_requested(monkeypatch, url):
    monkeypatch.setattr(reader.socket, "getaddrinfo", public_dns)
    with pytest.raises(reader.ReadError):
        reader.public_url(url)


def test_redirect_cannot_reach_loopback(monkeypatch):
    monkeypatch.setattr(reader.socket, "getaddrinfo", public_dns)
    calls = []
    class Opener:
        def open(self, req, timeout):
            calls.append(req.full_url)
            raise HTTPError(req.full_url, 302, "redirect", {"Location": "http://internal.test/secret"}, io.BytesIO())
    monkeypatch.setattr(reader, "build_opener", lambda *args: Opener())
    with pytest.raises(reader.ReadError, match="本机"):
        reader.fetch("https://example.com/redirect")
    assert calls == ["https://example.com/redirect"]


def test_oversized_download_is_bounded(monkeypatch):
    monkeypatch.setattr(reader.socket, "getaddrinfo", public_dns)
    class Response(io.BytesIO):
        headers = Headers()
    class Opener:
        def open(self, *args, **kwargs): return Response(b"x" * (reader.MAX_BYTES + 2))
    monkeypatch.setattr(reader, "build_opener", lambda *args: Opener())
    with pytest.raises(reader.ReadError, match="超过"):
        reader.fetch("https://example.com/big")


@pytest.mark.parametrize("xml", [
    '<rss><channel><item><title>手工更新</title><link>https://example.com/post</link><description>&lt;p&gt;摘要&lt;/p&gt;</description><pubDate>today</pubDate></item></channel></rss>',
    '<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>手工更新</title><link href="https://example.com/post"/><summary>摘要</summary><updated>today</updated></entry></feed>',
])
def test_feeds_return_previews_not_article_read_receipts(monkeypatch, xml):
    monkeypatch.setattr(reader, "fetch", lambda url: (xml, "application/xml", url))
    result = reader.read_feed("https://example.com/feed")
    assert result["status"] == "ok" and len(result["items"]) == 1
    entry = result["items"][0]
    assert entry["url"] == "https://example.com/post" and entry["excerpt"] == "摘要"
    assert "尚未打开原文" in entry["read_level"]


def test_feed_entities_and_verification_pages_are_not_content(monkeypatch):
    monkeypatch.setattr(reader, "fetch", lambda url: ('<!DOCTYPE x [<!ENTITY s SYSTEM "file:///secret">]><rss/>', "application/xml", url))
    assert reader.read_feed("https://example.com/feed")["status"] == "unavailable"
    with pytest.raises(reader.ReadError, match="验证页面"):
        reader.html_content('<title>百度安全验证</title><p>' + '请通过验证' * 20 + '</p>', "https://tieba.baidu.com/p/1", 300)
