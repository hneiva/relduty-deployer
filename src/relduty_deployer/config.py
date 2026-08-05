# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Machine-local settings, cached in ~/.config/relduty-deployer-config.json.

Only things that differ between machines belong here: where each checkout lives, which
remote deploys it, and whether to show it. Which branch deploys which environment is
knowledge about the project and lives in `projects.py`.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

from relduty_deployer.projects import PROJECT_SPECS, Project, ProjectSettings, default_path

CONFIG_PATH = Path.home() / ".config" / "relduty-deployer-config.json"
SCHEMA_VERSION = 1
DEFAULT_FETCH_TIMEOUT_SECONDS = 60


class ConfigError(RuntimeError):
    """The config file exists but cannot be used as-is."""


def _collapse_home(path: Path) -> str:
    """Write paths under the home directory back out as ~/... for readability."""
    try:
        return str(Path("~") / path.relative_to(Path.home()))
    except ValueError:
        return str(path)


@dataclass
class Config:
    """The full contents of the config file."""

    projects: dict[str, ProjectSettings]
    fetch_timeout_seconds: int = DEFAULT_FETCH_TIMEOUT_SECONDS
    confirm_before_push: bool = True

    def copy(self) -> Config:
        """A snapshot that later edits cannot mutate.

        `ProjectSettings` is frozen, so copying the mapping is enough.
        """
        return Config(
            projects=dict(self.projects),
            fetch_timeout_seconds=self.fetch_timeout_seconds,
            confirm_before_push=self.confirm_before_push,
        )

    def to_json(self) -> dict:
        """Serialise in registry order so the file diffs cleanly."""
        return {
            "schema_version": SCHEMA_VERSION,
            "fetch_timeout_seconds": self.fetch_timeout_seconds,
            "confirm_before_push": self.confirm_before_push,
            "projects": {
                spec.name: {
                    "path": _collapse_home(self.projects[spec.name].path),
                    "remote": self.projects[spec.name].remote,
                    "enabled": self.projects[spec.name].enabled,
                }
                for spec in PROJECT_SPECS
                if spec.name in self.projects
            },
        }


def default_config() -> Config:
    """Settings for a machine that has never run this tool."""
    return Config(projects={spec.name: ProjectSettings(path=default_path(spec.checkout_name)) for spec in PROJECT_SPECS})


def build_projects(config: Config) -> tuple[Project, ...]:
    """Pair each spec with its settings, in display order."""
    return tuple(Project(spec=spec, settings=config.projects[spec.name]) for spec in PROJECT_SPECS if spec.name in config.projects)


class ConfigStore:
    """Reads and writes the config file, and tracks whether it has unsaved edits."""

    def __init__(self, path: Path = CONFIG_PATH) -> None:
        self.path = path
        self.notes: list[str] = []
        self._snapshot: Config | None = None

    def load(self) -> Config:
        """Read the config, creating it with defaults if it does not exist yet.

        A first run writes the file immediately so the Save button starts out saved —
        nothing has been changed by the user at that point.
        """
        self.notes = []
        if not self.path.exists():
            config = default_config()
            self.save(config)
            return config

        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ConfigError(f"could not read {self.path}: {exc}") from exc
        if not isinstance(raw, dict):
            raise ConfigError(f"{self.path} should contain a JSON object, found {type(raw).__name__}")

        raw = self._migrate(raw)
        config = self._parse(raw)
        # Snapshot what is genuinely on disk. If defaults had to be filled in, the
        # in-memory config differs and the Save button will light up.
        self._snapshot = self._parse(raw, fill_missing=False).copy()
        return config

    def _migrate(self, raw: dict) -> dict:
        """Bring an older file up to the current schema, or refuse a newer one."""
        version = raw.get("schema_version")
        if not isinstance(version, int):
            raise ConfigError(f"{self.path} has no integer schema_version; move it aside to start fresh")
        if version > SCHEMA_VERSION:
            raise ConfigError(f"{self.path} has schema_version {version} but this build understands {SCHEMA_VERSION}; upgrade relduty-deployer")
        # Version 1 is the oldest schema, so there is nothing to upgrade yet. Future
        # migrations chain from here, each raising `version` by one.
        return raw

    def _parse(self, raw: dict, *, fill_missing: bool = True) -> Config:
        """Build a `Config` from already-migrated JSON."""
        raw_projects = raw.get("projects", {})
        if not isinstance(raw_projects, dict):
            raise ConfigError(f"{self.path}: 'projects' should be a JSON object")

        known = {spec.name for spec in PROJECT_SPECS}
        if fill_missing:
            for unknown in sorted(set(raw_projects) - known):
                self.notes.append(f"ignoring unknown project {unknown!r} in {self.path}")

        projects: dict[str, ProjectSettings] = {}
        for spec in PROJECT_SPECS:
            entry = raw_projects.get(spec.name)
            if entry is None:
                if fill_missing:
                    self.notes.append(f"{spec.name} was missing from {self.path}; using defaults")
                    projects[spec.name] = ProjectSettings(path=default_path(spec.checkout_name))
                continue
            if not isinstance(entry, dict):
                raise ConfigError(f"{self.path}: project {spec.name!r} should be a JSON object")
            raw_path = entry.get("path") or str(default_path(spec.checkout_name))
            projects[spec.name] = ProjectSettings(
                path=Path(str(raw_path)).expanduser(),
                remote=str(entry.get("remote") or "origin"),
                enabled=bool(entry.get("enabled", True)),
            )

        return Config(
            projects=projects,
            fetch_timeout_seconds=int(raw.get("fetch_timeout_seconds", DEFAULT_FETCH_TIMEOUT_SECONDS)),
            confirm_before_push=bool(raw.get("confirm_before_push", True)),
        )

    def save(self, config: Config) -> None:
        """Write the config atomically, then treat it as the new clean state."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(config.to_json(), indent=2) + "\n"
        handle, tmp_name = tempfile.mkstemp(dir=self.path.parent, prefix=f"{self.path.name}.", suffix=".tmp")
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as tmp:
                tmp.write(payload)
            os.replace(tmp_name, self.path)
        except BaseException:
            Path(tmp_name).unlink(missing_ok=True)
            raise
        self._snapshot = config.copy()

    def is_dirty(self, config: Config) -> bool:
        """Whether `config` differs from what is on disk."""
        return self._snapshot is None or config != self._snapshot
