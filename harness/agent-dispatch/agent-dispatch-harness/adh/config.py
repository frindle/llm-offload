"""Configuration loading.

Every host-specific value — endpoints, paths, model memory figures, task
locations — lives in a TOML file that is not committed. The source contains no
host literals, which is what makes the harness portable *and* what keeps a
contributor's machine out of the repository.

Resolution order: --config PATH, then $ADH_CONFIG, then ./config.toml.
"""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

CONFIG_ENV = "ADH_CONFIG"
DEFAULT_NAME = "config.toml"


class ConfigError(RuntimeError):
    pass


@dataclass(frozen=True)
class ModelSpec:
    name: str
    native_ctx: int
    resident_mb: int
    wall_s: int = 3600
    manual_tools: bool = False

    @property
    def slug(self) -> str:
        """Filesystem-safe form used for archived diffs and transcripts."""
        return self.name.replace(":", "-").replace("/", "-")

    def num_ctx(self, target_ctx: int) -> int:
        """min(target, native). Exceeding native is not a bigger window, it is
        an out-of-spec run, and comparing two models at silently different
        windows is not a comparison at all — both numbers go in the CSV."""
        if self.native_ctx <= 0:
            raise ConfigError(
                f"model {self.name!r} has no native_ctx; probe it with "
                f"`bin/adh probe` and put the real number in the config "
                f"rather than letting the harness guess")
        return min(target_ctx, self.native_ctx)


@dataclass
class Config:
    path: Path
    raw: dict[str, Any] = field(default_factory=dict)

    # -- section accessors ---------------------------------------------------
    def section(self, name: str) -> dict[str, Any]:
        v = self.raw.get(name)
        return v if isinstance(v, dict) else {}

    def get(self, section: str, key: str, default: Any = None) -> Any:
        return self.section(section).get(key, default)

    @property
    def root(self) -> Path:
        return self.path.parent

    def resolve(self, p: str | os.PathLike) -> Path:
        """Relative paths resolve against the config file's directory, so a
        config can be moved somewhere private without breaking its own paths."""
        q = Path(p).expanduser()
        return q if q.is_absolute() else (self.root / q)

    def dir(self, key: str) -> Path:
        paths = self.section("paths")
        if key not in paths:
            raise ConfigError(f"[paths] is missing {key!r}")
        d = self.resolve(paths[key])
        d.mkdir(parents=True, exist_ok=True)
        return d

    def log_file(self) -> Path:
        p = self.resolve(self.get("paths", "log_file", "logs/driver.log"))
        p.parent.mkdir(parents=True, exist_ok=True)
        return p

    # -- roster --------------------------------------------------------------
    @property
    def models(self) -> list[ModelSpec]:
        out: list[ModelSpec] = []
        for m in self.raw.get("models", []) or []:
            if "name" not in m:
                raise ConfigError("a [[models]] block has no `name`")
            out.append(ModelSpec(
                name=m["name"],
                native_ctx=int(m.get("native_ctx", 0)),
                resident_mb=int(m.get("resident_mb", 0)),
                wall_s=int(m.get("wall_s", 3600)),
                manual_tools=bool(m.get("manual_tools", False)),
            ))
        if not out:
            raise ConfigError("no [[models]] configured")
        return out

    def model(self, name: str) -> ModelSpec:
        for m in self.models:
            if m.name == name:
                return m
        raise ConfigError(f"model {name!r} is not in the roster")

    @property
    def task_dirs(self) -> list[Path]:
        out = [self.resolve(t["path"]) for t in (self.raw.get("tasks", []) or [])
               if "path" in t]
        if not out:
            raise ConfigError("no [[tasks]] configured")
        return out

    @property
    def target_ctx(self) -> int:
        return int(self.get("context", "target_ctx", 32768))

    @property
    def arms(self) -> list[str]:
        arms = list(self.get("arms", "enabled", ["base"]))
        return arms if "base" in arms else ["base", *arms]


def find_config(explicit: str | None = None) -> Path:
    if explicit:
        p = Path(explicit).expanduser()
        if not p.is_file():
            raise ConfigError(f"config not found: {p}")
        return p.resolve()
    env = os.environ.get(CONFIG_ENV)
    if env:
        p = Path(env).expanduser()
        if not p.is_file():
            raise ConfigError(f"${CONFIG_ENV} points at a missing file: {p}")
        return p.resolve()
    p = Path.cwd() / DEFAULT_NAME
    if p.is_file():
        return p.resolve()
    raise ConfigError(
        f"no config found. Copy config.example.toml to {DEFAULT_NAME}, or set "
        f"${CONFIG_ENV}, or pass --config.")


def load(explicit: str | None = None) -> Config:
    path = find_config(explicit)
    with path.open("rb") as fh:
        raw = tomllib.load(fh)
    return Config(path=path, raw=raw)
