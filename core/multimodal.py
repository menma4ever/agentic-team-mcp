"""Multimodal Processing Engine for Telegram Bridge.
Supports voice notes, audio recordings, images/photos, videos, and documents/files.
Integrates with Gemini Multimodal REST API, Groq/OpenAI Whisper transcription,
FFmpeg audio conversion, and direct text document parsing.
"""
import base64
import json
import logging
import os
import shutil
import subprocess
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from core.config import DATA_DIR, settings

logger = logging.getLogger("Multimodal")


def download_telegram_file(bot_token: str, file_id: str, dest_dir: Path, preferred_name: Optional[str] = None) -> Optional[Path]:
    """Downloads a file from Telegram servers using file_id."""
    if not bot_token or bot_token in ("YOUR_TELEGRAM_BOT_TOKEN_HERE", "MOCK_TOKEN", ""):
        logger.warning("download_telegram_file skipped: invalid or mock bot token.")
        return None

    try:
        get_file_url = f"https://api.telegram.org/bot{bot_token}/getFile?file_id={file_id}"
        req = urllib.request.Request(get_file_url, headers={"User-Agent": "AgenticTeamBridge/1.0"})
        with urllib.request.urlopen(req, timeout=12) as resp:
            data = json.loads(resp.read().decode("utf-8"))

        if not data.get("ok") or not data.get("result", {}).get("file_path"):
            logger.error(f"Telegram getFile failed: {data}")
            return None

        file_path_on_server = data["result"]["file_path"]
        download_url = f"https://api.telegram.org/file/bot{bot_token}/{file_path_on_server}"

        dest_dir.mkdir(parents=True, exist_ok=True)
        if preferred_name:
            clean_name = "".join(c for c in preferred_name if c.isalnum() or c in "._-")
            target_path = dest_dir / clean_name
        else:
            ext = Path(file_path_on_server).suffix or ""
            target_path = dest_dir / f"tg_{int(time.time())}_{file_id[:8]}{ext}"

        req_dl = urllib.request.Request(download_url, headers={"User-Agent": "AgenticTeamBridge/1.0"})
        with urllib.request.urlopen(req_dl, timeout=60) as dl_resp:
            file_bytes = dl_resp.read()

        target_path.write_bytes(file_bytes)
        logger.info(f"Downloaded Telegram media: {target_path} ({len(file_bytes)} bytes)")
        return target_path
    except Exception as e:
        logger.error(f"Failed to download file_id {file_id}: {e}")
        return None


def convert_audio_to_wav(audio_path: Path) -> Path:
    """Converts audio/voice (e.g. .oga, .ogg, .mp3, .m4a) to 16kHz mono WAV using ffmpeg."""
    if audio_path.suffix.lower() == ".wav" and audio_path.is_file():
        return audio_path

    ffmpeg_exe = shutil.which("ffmpeg")
    if not ffmpeg_exe:
        return audio_path

    wav_path = audio_path.with_suffix(".wav")
    cmd = [
        ffmpeg_exe,
        "-y",
        "-i", str(audio_path),
        "-ar", "16000",
        "-ac", "1",
        "-c:a", "pcm_s16le",
        str(wav_path)
    ]
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    try:
        subprocess.run(cmd, capture_output=True, check=True, timeout=20, creationflags=flags)
        if wav_path.is_file() and wav_path.stat().st_size > 0:
            return wav_path
    except Exception as e:
        logger.warning(f"ffmpeg audio conversion failed for {audio_path}: {e}")
    return audio_path


def transcribe_audio(audio_path: Path, mime_type: str = "audio/ogg") -> Optional[str]:
    """Transcribes audio using local Whisper, Groq Whisper, OpenAI Whisper, or Gemini Multimodal."""
    # 0. Check Local Whisper Engine (100% offline, zero-token, CPU-optimized)
    try:
        import whisper
        wav_path = convert_audio_to_wav(audio_path)
        model = whisper.load_model("base")
        res = model.transcribe(str(wav_path))
        text = (res.get("text") or "").strip()
        if text:
            logger.info(f"Local Whisper transcribed: {text[:80]}...")
            return text
    except Exception as e:
        logger.debug(f"Local Whisper transcription fallback: {e}")

    # 1. Check Groq Whisper (fastest, ultra-accurate)
    groq_key = settings.get_api_key("groq")
    if groq_key:
        try:
            wav_path = convert_audio_to_wav(audio_path)
            with open(wav_path, "rb") as f:
                audio_data = f.read()

            boundary = "----WebKitFormBoundary" + str(int(time.time()))
            body = (
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="model"\r\n\r\n'
                f"whisper-large-v3\r\n"
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="file"; filename="{wav_path.name}"\r\n'
                f"Content-Type: audio/wav\r\n\r\n"
            ).encode("utf-8") + audio_data + f"\r\n--{boundary}--\r\n".encode("utf-8")

            req = urllib.request.Request(
                "https://api.groq.com/openai/v1/audio/transcriptions",
                data=body,
                headers={
                    "Authorization": f"Bearer {groq_key}",
                    "Content-Type": f"multipart/form-data; boundary={boundary}",
                    "User-Agent": "AgenticTeamBridge/1.0"
                }
            )
            with urllib.request.urlopen(req, timeout=25) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                text = data.get("text", "").strip()
                if text:
                    logger.info(f"Groq Whisper transcribed: {text[:80]}...")
                    return text
        except Exception as e:
            logger.warning(f"Groq Whisper transcription error: {e}")

    # 2. Check OpenAI Whisper
    openai_key = settings.get_api_key("openai")
    if openai_key:
        try:
            wav_path = convert_audio_to_wav(audio_path)
            with open(wav_path, "rb") as f:
                audio_data = f.read()

            boundary = "----WebKitFormBoundary" + str(int(time.time()))
            body = (
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="model"\r\n\r\n'
                f"whisper-1\r\n"
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="file"; filename="{wav_path.name}"\r\n'
                f"Content-Type: audio/wav\r\n\r\n"
            ).encode("utf-8") + audio_data + f"\r\n--{boundary}--\r\n".encode("utf-8")

            req = urllib.request.Request(
                "https://api.openai.com/v1/audio/transcriptions",
                data=body,
                headers={
                    "Authorization": f"Bearer {openai_key}",
                    "Content-Type": f"multipart/form-data; boundary={boundary}",
                    "User-Agent": "AgenticTeamBridge/1.0"
                }
            )
            with urllib.request.urlopen(req, timeout=25) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                text = data.get("text", "").strip()
                if text:
                    logger.info(f"OpenAI Whisper transcribed: {text[:80]}...")
                    return text
        except Exception as e:
            logger.warning(f"OpenAI Whisper transcription error: {e}")

    # 3. Check Gemini Multimodal Transcription
    gemini_key = settings.get_api_key("gemini")
    if gemini_key:
        try:
            text = call_gemini_multimodal(
                prompt="Transcribe the spoken audio verbatim in its original language without any conversational commentary or preamble. Output only the transcript.",
                media_path=audio_path,
                mime_type=mime_type,
                api_key=gemini_key
            )
            if text:
                logger.info(f"Gemini multimodal transcribed: {text[:80]}...")
                return text
        except Exception as e:
            logger.warning(f"Gemini transcription error: {e}")

    return None


def call_gemini_multimodal(
    prompt: str,
    media_path: Path,
    mime_type: str,
    api_key: str,
    model: str = "gemini-2.5-flash",
    system_instruction: Optional[str] = None
) -> Optional[str]:
    """Calls the Google Gemini REST API directly with binary media payload."""
    try:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"
        media_bytes = media_path.read_bytes()
        b64_data = base64.b64encode(media_bytes).decode("utf-8")

        parts = [
            {"text": prompt},
            {
                "inline_data": {
                    "mime_type": mime_type,
                    "data": b64_data
                }
            }
        ]

        payload: Dict[str, Any] = {
            "contents": [
                {
                    "role": "user",
                    "parts": parts
                }
            ]
        }

        if system_instruction:
            payload["system_instruction"] = {
                "parts": [{"text": system_instruction}]
            }

        data_bytes = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=data_bytes,
            headers={
                "Content-Type": "application/json",
                "User-Agent": "AgenticTeamBridge/1.0"
            }
        )
        with urllib.request.urlopen(req, timeout=45) as resp:
            res = json.loads(resp.read().decode("utf-8"))

        candidates = res.get("candidates", [])
        if candidates:
            parts = candidates[0].get("content", {}).get("parts", [])
            if parts and "text" in parts[0]:
                return parts[0]["text"].strip()
    except Exception as e:
        logger.error(f"Gemini multimodal API call failed: {e}")
    return None


def is_text_document(file_path: Path) -> bool:
    """Checks if a file extension represents a readable code or text document."""
    ext = file_path.suffix.lower()
    return ext in {
        ".py", ".json", ".csv", ".txt", ".md", ".yaml", ".yml", ".sh",
        ".cmd", ".ps1", ".log", ".xml", ".html", ".js", ".ts", ".rs",
        ".toml", ".ini", ".sql", ".c", ".cpp", ".h", ".diff", ".patch"
    }


def read_text_document(file_path: Path, max_chars: int = 40_000) -> Optional[str]:
    """Reads content from a text/code file safely."""
    for enc in ("utf-8", "utf-8-sig", "latin-1", "cp1252"):
        try:
            content = file_path.read_text(encoding=enc)
            if len(content) > max_chars:
                content = content[:max_chars] + f"\n\n... [Truncated: {len(content) - max_chars} characters omitted] ..."
            return content
        except Exception:
            continue
    return None
