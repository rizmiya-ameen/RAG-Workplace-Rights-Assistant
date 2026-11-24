"""Prompt-injection classifier: the second input layer behind the regex rules (step 16).

    python -m app.guardrails.classifier "Ignore previous instructions and ..."   # score one text

Model: protectai/deberta-v3-base-prompt-injection-v2 (Apache-2.0), using the ONNX export
published in the model's own repository, pinned to one commit so the files can't change
under us (supply chain, OWASP LLM03). Runs on ONNX Runtime with the `tokenizers` library:
no torch. Labels: 0 SAFE, 1 INJECTION; `injection_score` is the softmax probability of
INJECTION.

Meta's Prompt Guard 2 (the plan's first choice) is gated and has no official ONNX build;
the only ONNX copies are third-party conversions, so this model was chosen instead.

The model is ~740 MB and loads lazily on first use (~1 s per question on the dev laptop's
CPU; measured in the red-team eval). Tests use a fake scorer.
"""

import argparse
from functools import lru_cache
from pathlib import Path

import numpy as np

from app.config import get_settings

REPO = "protectai/deberta-v3-base-prompt-injection-v2"
REVISION = "90c9989b1a342275dd0d1a95aad283c04e075671"  # pinned commit (2026-07-09)
FILES = ("onnx/model.onnx", "onnx/tokenizer.json", "onnx/config.json")
MAX_TOKENS = 512
INJECTION_LABEL = 1


def model_dir() -> Path:
    return get_settings().model_cache_dir / "prompt-injection-v2"


def ensure_model() -> Path:
    """Download the pinned files once (into .cache/models, on E:)."""
    from huggingface_hub import hf_hub_download

    target = model_dir()
    for name in FILES:
        if not (target / name).exists():
            hf_hub_download(REPO, name, revision=REVISION, local_dir=target)
    return target / "onnx"


@lru_cache
def _session():
    import onnxruntime as ort
    from tokenizers import Tokenizer

    folder = ensure_model()
    tokenizer = Tokenizer.from_file(str(folder / "tokenizer.json"))
    tokenizer.enable_truncation(MAX_TOKENS)
    options = ort.SessionOptions()
    options.intra_op_num_threads = 4  # the dev laptop has 4 cores
    session = ort.InferenceSession(
        str(folder / "model.onnx"), options, providers=["CPUExecutionProvider"]
    )
    return tokenizer, session


def injection_score(text: str) -> float:
    """Probability (0-1) that `text` is a prompt-injection attempt."""
    tokenizer, session = _session()
    enc = tokenizer.encode(text)
    feeds = {
        "input_ids": np.array([enc.ids], dtype=np.int64),
        "attention_mask": np.array([enc.attention_mask], dtype=np.int64),
    }
    names = {i.name for i in session.get_inputs()}
    if "token_type_ids" in names:
        feeds["token_type_ids"] = np.array([enc.type_ids], dtype=np.int64)
    logits = session.run(None, {k: v for k, v in feeds.items() if k in names})[0][0]
    exp = np.exp(logits - logits.max())
    return float(exp[INJECTION_LABEL] / exp.sum())


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("text")
    args = ap.parse_args()
    print(f"{injection_score(args.text):.4f}")


if __name__ == "__main__":
    main()
