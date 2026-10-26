# /// script
# requires-python = ">=3.10"
# dependencies = ["mcp>=2.3", "yt-dlp", "sherpa-onnx==1.13.8", "numpy"]
# ///
"""看视频: a small MCP server so members can search and "watch" online videos.

Run it with ``uv run --script video.py`` (uv builds its own small environment from the
header above). Two tools:

- search_videos: find videos on YouTube or Bilibili by keyword (yt-dlp, no download).
- watch_video: download a low-resolution copy, transcribe the speech with SenseVoice
  (sherpa-onnx, local, offline) and grab a few frames; returns the transcript and pictures.

Settings come from environment variables:
    SENSEVOICE_DIR     folder with model.int8.onnx (or model.onnx) and tokens.txt
                       (default: ~/.blurt/models/sensevoice, the model Blurt downloaded)
    SILERO_VAD         silero_vad.onnx used to cut the audio into speech segments
                       (default: ~/.blurt/models/silero_vad.onnx)
    FFMPEG             ffmpeg executable (default: ffmpeg on PATH)
    VIDEO_MAX_MINUTES  longest video it will watch (default 30)
    VIDEO_PROXY        proxy for yt-dlp, e.g. http://127.0.0.1:7890 (default: none / system env)
    VIDEO_COOKIES      a cookies.txt (Netscape format) for sites that want a login, e.g. Bilibili search

Nothing is kept: downloads go to a temporary folder that is deleted after each video.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from mcp.server.mcpserver import Image, MCPServer

HOME = Path.home()
MODEL_DIR = Path(os.environ.get("SENSEVOICE_DIR") or HOME / ".blurt" / "models" / "sensevoice")
VAD_MODEL = Path(os.environ.get("SILERO_VAD") or HOME / ".blurt" / "models" / "silero_vad.onnx")
FFMPEG = os.environ.get("FFMPEG") or shutil.which("ffmpeg") or "ffmpeg"
MAX_MINUTES = float(os.environ.get("VIDEO_MAX_MINUTES") or 30)
PROXY = os.environ.get("VIDEO_PROXY") or ""
COOKIES = os.environ.get("VIDEO_COOKIES") or ""
MAX_TRANSCRIPT = 8000  # characters handed back; the rest is cut
SAMPLE_RATE = 16000

server = MCPServer("看视频")
_recognizer = None
_vad_config = None


def _ydl_options(**extra) -> dict:
    opts = {"quiet": True, "no_warnings": True, "noprogress": True, "noplaylist": True, "ffmpeg_location": FFMPEG}
    if PROXY:
        opts["proxy"] = PROXY
    if COOKIES:
        opts["cookiefile"] = COOKIES
    opts.update(extra)
    return opts


def _problem(e: Exception) -> str:
    """A short reason for the model, instead of a traceback."""
    text = str(e).replace("ERROR: ", "").strip()
    if "412" in text and "bili" in text.lower():
        return "B 站拒绝了没登录的搜索（HTTP 412）。可以换 YouTube 搜，或者直接给 B 站视频链接。"
    return text.splitlines()[0][:300] if text else type(e).__name__


@server.tool()
def search_videos(query: str, site: str = "youtube", limit: int = 5) -> str:
    """Search videos by keyword. site is "youtube" or "bilibili". Returns titles, links, channels and lengths.

    Use it to find something worth watching, then call watch_video with a link.
    """
    import yt_dlp

    limit = max(1, min(int(limit), 10))
    prefix = "bilisearch" if site.lower().startswith("bili") else "ytsearch"
    try:
        with yt_dlp.YoutubeDL(_ydl_options(extract_flat=True)) as ydl:
            info = ydl.extract_info(f"{prefix}{limit}:{query}", download=False)
    except Exception as e:  # noqa: BLE001 - network, site changes, logins
        return f"搜索失败：{_problem(e)}"
    lines = []
    for e in (info or {}).get("entries") or []:
        url = e.get("url") or e.get("webpage_url") or ""
        if url and not url.startswith("http") and prefix == "ytsearch":
            url = f"https://www.youtube.com/watch?v={e.get('id')}"
        minutes = f"{(e.get('duration') or 0) / 60:.0f} 分钟" if e.get("duration") else "时长未知"
        views = f"，{e['view_count']} 次观看" if e.get("view_count") else ""
        title = e.get("title") or "（标题没拿到，看了才知道）"
        lines.append(f"- {title}（{e.get('channel') or e.get('uploader') or '未知作者'}，{minutes}{views}）\n  {url}")
    return "\n".join(lines) or "没找到相关视频。"


@server.tool()
def watch_video(url: str, frames: int = 4) -> list:
    """Watch a video: returns its title, channel, description, the transcript of what is said
    (speech recognised locally) and a few frames from across the video.

    Only videos up to the configured length (30 minutes by default) are watched.
    """
    frames = max(0, min(int(frames), 6))
    work = Path(tempfile.mkdtemp(prefix="watch-"))
    try:
        return _watch(url, frames, work)
    except Exception as e:  # noqa: BLE001 - network, site changes, missing model
        return [f"没看成：{_problem(e)}"]
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _watch(url: str, frames: int, work: Path) -> list:
    import yt_dlp

    opts = _ydl_options(
        format="bv*[height<=480]+ba/b[height<=480]/b",
        outtmpl=str(work / "video.%(ext)s"),
        merge_output_format="mp4",
    )
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
        duration = float(info.get("duration") or 0)
        if duration and duration > MAX_MINUTES * 60:
            return [f"这个视频有 {duration / 60:.0f} 分钟，超过了 {MAX_MINUTES:.0f} 分钟的上限，没有看。"
                    f"标题：{info.get('title')}"]
        ydl.download([url])
    video = next((p for p in work.iterdir() if p.name.startswith("video.")), None)
    if video is None:
        return ["视频没能下载下来。"]

    transcript = _transcribe(video)
    if len(transcript) > MAX_TRANSCRIPT:
        transcript = transcript[:MAX_TRANSCRIPT] + "……（后面太长，省略了）"
    description = (info.get("description") or "").strip()
    header = [
        f"标题：{info.get('title')}",
        f"作者：{info.get('channel') or info.get('uploader') or '未知'}",
        f"时长：{duration / 60:.1f} 分钟" if duration else "",
        f"简介：{description[:400]}" if description else "",
        "说话内容（本地语音识别，可能有错字）：",
        transcript or "（没有识别到说话，可能是纯音乐或没有人声）",
    ]
    out: list = ["\n".join(line for line in header if line)]
    out += [Image(data=jpg, format="jpeg") for jpg in _frames(video, duration, frames)]
    return out


def _audio(video: Path):
    import numpy as np

    raw = subprocess.run(
        [FFMPEG, "-nostdin", "-loglevel", "error", "-i", str(video), "-ac", "1", "-ar", str(SAMPLE_RATE), "-f", "s16le", "-"],
        capture_output=True, check=True,
    ).stdout
    return np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0


def _models():
    global _recognizer, _vad_config
    if _recognizer is None:
        import sherpa_onnx

        model = next((MODEL_DIR / n for n in ("model.int8.onnx", "model.onnx") if (MODEL_DIR / n).exists()), None)
        if model is None or not (MODEL_DIR / "tokens.txt").exists():
            raise RuntimeError(f"在 {MODEL_DIR} 里没找到 SenseVoice 模型（model.int8.onnx 和 tokens.txt）")
        _recognizer = sherpa_onnx.OfflineRecognizer.from_sense_voice(
            model=str(model), tokens=str(MODEL_DIR / "tokens.txt"), num_threads=4, use_itn=True, language="auto",
        )
        _vad_config = sherpa_onnx.VadModelConfig()
        _vad_config.silero_vad.model = str(VAD_MODEL)
        _vad_config.silero_vad.min_silence_duration = 0.3
        _vad_config.sample_rate = SAMPLE_RATE
    return _recognizer, _vad_config


def _transcribe(video: Path) -> str:
    import sherpa_onnx

    recognizer, vad_config = _models()
    samples = _audio(video)
    if not len(samples):
        return ""
    segments = []
    if VAD_MODEL.exists():
        vad = sherpa_onnx.VoiceActivityDetector(vad_config, buffer_size_in_seconds=60)
        window = vad_config.silero_vad.window_size
        for start in range(0, len(samples), window):
            vad.accept_waveform(samples[start:start + window])
            while not vad.empty():
                segments.append(vad.front.samples)
                vad.pop()
        vad.flush()
        while not vad.empty():
            segments.append(vad.front.samples)
            vad.pop()
    else:  # no VAD model: plain 20-second slices
        step = SAMPLE_RATE * 20
        segments = [samples[i:i + step] for i in range(0, len(samples), step)]
    texts = []
    for i in range(0, len(segments), 16):  # decode in small batches
        streams = []
        for seg in segments[i:i + 16]:
            s = recognizer.create_stream()
            s.accept_waveform(SAMPLE_RATE, seg)
            streams.append(s)
        recognizer.decode_streams(streams)
        texts += [s.result.text.strip() for s in streams]
    return "\n".join(t for t in texts if t)


def _frames(video: Path, duration: float, count: int) -> list[bytes]:
    if count < 1 or duration <= 0:
        return []
    out = []
    for i in range(count):
        at = duration * (0.08 + 0.84 * i / max(count - 1, 1))  # spread out, skipping intro and outro
        result = subprocess.run(
            [FFMPEG, "-nostdin", "-loglevel", "error", "-ss", f"{at:.1f}", "-i", str(video),
             "-frames:v", "1", "-vf", "scale=640:-2", "-q:v", "5", "-f", "image2", "-c:v", "mjpeg", "-"],
            capture_output=True,
        )
        if result.returncode == 0 and result.stdout:
            out.append(result.stdout)
    return out


if __name__ == "__main__":
    server.run()
