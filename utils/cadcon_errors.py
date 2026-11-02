"""CAD Converter"""

from collections.abc import Mapping
from types import MappingProxyType
from typing import Any


class CADConError(RuntimeError):
    code = "CADCon.ERROR"
    def __init__(self) -> None:
        super().__init__()
    

class CADConErrorWithMessage(CADConError):
    def __init__(self, message: str) -> None:
        super().__init__()
        self.message = message
        self.args = (message,)


class CADConErrorWithCode(CADConError):
    def __init__(self, code: str) -> None:
        super().__init__()
        self.code = code
        self.args = (code,)

__all__ = [
    "CADConError",
    "CADConErrorWithMessage",
    "CADConErrorWithCode"
]