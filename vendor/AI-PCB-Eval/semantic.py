from __future__ import annotations

from dataclasses import asdict
from difflib import SequenceMatcher
import re
from typing import Dict

from .kicad_utils import (
    extract_plain_text,
    extract_kicad_or_text,
    extract_numeric_values,
    extract_structural_features,
    feature_similarity,
    jaccard_similarity,
    normalize_kicad,
    number_similarity,
    tokenize_kicad,
    weighted_counter_similarity,
)
from .types import EvalConfig, SampleInput, SemanticScore

TEXT_TOKEN_RE = re.compile(r"\w+", re.UNICODE)


class KiCadSemanticScorer:
    """KiCad-aware semantic scorer for code completion responses."""

    def __init__(self, config: EvalConfig):
        self.config = config

    def score(self, sample: SampleInput) -> SemanticScore:
        prediction_code, has_kicad_code, extracted_blocks = extract_kicad_or_text(sample.prediction_raw)
        if self.config.require_kicad_code and not has_kicad_code:
            return SemanticScore(
                score=0.0,
                has_kicad_code=False,
                prediction_code=prediction_code,
                extracted_blocks=extracted_blocks,
                detail={
                    "reason": "missing_kicad_code",
                    "require_kicad_code": True,
                },
            )

        normalized_pred = normalize_kicad(prediction_code)
        normalized_label = normalize_kicad(sample.label)
        pred_tokens = tokenize_kicad(prediction_code)
        label_tokens = tokenize_kicad(sample.label)
        pred_numbers = extract_numeric_values(normalized_pred)
        label_numbers = extract_numeric_values(normalized_label)
        pred_features = extract_structural_features(prediction_code)
        label_features = extract_structural_features(sample.label)

        metrics: Dict[str, float] = {
            "sequence_ratio": SequenceMatcher(None, normalized_pred, normalized_label).ratio()
            if (normalized_pred or normalized_label)
            else 1.0,
            "token_jaccard": jaccard_similarity(pred_tokens, label_tokens),
            "token_overlap": weighted_counter_similarity(pred_tokens, label_tokens),
            "number_score": number_similarity(pred_numbers, label_numbers),
            "feature_score": feature_similarity(pred_features, label_features),
        }
        score = (
            0.20 * metrics["sequence_ratio"]
            + 0.25 * metrics["token_jaccard"]
            + 0.25 * metrics["token_overlap"]
            + 0.15 * metrics["number_score"]
            + 0.15 * metrics["feature_score"]
        )
        kicad_score = max(0.0, min(1.0, score))
        prediction_text = extract_plain_text(sample.prediction_raw)
        label_text = extract_plain_text(sample.label)
        text_score = self._text_similarity(prediction_text, label_text)
        semantic_score = self._combine_semantic_scores(kicad_score, text_score, label_text)

        return SemanticScore(
            score=semantic_score,
            has_kicad_code=has_kicad_code,
            prediction_code=prediction_code,
            extracted_blocks=extracted_blocks,
            detail={
                "normalized_prediction_length": len(normalized_pred),
                "normalized_label_length": len(normalized_label),
                "prediction_token_count": len(pred_tokens),
                "label_token_count": len(label_tokens),
                "prediction_numeric_count": len(pred_numbers),
                "label_numeric_count": len(label_numbers),
                "prediction_text": prediction_text,
                "label_text": label_text,
                "metrics": metrics,
                "kicad_score": kicad_score,
                "text_score": text_score,
                "semantic_kicad_weight": self.config.semantic_kicad_weight,
                "semantic_text_weight": self.config.semantic_text_weight,
                "prediction_features": pred_features,
                "label_features": label_features,
            },
        )

    def score_to_dict(self, sample: SampleInput) -> Dict[str, object]:
        return asdict(self.score(sample))

    def _text_similarity(self, prediction_text: str, label_text: str) -> float:
        if not label_text.strip():
            return 1.0
        pred = " ".join(prediction_text.split()).lower()
        label = " ".join(label_text.split()).lower()
        if not pred and not label:
            return 1.0
        if not pred or not label:
            return 0.0
        pred_tokens = TEXT_TOKEN_RE.findall(pred)
        label_tokens = TEXT_TOKEN_RE.findall(label)
        sequence_score = SequenceMatcher(None, pred, label).ratio()
        token_jaccard = jaccard_similarity(pred_tokens, label_tokens)
        token_overlap = weighted_counter_similarity(pred_tokens, label_tokens)
        return max(0.0, min(1.0, 0.5 * sequence_score + 0.25 * token_jaccard + 0.25 * token_overlap))

    def _combine_semantic_scores(self, kicad_score: float, text_score: float, label_text: str) -> float:
        if not label_text.strip():
            return kicad_score
        total_weight = self.config.semantic_kicad_weight + self.config.semantic_text_weight
        if total_weight <= 0:
            return kicad_score
        return (
            self.config.semantic_kicad_weight * kicad_score
            + self.config.semantic_text_weight * text_score
        ) / total_weight
