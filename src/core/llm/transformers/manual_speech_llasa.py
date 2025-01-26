import logging
from threading import Thread
from queue import Queue


try:
    import torch
    from transformers import AutoModel, AutoTokenizer, AutoModelForCausalLM
    from transformers.generation.streamers import BaseStreamer
except ModuleNotFoundError as e:
    logging.error(f"Exception: {e}")
    logging.error(
        "In order to use TTS llasa, you need to `pip install achatbot[llm_transformers_manual_speech_llasa]`,"
    )
    raise Exception(f"Missing module: {e}")


from src.common.session import Session
from src.types.llm.transformers import TransformersLMArgs
from .base import TransformersBaseLLM


def ids_to_speech_tokens(speech_ids):
    speech_tokens_str = []
    for speech_id in speech_ids:
        speech_tokens_str.append(f"<|s_{speech_id}|>")
    return speech_tokens_str


def extract_speech_ids(speech_tokens_str):
    speech_ids = []
    for token_str in speech_tokens_str:
        if token_str.startswith("<|s_") and token_str.endswith("|>"):
            num_str = token_str[4:-2]

            num = int(num_str)
            speech_ids.append(num)
        else:
            print(f"Unexpected token: {token_str}")
    return speech_ids


class TokenStreamer(BaseStreamer):
    def __init__(self, skip_prompt: bool = False, timeout=None):
        self.skip_prompt = skip_prompt

        # variables used in the streaming process
        self.token_queue = Queue()
        self.stop_signal = None
        self.next_tokens_are_prompt = True
        self.timeout = timeout

    def put(self, value):
        if len(value.shape) > 1 and value.shape[0] > 1:
            raise ValueError("TextStreamer only supports batch size 1")
        elif len(value.shape) > 1:
            value = value[0]

        if self.skip_prompt and self.next_tokens_are_prompt:
            self.next_tokens_are_prompt = False
            return

        for token in value.tolist():
            self.token_queue.put(token)

    def end(self):
        self.token_queue.put(self.stop_signal)

    def __iter__(self):
        return self

    def __next__(self):
        value = self.token_queue.get(timeout=self.timeout)
        if value == self.stop_signal:
            raise StopIteration()
        else:
            return value


class TransformersManualSpeechLlasa(TransformersBaseLLM):
    """
    TTS: text + ref audio -> llama2 -> vq code tokens
    """

    TAG = "llm_transformers_manual_speech_llasa"
    DEFAULT_SYS_PROMPT = ""

    def __init__(self, **args):
        self.args = TransformersLMArgs(**args)
        logging.info("TransformersLMArgs: %s", self.args)
        self._model = AutoModelForCausalLM.from_pretrained(self.args.lm_model_name_or_path)
        self._model.eval().to(self.args.lm_device)
        self._tokenizer = AutoTokenizer.from_pretrained(self.args.lm_model_name_or_path)
        self._streamer = TokenStreamer(skip_prompt=True)

        self.warmup()

    def warmup(self):
        if self.args.warnup_steps <= 0:
            logging.info("no warmup!")
            return

        formatted_text = (
            f"<|TEXT_UNDERSTANDING_START|>{self.args.warnup_prompt}<|TEXT_UNDERSTANDING_END|>"
        )

        # Tokenize the text
        chat = [
            {"role": "user", "content": "Convert the text to speech:" + formatted_text},
            {"role": "assistant", "content": "<|SPEECH_GENERATION_START|>"},
        ]

        input_ids = self.tokenizer.apply_chat_template(
            chat, tokenize=True, return_tensors="pt", continue_final_message=True
        )
        input_ids = input_ids.to("cuda")
        speech_end_id = self.tokenizer.convert_tokens_to_ids("<|SPEECH_GENERATION_END|>")

        warmup_gen_kwargs = dict(
            input_ids=input_ids,
            eos_token_id=speech_end_id,
            streamer=self.streamer,
            min_new_tokens=self.args.lm_gen_min_new_tokens,
            max_new_tokens=self.args.lm_gen_max_new_tokens,
            top_k=self.args.lm_gen_top_k,
            top_p=self.args.lm_gen_top_p,
            do_sample=self.args.lm_gen_do_sample,
            temperature=self.args.lm_gen_temperature,
            repetition_penalty=self.args.lm_gen_repetition_penalty,
        )

        self._warmup(
            target=self._model.generate,
            kwargs=warmup_gen_kwargs,
            streamer=self._streamer,
        )

    # @torch.no_grad()
    @torch.inference_mode()
    def generate(self, session: Session):
        """
        TTS: text + ref audio -> llama2 -> vq code tokens
        """
        prompt = session.ctx.state["prompt"]  # tts text
        formatted_text = f"<|TEXT_UNDERSTANDING_START|>{prompt}<|TEXT_UNDERSTANDING_END|>"

        # Tokenize the text
        chat = [
            {"role": "user", "content": "Convert the text to speech:" + formatted_text},
            {"role": "assistant", "content": "<|SPEECH_GENERATION_START|>"},
        ]

        input_ids = self._tokenizer.apply_chat_template(
            chat, tokenize=True, return_tensors="pt", continue_final_message=True
        )
        input_ids = input_ids.to("cuda")
        speech_end_id = self._tokenizer.convert_tokens_to_ids("<|SPEECH_GENERATION_END|>")
        generation_kwargs = dict(
            input_ids=input_ids,
            eos_token_id=speech_end_id,
            streamer=self._streamer,
            min_new_tokens=self.args.lm_gen_min_new_tokens,
            max_new_tokens=self.args.lm_gen_max_new_tokens,
            top_k=self.args.lm_gen_top_k,
            top_p=self.args.lm_gen_top_p,
            do_sample=self.args.lm_gen_do_sample,
            temperature=self.args.lm_gen_temperature,
            repetition_penalty=self.args.lm_gen_repetition_penalty,
        )
        thread = Thread(target=self._model.generate, kwargs=generation_kwargs)
        thread.start()

        i, j = 0, 0
        generated_ids = []
        for token_id in self._streamer:
            print(token_id, end=",", flush=True)
            generated_ids.append(token_id)
            i += 1

            if i % self.args.lm_tokenizer_decode_batch_size == 0:
                # print(generated_ids)
                speech_tokens = self._tokenizer.batch_decode(
                    torch.tensor(generated_ids).to(self.args.lm_device),
                    skip_special_tokens=True,
                )
                # Convert  token <|s_23456|> to int 23456
                speech_tokens = extract_speech_ids(speech_tokens)
                speech_vq_tokens = torch.tensor(speech_tokens).to(self.args.lm_device)
                yield speech_vq_tokens
                generated_ids = []
                j += 1

        if len(generated_ids) > 0:  # last batch
            speech_tokens = self._tokenizer.batch_decode(
                torch.tensor(generated_ids).to(self.args.lm_device),
                skip_special_tokens=True,
            )
            # Convert  token <|s_23456|> to int 23456
            speech_tokens = extract_speech_ids(speech_tokens)
            speech_vq_tokens = torch.tensor(speech_tokens).to(self.args.lm_device)
            yield speech_vq_tokens
