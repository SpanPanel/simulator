"""Standalone configuration validation functions.

Extracted from DynamicSimulationEngine so that both the engine and the
dashboard can validate configs without circular imports.
"""

from __future__ import annotations

from typing import Any


def validate_yaml_config(config_data: Any) -> None:
    """Validate YAML configuration structure and required fields."""
    if not isinstance(config_data, dict):
        raise ValueError("YAML configuration must be a dictionary")

    required_sections = ["panel_config", "circuit_templates", "circuits"]
    for section in required_sections:
        if section not in config_data:
            raise ValueError(f"Missing required section: {section}")

    validate_panel_config(config_data["panel_config"])
    validate_circuit_templates(config_data["circuit_templates"])
    validate_circuits(config_data["circuits"], config_data["circuit_templates"])


def validate_panel_config(panel_config: Any) -> None:
    """Validate panel configuration section."""
    if not isinstance(panel_config, dict):
        raise ValueError("panel_config must be a dictionary")

    required_panel_fields = ["serial_number", "total_tabs", "main_size"]
    for field in required_panel_fields:
        if field not in panel_config:
            raise ValueError(f"Missing required panel_config field: {field}")


def validate_circuit_templates(circuit_templates: Any) -> None:
    """Validate circuit templates section."""
    if not isinstance(circuit_templates, dict):
        raise ValueError("circuit_templates must be a dictionary")

    if not circuit_templates:
        raise ValueError("At least one circuit template must be defined")

    for template_name, template in circuit_templates.items():
        validate_single_template(template_name, template)


def validate_single_template(template_name: str, template: Any) -> None:
    """Validate a single circuit template."""
    if not isinstance(template, dict):
        raise ValueError(f"Circuit template '{template_name}' must be a dictionary")

    required_template_fields = [
        "energy_profile",
        "relay_behavior",
        "priority",
    ]
    for field in required_template_fields:
        if field not in template:
            raise ValueError(
                f"Missing required field '{field}' in circuit template '{template_name}'"
            )


def validate_circuits(circuits: Any, circuit_templates: dict[str, Any]) -> None:
    """Validate circuits section."""
    if not isinstance(circuits, list):
        raise ValueError("circuits must be a list")

    if not circuits:
        raise ValueError("At least one circuit must be defined")

    for i, circuit in enumerate(circuits):
        validate_single_circuit(i, circuit, circuit_templates)


def validate_single_circuit(index: int, circuit: Any, circuit_templates: dict[str, Any]) -> None:
    """Validate a single circuit definition."""
    if not isinstance(circuit, dict):
        raise ValueError(f"Circuit {index} must be a dictionary")

    required_circuit_fields = ["id", "name", "template", "tabs"]
    for field in required_circuit_fields:
        if field not in circuit:
            raise ValueError(f"Missing required field '{field}' in circuit {index}")

    template_name = circuit["template"]
    if template_name not in circuit_templates:
        raise ValueError(f"Circuit {index} references unknown template '{template_name}'")

    tabs = circuit["tabs"]
    if not isinstance(tabs, list) or not tabs:
        raise ValueError(f"Circuit {index} ('tabs') must be a non-empty list")

    if len(tabs) == 2:
        validate_double_pole_tabs(index, circuit.get("name", f"circuit {index}"), tabs)


def validate_double_pole_tabs(index: int, name: str, tabs: list[int]) -> None:
    """Validate that a double-pole (240V) circuit uses a valid tab pair.

    A valid pair must be:
    - Same parity (both odd or both even)
    - Exactly 2 apart
    """
    a, b = sorted(tabs)

    if a % 2 != b % 2:
        raise ValueError(
            f"Circuit {index} ('{name}') has double-pole tabs {tabs} with mixed parity. "
            f"Both tabs must be odd or both even."
        )

    if b - a != 2:
        raise ValueError(
            f"Circuit {index} ('{name}') has double-pole tabs {tabs} that are "
            f"{b - a} apart. Double-pole tabs must be exactly 2 apart."
        )
