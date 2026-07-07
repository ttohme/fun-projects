"""
apps/orchestrator/schema_validator.py
Validates task objects against task-object.schema.json using jsonschema.
"""
import json
from pathlib import Path

try:
    import jsonschema
    _JSONSCHEMA_AVAILABLE = True
except ImportError:
    _JSONSCHEMA_AVAILABLE = False

SCHEMA_PATH = Path(__file__).resolve().parents[2] / "apps" / "orchestrator" / "schemas" / "task-object.schema.json"
_SCHEMA: dict | None = None


class ValidationError(Exception):
    pass


def _get_schema() -> dict:
    global _SCHEMA
    if _SCHEMA is None:
        _SCHEMA = json.loads(SCHEMA_PATH.read_text())
    return _SCHEMA


def validate_task_object(task: dict) -> None:
    """
    Raise ValidationError if task does not conform to task-object.schema.json.
    Falls back to manual required-field check if jsonschema is not installed.
    """
    schema = _get_schema()
    required = schema.get("required", [])

    if _JSONSCHEMA_AVAILABLE:
        try:
            jsonschema.validate(instance=task, schema=schema)
        except jsonschema.ValidationError as e:
            raise ValidationError(f"Task object invalid: {e.message}") from e
        return

    # Fallback: check required fields only
    missing = [f for f in required if f not in task]
    if missing:
        raise ValidationError(f"Task object missing required fields: {missing}")

    # Validate enum values for fields present in the object
    for prop, spec in schema.get("properties", {}).items():
        if prop in task and "enum" in spec:
            if task[prop] not in spec["enum"]:
                raise ValidationError(
                    f"Field '{prop}' value {task[prop]!r} not in allowed values: {spec['enum']}"
                )
