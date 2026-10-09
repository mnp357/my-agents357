"""Local transcription of a paired owner's Telegram voice note."""

import io
import re
import urllib.error
import urllib.request
from pathlib import Path

MAX_BYTES = 5 * 1024 * 1024
MAX_SECONDS = 90
MODEL_PATH = Path('/opt/my-agents/models/faster-whisper-small')
_model = None


class VoiceError(Exception):
    pass


def transcribe(token, voice, telegram_api):
    global _model
    if not isinstance(voice, dict) or not voice.get('file_id'):
        raise VoiceError('Не удалось прочитать голосовое сообщение. Запишите его ещё раз.')
    try:
        if int(voice.get('duration') or 0) > MAX_SECONDS or int(voice.get('file_size') or 0) > MAX_BYTES:
            raise VoiceError('Голосовое сообщение слишком длинное. Отправьте запись до 90 секунд.')
        metadata = telegram_api(token, 'getFile', {'file_id': voice['file_id']})
        path = metadata.get('file_path') or ''
        if (not re.fullmatch(r'[A-Za-z0-9_./-]+', path) or
                '..' in path or path.startswith('/') or
                int(metadata.get('file_size') or 0) > MAX_BYTES):
            raise VoiceError('Не удалось безопасно загрузить голосовое сообщение.')
        # The Telegram token is necessarily in the Bot API URL. Never log it.
        url = 'https://api.telegram.org/file/bot' + token + '/' + path
        with urllib.request.urlopen(url, timeout=30) as response:
            audio = response.read(MAX_BYTES + 1)
        if len(audio) > MAX_BYTES:
            raise VoiceError('Голосовое сообщение слишком большое. Запишите более короткое.')
        if not audio:
            raise VoiceError('Голосовое сообщение пустое. Запишите его ещё раз.')
        if not MODEL_PATH.is_dir():
            raise VoiceError('Распознавание голоса пока недоступно. Отправьте поручение текстом.')
        if _model is None:
            from faster_whisper import WhisperModel
            _model = WhisperModel(str(MODEL_PATH), device='cpu', compute_type='int8',
                                  cpu_threads=4)
        segments, _ = _model.transcribe(io.BytesIO(audio), language='ru', beam_size=5,
                                        vad_filter=True, condition_on_previous_text=False)
        result = ' '.join(segment.text.strip() for segment in segments).strip()
        if not result:
            raise VoiceError('Не удалось разобрать речь. Попробуйте записать сообщение ещё раз.')
        return result[:1500]
    except VoiceError:
        raise
    except Exception:
        # Never expose an exception: download errors may contain the bot token URL.
        raise VoiceError('Не удалось распознать голосовое сообщение. Попробуйте ещё раз или напишите текстом.') from None
