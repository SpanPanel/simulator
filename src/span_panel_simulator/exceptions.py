"""Simulator-specific exceptions."""

from __future__ import annotations


class SimulationConfigurationError(Exception):
    """Raised when simulation YAML configuration is invalid."""
