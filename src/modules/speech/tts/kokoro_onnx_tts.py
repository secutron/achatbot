import logging
import os
import sys
from typing import AsyncGenerator

import numpy as np

from src.common.types import MODELS_DIR, PYAUDIO_PAFLOAT32
from src.common.session import Session
from src.common.interface import ITts
from src.modules.speech.tts.base import BaseTTS
from src.types.speech.tts.kokoro import KokoroOnnxTTSArgs

"""
brew install espeak-ng
python -m demo.tts_kokoro export_pytorch_voices_to_json
"""
try:
    from kokoro_onnx import Kokoro
except ModuleNotFoundError as e:
    logging.error(
        "In order to use kokoro-tts with onnx, you need to `pip install achatbot[tts_onnx_kokoro]`."
    )
    raise Exception(f"Missing module: {e}")


class KokoroOnnxTTS(BaseTTS, ITts):
    TAG = "tts_onnx_kokoro"

    @classmethod
    def get_args(cls, **kwargs) -> dict:
        return {**KokoroOnnxTTSArgs().__dict__, **kwargs}

    def __init__(self, **args) -> None:
        self.args = KokoroOnnxTTSArgs(**args)
        logging.debug(f"{KokoroOnnxTTS.TAG} args: {self.args}")

        self.kokoro = Kokoro(
            self.args.model_struct_stats_ckpt,
            self.args.voices_file_path,
            espeak_ng_lib_path=self.args.espeak_ng_lib_path,
            espeak_ng_data_path=self.args.espeak_ng_data_path,
        )

    def get_voices(self) -> list[str]:
        return self.kokoro.get_voices()

    def set_voice(self, voice: str):
        if voice in self.get_voices():
            self.args.voice = voice

    def get_stream_info(self) -> dict:
        return {
            # "format": PYAUDIO_PAINT16,
            "format": PYAUDIO_PAFLOAT32,
            "channels": 1,
            "rate": 24000,  # target_sample_rate
            "sample_width": 2,
            # "np_dtype": np.int16,
            "np_dtype": np.float32,
        }

    async def _inference(self, session: Session, text: str) -> AsyncGenerator[bytes, None]:
        if self.args.tts_stream is True:
            for audio_samples, _ in self.kokoro.create_stream(
                text, voice=self.args.voice, speed=self.args.speed, lang=self.args.language
            ):
                yield np.frombuffer(audio_samples, dtype=np.float32).tobytes()
        else:
            audio_samples, _ = self.kokoro.create(
                text, voice=self.args.voice, speed=self.args.speed, lang=self.args.language
            )
            yield np.frombuffer(audio_samples, dtype=np.float32).tobytes()
