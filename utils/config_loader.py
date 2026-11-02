"""

"""

from __future__ import annotations

import copy
import threading

from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from logs.logger import PrettyPrinter, get_logger  # type: ignore
from src.utils.configuration import DEFAULT_CONFIG_REPOSITORY  # type: ignore


logger = get_logger("CADCon Config Loader")
printer = PrettyPrinter()

_COMPONENT = "config_loader"



__all__ = []