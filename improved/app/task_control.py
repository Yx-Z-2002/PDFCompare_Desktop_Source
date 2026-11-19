"""Cooperative cancellation shared by the native UI and background services."""
from __future__ import annotations


class TaskCancelled(Exception):
    """The user requested cancellation; this is not an extraction failure."""

    def __init__(self, message: str = '任务已中止。'):
        super().__init__(message)


def check_cancel(cancel_check=None):
    """Call an optional no-argument checkpoint, which raises TaskCancelled.

    A checkpoint does not terminate a thread or interrupt an indivisible PDF
    library call. Callers place it around those calls and inside Python loops.
    """
    if cancel_check is not None:
        cancel_check()
