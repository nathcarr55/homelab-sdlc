"""
Sandbox app — used by the E2E pipeline tests.
The AI agent is asked to add new functions here; tests verify they work.
"""

from __future__ import annotations


def add(a: int | float, b: int | float) -> int | float:
    return a + b


def greet(name: str) -> str:
    return f"Hello, {name}!"
