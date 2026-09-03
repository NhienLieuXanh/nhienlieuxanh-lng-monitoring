"""Cây exception của adapter nguồn đo phút. Không để message lọt ra HTTP."""

from __future__ import annotations


class YokohamaError(Exception):
    """Gốc. URL/PSN có thể nằm trong message — không phát ra response."""


class YokohamaTransientError(YokohamaError):
    """5xx, timeout, connect. Có thể retry."""

    def __init__(self, status: int | None, detail: str = "") -> None:
        super().__init__(f"transient (status={status}): {detail}")
        self.status = status


class YokohamaSchemaError(YokohamaError):
    """Payload không đúng hợp đồng (quá lớn, không phải mảng, hết thời gian stream).

    Fatal: retry một dump 165 MB không giúp gì và đốt hết ngân sách function.
    """

    def __init__(self, detail: str, remediation: str = "") -> None:
        super().__init__(detail)
        self.detail = detail
        self.remediation = remediation
