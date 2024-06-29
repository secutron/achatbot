import os
import logging
import asyncio

import unittest
import pyaudio

from src.modules.speech.tts.g_tts import GTTS
from src.common.factory import EngineFactory, EngineClass
from src.common.logger import Logger
from src.common.session import Session
from src.common.types import SessionCtx, MODELS_DIR, RECORDS_DIR
import src.modules.speech

r"""
python -m unittest test.modules.speech.tts.test_gtts.TestGTTS.test_synthesize
python -m unittest test.modules.speech.tts.test_gtts.TestGTTS.test_get_voices
"""


class TestGTTS(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tts_tag = os.getenv('TTS_TAG', "tts_g")
        cls.tts_text = os.getenv(
            'TTS_TEXT',
            "你好，我是机器人, hello, test.modules.speech.tts.test_gtts.TestGTTS.test_synthesize")
        Logger.init(logging.DEBUG, is_file=False)

    @classmethod
    def tearDownClass(cls):
        pass

    def setUp(self):
        kwargs = {}
        kwargs["language"] = "zh-CN"
        kwargs["speed_increase"] = 1.5
        self.tts: GTTS = EngineFactory.get_engine_by_tag(
            EngineClass, self.tts_tag, **kwargs)
        self.session = Session(**SessionCtx("test_tts_client_id").__dict__)

        stream_info = self.tts.get_stream_info()
        self.pyaudio_instance = pyaudio.PyAudio()
        self.audio_stream = self.pyaudio_instance.open(
            format=stream_info["format_"],
            channels=stream_info["channels"],
            rate=stream_info["rate"],
            output_device_index=None,
            output=True)

    def tearDown(self):
        self.audio_stream.stop_stream()
        self.audio_stream.close()
        self.pyaudio_instance.terminate()

    def test_get_voices(self):
        voices = self.tts.get_voices()
        self.assertGreater(len(voices), 0)
        print(voices)

    def test_synthesize(self):
        self.session.ctx.state["tts_text"] = self.tts_text
        print(self.session.ctx)
        iter = self.tts.synthesize_sync(self.session)
        sub_chunk_size = 1024
        for i, chunk in enumerate(iter):
            print(f"get {i} chunk {len(chunk)}")
            self.assertGreaterEqual(len(chunk), 0)
            if len(chunk) / sub_chunk_size < 100:
                self.audio_stream.write(chunk)
                continue
            for i in range(0, len(chunk), sub_chunk_size):
                sub_chunk = chunk[i:i + sub_chunk_size]
                self.audio_stream.write(sub_chunk)
