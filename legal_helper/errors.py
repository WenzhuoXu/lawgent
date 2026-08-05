"""Shared provider/API error classification."""

from __future__ import annotations


OUT_OF_MONEY_MESSAGE = "没钱啦，喊修勾充钱！"


def is_out_of_money_error(exc: BaseException) -> bool:
    text = f"{type(exc).__name__}: {exc}".lower()
    markers = (
        "insufficient_quota",
        "insufficient quota",
        "exceeded your current quota",
        "billing",
        "payment required",
        "credits",
        "credit balance",
        "prepaid",
        "out of money",
        "no credit",
        "quota_exceeded",
        "没钱",
        "充钱",
    )
    return any(marker in text for marker in markers)


def is_rate_limit_error(exc: BaseException) -> bool:
    text = f"{type(exc).__name__}: {exc}".lower()
    markers = (
        "rate_limit_error",
        "rate limit",
        "rate-limit",
        "too many requests",
        "error code: 429",
        "status code: 429",
        "http 429",
        "tokens per minute",
        "requests per minute",
        "input tokens per minute",
        "output tokens per minute",
        "tpm",
        "rpm",
    )
    return any(marker in text for marker in markers)
