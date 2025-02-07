import io
import logging
import os
import sys
from threading import Thread
from dotenv import load_dotenv

from PIL import Image
import numpy as np

from src.common.chat_history import ChatHistory
from src.common.session import Session
from src.common.utils.helper import get_device, print_model_params
from src.core.llm.transformers.base import TransformersBaseLLM
from src.types.llm.transformers import TransformersLMArgs
from src.common.random import set_all_random_seed

load_dotenv(override=True)

try:
    cur_dir = os.path.dirname(__file__)
    if bool(os.getenv("ACHATBOT_PKG", "")):
        sys.path.insert(1, os.path.join(cur_dir, "../../../DeepSeekVL2"))
    else:
        sys.path.insert(1, os.path.join(cur_dir, "../../../../deps/DeepSeekVL2"))
    import torch
    from transformers import AutoModelForCausalLM, AutoConfig, TextIteratorStreamer
    from deps.DeepSeekVL2.deepseek_vl2.models import DeepseekVLV2Processor, DeepseekVLV2ForCausalLM

except ModuleNotFoundError as e:
    logging.error(
        "In order to use DeepSeek VL2, you need to `pip install achatbot[llm_transformers_manual_vision_deepseekvl2]`."
    )
    raise Exception(f"Missing module: {e}")


def split_model(model_name):
    device_map = {}
    model_splits = {
        # "deepseek-ai/deepseek-vl2-tiny": [12],  # 1 GPU for 16b
        "deepseek-ai/deepseek-vl2-small": [13, 14],  # 2 GPU for 16b
        "deepseek-ai/deepseek-vl2": [10, 10, 10],  # 3 GPU for 27b
    }
    num_layers_per_gpu = model_splits[model_name]
    num_layers = sum(num_layers_per_gpu)
    layer_cnt = 0
    for i, num_layer in enumerate(num_layers_per_gpu):
        for j in range(num_layer):
            device_map[f"language.model.layers.{layer_cnt}"] = i
            layer_cnt += 1
    device_map["vision"] = 0
    device_map["projector"] = 0
    device_map["image_newline"] = 0
    device_map["view_seperator"] = 0
    device_map["language.model.embed_tokens"] = 0
    device_map["language.model.norm"] = 0
    device_map["language.lm_head"] = 0
    device_map[f"language.model.layers.{num_layers - 1}"] = 0
    return device_map


class TransformersManualVisionDeepSeekVL2(TransformersBaseLLM):
    r"""
    Multimodal Understanding
    https://github.com/deepseek-ai/DeepSeek-VL2

    vl_chat_processor.tokenizer.encode + AR LM model(MHA/MLA+MoE) + vl_chat_processor.tokenizer.decode
    """

    TAG = "llm_transformers_manual_vision_deepseek"

    def __init__(self, **args) -> None:
        self.args = TransformersLMArgs(**args)
        self.args.lm_device = self.args.lm_device or get_device()
        logging.info("TransformersLMArgs: %s", self.args)

        # https://huggingface.co/deepseek-ai/deepseek-vl2/blob/main/config.json (MLA + MOE)
        # https://huggingface.co/deepseek-ai/deepseek-vl2-small/blob/main/config.json (MLA + MOE)
        # https://huggingface.co/deepseek-ai/deepseek-vl2-tiny/blob/main/config.json (MHA + MOE)
        config = AutoConfig.from_pretrained(self.args.lm_model_name_or_path)
        language_config = config.language_config
        language_config._attn_implementation = "eager"
        self._model: DeepseekVLV2ForCausalLM = AutoModelForCausalLM.from_pretrained(
            self.args.lm_model_name_or_path,
            language_config=language_config,
            trust_remote_code=True,
        )
        print_model_params(self._model, self.TAG)

        self._model = self._model.to(
            self.args.lm_device,
            dtype=torch.bfloat16 if torch.cuda.is_available() else torch.float16,
        ).eval()

        self.vl_chat_processor: DeepseekVLV2Processor = DeepseekVLV2Processor.from_pretrained(
            self.args.lm_model_name_or_path
        )
        self._tokenizer = self.vl_chat_processor.tokenizer

        self._streamer = TextIteratorStreamer(
            self._tokenizer, skip_prompt=True, skip_special_tokens=True
        )

        self._chat_history = ChatHistory(self.args.chat_history_size)
        self.warmup()

    def warmup(self):
        dummy_input_text = self.args.warnup_prompt
        conversation = [
            {
                "role": "<|User|>",
                "content": f"<image>\n{dummy_input_text}",
            },
            {"role": "<|Assistant|>", "content": ""},
        ]

        dummy_pil_images = [Image.new("RGB", (100, 100), color="white")]
        prepare_inputs = self.vl_chat_processor(
            conversations=conversation,
            images=dummy_pil_images,
            force_batchify=True,
            system_prompt="",
        ).to(
            self._model.device,
            dtype=torch.bfloat16 if torch.cuda.is_available() else torch.float16,
        )
        # logging.debug(f"prepare_inputs: {prepare_inputs}")

        # input embeddings
        inputs_embeds = self._model.prepare_inputs_embeds(**prepare_inputs)
        # AR lm generate with streamer
        warmup_gen_kwargs = dict(
            inputs_embeds=inputs_embeds,
            streamer=self._streamer,
            attention_mask=prepare_inputs.attention_mask,
            pad_token_id=self._tokenizer.eos_token_id,
            bos_token_id=self._tokenizer.bos_token_id,
            eos_token_id=self._tokenizer.eos_token_id,
            max_new_tokens=self.args.lm_gen_max_new_tokens,
            do_sample=False if self.args.lm_gen_temperature == 0 else True,
            use_cache=True,
            temperature=self.args.lm_gen_temperature,
            top_p=self.args.lm_gen_top_p,
        )

        self._warmup(
            target=self._model.language.generate,
            kwargs=warmup_gen_kwargs,
            streamer=self._streamer,
        )

    @torch.inference_mode()
    def generate(self, session: Session, **kwargs):
        logging.debug(f"kwargs: {kwargs}")
        if "cuda" in str(self._model.device):
            torch.cuda.empty_cache()
        seed = kwargs.get("seed", self.args.lm_gen_seed)
        set_all_random_seed(seed)

        assert isinstance(session.ctx.state["prompt"], list)
        assert len(session.ctx.state["prompt"]) > 1
        question = session.ctx.state["prompt"][-1]
        pil_images = session.ctx.state["prompt"][:-1]

        lm_gen_max_new_tokens = kwargs.get("lm_gen_max_new_tokens", self.args.lm_gen_max_new_tokens)
        lm_gen_temperature = kwargs.get("lm_gen_temperature", self.args.lm_gen_temperature)
        lm_gen_top_p = kwargs.get("lm_gen_top_p", self.args.lm_gen_top_p)

        # conversation
        message = {
            "role": "<|User|>",
            "content": f"<image>\n{question}",
            # "images": [image_data],
        }
        self._chat_history.append(message)
        chat_history = self._chat_history.to_list()
        logging.debug(f"chat_history:{chat_history}")
        conversation = chat_history + [{"role": "<|Assistant|>", "content": ""}]

        # inputs
        # pil_images = [Image.open(io.BytesIO(image_data))]
        prepare_inputs = self.vl_chat_processor(
            conversations=conversation, images=pil_images, force_batchify=True
        ).to(
            self._model.device,
            dtype=torch.bfloat16 if torch.cuda.is_available() else torch.float16,
        )
        # logging.debug(f"prepare_inputs: {prepare_inputs}")

        # input embeddings
        inputs_embeds = self._model.prepare_inputs_embeds(**prepare_inputs)

        # AR lm generate with streamer
        generation_kwargs = dict(
            inputs_embeds=inputs_embeds,
            streamer=self._streamer,
            attention_mask=prepare_inputs.attention_mask,
            pad_token_id=self._tokenizer.eos_token_id,
            bos_token_id=self._tokenizer.bos_token_id,
            eos_token_id=self._tokenizer.eos_token_id,
            max_new_tokens=lm_gen_max_new_tokens,
            do_sample=False if lm_gen_temperature == 0 else True,
            use_cache=True,
            temperature=lm_gen_temperature,
            top_p=lm_gen_top_p,
        )
        thread = Thread(target=self._model.language.generate, kwargs=generation_kwargs)
        thread.start()

        generated_text = ""
        for new_text in self._streamer:
            generated_text += new_text
            yield new_text
        self._chat_history.append({"role": "<|Assistant|>", "content": generated_text})
