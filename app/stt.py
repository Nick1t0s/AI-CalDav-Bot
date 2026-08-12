"""Распознавание голосовых сообщений (speech-to-text) через OpenAI Whisper.

Синхронные функции — вызываются из aiogram-обработчиков через asyncio.to_thread,
чтобы не блокировать event loop.
"""
from __future__ import annotations

import io
import logging
from typing import BinaryIO, Optional

from app import config

logger = logging.getLogger(__name__)


class STTError(Exception):
    """Ошибка распознавания речи."""


_client = None


def _get_client():
    global _client
    if _client is None:
        from openai import OpenAI

        _client = OpenAI(
            api_key=config.STT_API_KEY or config.OPENAI_API_KEY,
            base_url=config.STT_BASE_URL or None,
            timeout=config.REQUESTS_TIMEOUT_SECONDS,
        )
    return _client


def transcribe_audio(
    data: bytes,
    mime_type: Optional[str] = None,
    filename: Optional[str] = None,
) -> str:
    """Распознать аудио (байты голосового сообщения) в текст."""
    if not config.STT_API_KEY and not config.OPENAI_API_KEY:
        raise STTError("не настроен STT_API_KEY / OPENAI_API_KEY")
    suffix = "ogg" if mime_type and "ogg" in mime_type else "m4a"
    file_obj: BinaryIO = io.BytesIO(data)
    file_obj.name = filename or f"voice.{suffix}"
    try:
        result = _get_client().audio.transcriptions.create(
            model=config.STT_MODEL,
            file=file_obj,
            language=config.STT_LANGUAGE or None,
        )
    except Exception as exc:
        logger.exception("Ошибка распознавания речи")
        raise STTError(f"ошибка распознавания: {exc}") from exc
    text = (result.text or "").strip()
    if not text:
        raise STTError("пустой результат распознавания")
    logger.debug("STT: %d байт → %r", len(data), text)
    return text