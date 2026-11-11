"""Operator-log helpers that never print exception text.

An exception message can contain anything a provider or library chose to put in it:
the prompt, a credential, an upstream body. So default logs record only the exception
type and the call-site frames (file:line:function), never ``str(exc)``, never
``exc_info``/traceback rendering (which prints the message), and never the exception
chain's messages.
"""

from __future__ import annotations

import os
import traceback

MAX_FRAMES = 8


def exception_type(exc: BaseException) -> str:
    cls = type(exc)
    return f"{cls.__module__}.{cls.__qualname__}"


def exception_frames(exc: BaseException) -> str:
    """Innermost-first ``file:line:function`` frames. Source text and locals are omitted."""
    frames = traceback.extract_tb(exc.__traceback__)[-MAX_FRAMES:]
    return " <- ".join(
        f"{os.path.basename(f.filename)}:{f.lineno}:{f.name}" for f in reversed(frames)
    )


def describe(exc: BaseException) -> str:
    return f"type={exception_type(exc)} at={exception_frames(exc)}"
