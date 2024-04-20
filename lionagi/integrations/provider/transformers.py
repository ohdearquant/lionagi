from typing import Union, Dict, Any

from lionagi.libs.ln_api import BaseService
from lionagi.integrations.bridge.transformers_._install import install_transformers

allowed_kwargs = [
    "model",
    "tokenizer",
    "modelcard",
    "framework",
    "task",
    "num_workers",
    "batch_size",
    "args_parser",
    "device",
    "torch_dtype",
    "min_length_for_response",
    "minimum_tokens",
]


class TransformersService(BaseService):
    def __init__(
        self,
        task: str = None,
        model: Union[str, Any] = None,
        config: Union[str, Dict, Any] = None,
        device="cpu",
        **kwargs,
    ):
        super().__init__()
        self.task = task
        self.model = model
        self.config = config
        try:
            from transformers import pipeline

            self.pipeline = pipeline
        except ImportError:
            try:
                install_transformers()
                from transformers import pipeline

                self.pipeline = pipeline
            except Exception as e:
                raise ImportError(
                    f"Unable to import required module from transformers. Please make sure that transformers is installed. Error: {e}"
                )

        self.pipe = self.pipeline(
            task=task, model=model, config=config, device=device, **kwargs
        )

    async def serve_chat(self, messages, **kwargs):
        if self.task:
            if self.task != "conversational":
                raise ValueError(f"Invalid transformers pipeline task: {self.task}.")

        payload = {"messages": messages}
        config = {}
        for k, v in kwargs.items():
            if k in allowed_kwargs:
                config[k] = v

        conversation = self.pipe(str(messages), **config)

        texts = conversation[-1]["generated_text"]
        msgs = (
            str(texts.split("]")[1:])
            .replace("\\n", "")
            .replace("['", "")
            .replace("\\", "")
        )

        completion = {"model": self.pipe.model, "choices": [{"message": msgs}]}

        return payload, completion
