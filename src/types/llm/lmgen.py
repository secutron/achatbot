from dataclasses import dataclass, field

from src.common.types import CHANNELS, RATE


@dataclass
class LMGenArgs:
    """moshi lm generation defualt arguments"""
    use_sampling: bool = True
    temp: float = 0.8
    temp_text: float = 0.7
    top_k: int = 250
    top_k_text: int = 25
    check: bool = False


@dataclass
class GLMInferenceArgs:
    """GLM inference(generation) defualt arguments"""
    temperature: float = 0.2
    top_p: float = 0.8
    max_new_token: int = 2000


class GLMVoiceArgs:
    """GLM Voice defualt arguments"""
    audio_sample_rate: int = RATE
    audio_channels: int = CHANNELS


class GLMVoiceInArgs(GLMVoiceArgs):
    """GLM Voice In defualt arguments"""


class GLMVoiceOutArgs(GLMVoiceArgs):
    """GLM Voice Out defualt arguments"""
