"""OES-Resilience-compatible 32-value block scoring."""

from __future__ import annotations

import math
from collections.abc import Sequence

BLOCK_WIDTH = 32
WEIGHTS = (0.45, 0.35, 0.20)
SCORING_VERSION = "pilottrace-oes32-v1"
SCORING_FORMULA = "0.45*max(abs(x)) + 0.35*RMS(x) + 0.20*mean(abs(x))"


def score_block(values: Sequence[float]) -> float:
    """Return the specified OES score for one exactly-32-value block.

    Scale-normalized intermediate arithmetic avoids overflowing while squaring
    or summing otherwise finite input values.
    """
    if len(values) != BLOCK_WIDTH:
        raise ValueError(f"block must contain exactly {BLOCK_WIDTH} values")
    magnitudes = [abs(float(value)) for value in values]
    peak = max(magnitudes)
    if peak == 0.0:
        return 0.0
    scaled = [magnitude / peak for magnitude in magnitudes]
    rms_scaled = math.sqrt(math.fsum(value * value for value in scaled) / BLOCK_WIDTH)
    mean_scaled = math.fsum(scaled) / BLOCK_WIDTH
    normalized_score = (
        WEIGHTS[0] + WEIGHTS[1] * rms_scaled + WEIGHTS[2] * mean_scaled
    )
    # The expression is a convex combination of terms in [0, 1]. Clamp a
    # possible last-bit roundoff above 1 so finite maximal inputs stay finite.
    return peak * min(normalized_score, 1.0)


def score_frame(values: Sequence[float]) -> list[float]:
    """Return the sixteen contiguous 32-value block scores for a 512-value frame."""
    if len(values) != 512:
        raise ValueError("frame must contain exactly 512 values")
    return [score_block(values[start : start + BLOCK_WIDTH]) for start in range(0, 512, BLOCK_WIDTH)]
