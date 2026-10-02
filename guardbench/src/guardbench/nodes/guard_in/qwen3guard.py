"""Qwen3Guard-Gen input moderation through the ordinary Node interface."""

import asyncio
import json
from pathlib import Path
import re
import threading

from ...core.node import Node, register
from ...llm.client import ROOT, model_settings

# Nodes are recreated per turn; heavyweight resources live for this process.
_MODELS = {}
_LOCK = threading.Lock()


def parse_output(output):
    safety = re.search(r"^Safety:\s*(Safe|Unsafe|Controversial)\s*$", output, re.MULTILINE)
    categories = re.search(r"^Categories:[ \t]*([^\n]+)", output, re.MULTILINE)
    names = [name.strip() for name in categories[1].split(",")] if categories else []
    valid = bool(safety and categories and all(names))
    return {
        "label": safety[1].lower() if valid else "unresolved",
        "categories": [name for name in names if name != "None"],
        "raw": {"output": output, "safety": safety[1] if safety else None,
                "categories": names, "parse_error": None if valid else "Missing or invalid Safety/Categories"},
    }


def _load_model(settings):
    import torch
    from transformers import AutoModelForCausalLM, AutoTokenizer

    path = settings["local_path"]
    if not Path(path).is_dir():
        raise ValueError(f"Qwen3Guard local model directory not found: {path}")
    manifest = Path(path) / "download_manifest.json"
    if manifest.is_file():
        downloaded = json.loads(manifest.read_text())
        for key in ("model_id", "revision"):
            if downloaded.get(key) != settings[key]:
                raise ValueError(f"Qwen3Guard download manifest does not match configured {key}")
    device = settings["device"]
    if device == "auto":
        device = "cuda:0" if torch.cuda.is_available() else "cpu"
    dtype = settings["dtype"]
    if dtype == "auto":
        dtype = "auto" if device.startswith("cuda") else torch.float32
    else:
        dtype = getattr(torch, dtype)
    torch.set_num_threads(settings["cpu_threads"])
    tokenizer = AutoTokenizer.from_pretrained(path, local_files_only=True)
    model = AutoModelForCausalLM.from_pretrained(path, dtype=dtype, local_files_only=True)
    model.to(device).eval()
    return tokenizer, model


def _infer(settings, messages):
    import torch

    key = tuple(settings[name] for name in ("local_path", "revision", "device", "dtype", "cpu_threads"))
    # Run in a worker thread, serializing access to the shared local model.
    with _LOCK:
        if key not in _MODELS:
            _MODELS[key] = _load_model(settings)
        tokenizer, model = _MODELS[key]
        rendered = tokenizer.apply_chat_template(messages, tokenize=False)
        encoded = tokenizer([rendered], return_tensors="pt", add_special_tokens=False).to(model.device)
        input_tokens = encoded["input_ids"].shape[-1]
        max_new_tokens = settings["max_new_tokens"]
        if input_tokens + max_new_tokens > model.config.max_position_embeddings:
            raise ValueError("Qwen3Guard input exceeds model context capacity")
        with torch.inference_mode():
            generated = model.generate(**encoded, max_new_tokens=max_new_tokens,
                                       do_sample=settings["do_sample"])
        completion = generated[0][input_tokens:]
        result = parse_output(tokenizer.decode(completion, skip_special_tokens=True))
        result["raw"].update(model_id=settings["model_id"], revision=settings["revision"],
                             device=str(model.device), input_tokens=input_tokens,
                             generated_tokens=len(completion))
        return result


@register("guard_in/qwen3guard")
class Qwen3Guard(Node):
    def __init__(self, model="qwen3guard", models_config="configs/models.yaml", sleep=0.0):
        super().__init__(sleep)
        settings = model_settings(model, models_config)
        if settings["backend"] != "local_hf" or settings.get("variant") != "Gen":
            raise ValueError("guard_in/qwen3guard requires local_hf with variant Gen")
        for key in ("model_id", "local_path"):
            settings[key] = settings[key].format(**settings)
        path = Path(settings["local_path"])
        settings["local_path"] = str(path if path.is_absolute() else ROOT / path)
        for key, default in (("device", "auto"), ("dtype", "auto"), ("cpu_threads", 8),
                             ("max_new_tokens", 128), ("do_sample", False)):
            settings.setdefault(key, default)
        if not isinstance(settings.get("revision"), str) or not settings["revision"]:
            raise ValueError("Qwen3Guard requires a configured revision")
        if not isinstance(settings["device"], str) or not settings["device"]:
            raise ValueError("Qwen3Guard requires a device string")
        if settings["dtype"] not in ("auto", "float32", "float16", "bfloat16"):
            raise ValueError("Qwen3Guard dtype must be auto/float32/float16/bfloat16")
        for key in ("cpu_threads", "max_new_tokens"):
            if isinstance(settings[key], bool) or not isinstance(settings[key], int) or settings[key] <= 0:
                raise ValueError(f"Qwen3Guard {key} must be a positive integer")
        if not isinstance(settings["do_sample"], bool):
            raise ValueError("Qwen3Guard do_sample must be boolean")
        self.settings = settings

    async def run(self, inputs):
        await self.delay()
        messages = [{"role": m["role"], "content": m["content"]} for m in inputs["context"]]
        if not messages or messages[-1] != {"role": "user", "content": inputs["user"]}:
            raise ValueError("Qwen3Guard context must end with the current user")
        return await asyncio.to_thread(_infer, self.settings, messages)
