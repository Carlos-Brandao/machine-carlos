"""Cooperative cancellation for login/captcha code on the owning browser thread."""
from __future__ import annotations

import threading
import time
from contextlib import contextmanager
from typing import Callable


class OperationCancelled(RuntimeError):
    """Operator cancellation/deadline; never a rejected portal credential."""

    def __init__(self, message: str, *, timed_out: bool = False):
        super().__init__(message)
        self.timed_out = timed_out


_current = threading.local()


@contextmanager
def cancellation_scope(cancelled: Callable[[], bool], *, timeout_seconds: float):
    previous = getattr(_current, 'state', None)
    _current.state = (cancelled, time.monotonic() + max(0, timeout_seconds))
    try:
        check_cancelled()
        yield
    finally:
        _current.state = previous


def check_cancelled() -> None:
    state = getattr(_current, 'state', None)
    if state is not None:
        cancelled, deadline = state
        if cancelled():
            raise OperationCancelled('Operação cancelada ou tempo limite atingido.')
        if time.monotonic() >= deadline:
            raise OperationCancelled('Tempo limite do login atingido.', timed_out=True)


def cancellation_wait(seconds: float, *, sleep: Callable[[float], None] = time.sleep) -> None:
    if getattr(_current, 'state', None) is None:
        sleep(seconds)
        return
    until = time.monotonic() + seconds
    while True:
        check_cancelled()
        remaining = until - time.monotonic()
        if remaining <= 0:
            return
        sleep(min(remaining, 0.25))
