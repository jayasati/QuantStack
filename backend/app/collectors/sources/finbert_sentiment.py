"""FinBERT sentiment provider (Volume 2, Prompt 2.10 upgrade).

Swaps the lexicon-based ``SentimentProvider`` for ``ProsusAI/finbert``, a
BERT model fine-tuned on financial text (Financial PhraseBank), through the
pluggable seam ``NewsIntelligenceCollector`` already exposed for exactly
this purpose. Model weights (~440MB) download once from the Hugging Face
Hub on first use and are cached locally (``~/.cache/huggingface``);
inference runs on CPU.

CPU inference is not free: a full forward pass on a BERT-base model takes on
the order of tens-to-low-hundreds of milliseconds per article. ``score_batch``
runs one batched forward pass instead of N separate ones and should be
preferred whenever scoring more than a couple of articles at once —
``NewsIntelligenceCollector`` calls it automatically when present. Both
``score`` and ``score_batch`` are blocking/CPU-bound; callers running inside
an event loop must offload them (e.g. ``asyncio.to_thread``) instead of
awaiting them directly.
"""

from typing import Any

from app.collectors.domains.news import SentimentProvider
from app.core.logging import get_logger

logger = get_logger(__name__)

MODEL_NAME = "ProsusAI/finbert"
MAX_TOKENS = 512  # FinBERT's positional embedding limit


class FinBertSentimentProvider(SentimentProvider):
    """Lazily loads the model on first score/score_batch call, not at
    construction — constructing this provider (e.g. as a collector's default
    constructor argument) must stay cheap and offline."""

    def __init__(self, model_name: str = MODEL_NAME) -> None:
        self._model_name = model_name
        self._pipeline: Any | None = None

    def _ensure_loaded(self) -> Any:
        if self._pipeline is None:
            from transformers import (
                AutoModelForSequenceClassification,
                AutoTokenizer,
                TextClassificationPipeline,
            )

            logger.info("loading finbert sentiment model", extra={"model": self._model_name})
            tokenizer = AutoTokenizer.from_pretrained(self._model_name)
            model = AutoModelForSequenceClassification.from_pretrained(self._model_name)
            self._pipeline = TextClassificationPipeline(
                model=model,
                tokenizer=tokenizer,
                top_k=None,
                truncation=True,
                max_length=MAX_TOKENS,
            )
        return self._pipeline

    def score(self, text: str) -> float:
        return self.score_batch([text])[0] if text else 0.0

    def score_batch(self, texts: list[str]) -> list[float]:
        """One batched forward pass for every text (empty strings score 0.0
        without going through the model)."""
        if not texts:
            return []
        indices = [i for i, t in enumerate(texts) if t]
        scores = [0.0] * len(texts)
        if not indices:
            return scores
        pipeline = self._ensure_loaded()
        results = pipeline([texts[i] for i in indices])
        for i, class_scores in zip(indices, results, strict=True):
            scores[i] = _to_signed_score(class_scores)
        return scores


def _to_signed_score(class_scores: list[dict]) -> float:
    """positive_prob - negative_prob, in [-1, 1] (neutral_prob dilutes both
    without needing its own sign — it's implicitly "the rest")."""
    by_label = {row["label"].lower(): row["score"] for row in class_scores}
    positive = by_label.get("positive", 0.0)
    negative = by_label.get("negative", 0.0)
    return max(-1.0, min(1.0, positive - negative))
