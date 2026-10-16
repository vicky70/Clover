"""Track loader: tracks/<skill>/*.yaml -> TaskSpec, in file-name order.

A spec is rejected unless its verifier is `state_check` and at least one check is
marked `unique: true` (eng D8): leftover state from earlier tasks can't make a
unique check pass, so it's what makes an attempt count.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

# Check kinds clover/checks/docker.py knows how to run, with their allowed params.
CHECK_PARAMS: dict[str, set[str]] = {
    "container_running": {"name", "image"},
    "container_exited": {"name", "image", "exit_code"},
    "port_published": {"container_port", "host_port", "protocol"},
    "http_ok": {"url", "contains"},
    "volume_exists": {"name"},
    "volume_mounted": {"container", "volume", "destination"},
    "file_in_container": {"container", "path", "contains"},
    "image_exists": {"tag"},
}
RESERVED_VERIFIERS = {"test_suite", "quiz", "ai_review"}
SPEC_KEYS = {"id", "skill", "title", "est_minutes", "instructions", "verifier"}

DEFAULT_TRACKS_DIR = Path(__file__).resolve().parent.parent / "tracks"


class TrackError(ValueError):
    pass


@dataclass(frozen=True)
class Check:
    key: str          # stable id within the task: "<index>:<kind>"
    kind: str
    params: dict
    unique: bool

    def to_dict(self) -> dict:
        return {"key": self.key, "kind": self.kind, "params": self.params, "unique": self.unique}


@dataclass(frozen=True)
class TaskSpec:
    id: str
    skill: str
    title: str
    est_minutes: int
    instructions: str
    checks: tuple[Check, ...] = field(default_factory=tuple)

    @property
    def unique_keys(self) -> frozenset[str]:
        return frozenset(c.key for c in self.checks if c.unique)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "skill": self.skill,
            "title": self.title,
            "est_minutes": self.est_minutes,
            "instructions": self.instructions,
            "verifier": {"type": "state_check", "checks": [c.to_dict() for c in self.checks]},
        }


def parse_spec(data: object, source: str = "<spec>") -> TaskSpec:
    if not isinstance(data, dict):
        raise TrackError(f"{source}: spec must be a mapping")
    unknown = set(data) - SPEC_KEYS
    if unknown:
        raise TrackError(f"{source}: unknown keys {sorted(unknown)}")
    for key in ("id", "skill", "title", "verifier"):
        if key not in data:
            raise TrackError(f"{source}: missing '{key}'")

    verifier = data["verifier"]
    if not isinstance(verifier, dict):
        raise TrackError(f"{source}: verifier must be a mapping")
    vtype = verifier.get("type")
    if vtype in RESERVED_VERIFIERS:
        raise TrackError(f"{source}: verifier type '{vtype}' is reserved and not built in v1")
    if vtype != "state_check":
        raise TrackError(f"{source}: unknown verifier type {vtype!r}")
    raw_checks = verifier.get("checks")
    if not isinstance(raw_checks, list) or not raw_checks:
        raise TrackError(f"{source}: state_check needs a non-empty 'checks' list")

    checks = []
    for i, item in enumerate(raw_checks):
        if not isinstance(item, dict) or len(item) != 1:
            raise TrackError(f"{source}: check #{i} must be a single-key mapping")
        (kind, params), = item.items()
        if kind not in CHECK_PARAMS:
            raise TrackError(f"{source}: unknown check kind '{kind}'")
        params = dict(params or {})
        unique = params.pop("unique", False)
        if not isinstance(unique, bool):
            raise TrackError(f"{source}: check #{i} 'unique' must be true or false")
        bad = set(params) - CHECK_PARAMS[kind]
        if bad:
            raise TrackError(f"{source}: check #{i} ({kind}) has unknown params {sorted(bad)}")
        checks.append(Check(key=f"{i}:{kind}", kind=kind, params=params, unique=unique))

    if not any(c.unique for c in checks):
        raise TrackError(f"{source}: no check is marked 'unique: true'")

    return TaskSpec(
        id=str(data["id"]),
        skill=str(data["skill"]),
        title=str(data["title"]),
        est_minutes=int(data.get("est_minutes", 60)),
        instructions=str(data.get("instructions", "")).strip(),
        checks=tuple(checks),
    )


def load_track(skill: str, tracks_dir: Path = DEFAULT_TRACKS_DIR) -> list[TaskSpec]:
    folder = Path(tracks_dir) / skill
    files = sorted(folder.glob("*.yaml"))
    if not files:
        raise TrackError(f"no task specs in {folder}")
    specs = []
    for path in files:
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
        except yaml.YAMLError as e:
            raise TrackError(f"{path.name}: invalid YAML: {e}") from e
        spec = parse_spec(data, path.name)
        if spec.skill != skill:
            raise TrackError(f"{path.name}: skill '{spec.skill}' doesn't match track '{skill}'")
        specs.append(spec)
    ids = [s.id for s in specs]
    if len(set(ids)) != len(ids):
        raise TrackError(f"duplicate task ids in track '{skill}'")
    return specs
