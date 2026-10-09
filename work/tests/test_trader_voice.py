import unittest
from unittest.mock import patch

import trader_voice
from telegram_bridge import voice_can_prepare_draft


class VoiceTests(unittest.TestCase):
    def test_rejects_oversized_voice_before_download(self):
        def api(*args):
            self.fail('Telegram must not be contacted')
        with self.assertRaises(trader_voice.VoiceError):
            trader_voice.transcribe('secret', {'file_id': 'abc', 'duration': 91}, api)

    def test_rejects_unsafe_telegram_path(self):
        with self.assertRaises(trader_voice.VoiceError):
            trader_voice.transcribe('secret', {'file_id': 'abc', 'duration': 2},
                                    lambda *args: {'file_path': '../unsafe'})

    def test_audio_never_confirms_an_order(self):
        self.assertTrue(voice_can_prepare_draft('Открыть лонг ADA, цена 0.25'))
        self.assertTrue(voice_can_prepare_draft('Закрыть лонг ADA полностью по рынку'))
        self.assertFalse(voice_can_prepare_draft('/confirm 12345'))
        self.assertFalse(voice_can_prepare_draft('Подтверждаю 12345'))


if __name__ == '__main__':
    unittest.main()
