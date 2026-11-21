"""Connection profiles (with credentials) and settings from .dbclidata/config.toml.

Everything dbcli stores lives in .dbclidata: config.toml, the query history and words.db.
"""
from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from .abbrev import DEFAULT_ABBREVIATIONS

# .dbclidata sits in the project root, next to the ./dbcli launcher (this file is src/dbcli/config.py).
DATA_DIR = Path(__file__).resolve().parents[2] / ".dbclidata"
CONFIG_PATH = DATA_DIR / "config.toml"
TYPES = ("sqlite", "postgres", "oracle")


class ConfigError(Exception):
    pass


@dataclass
class Profile:
    name: str
    type: str
    options: dict = field(default_factory=dict)

    def target(self) -> str:
        """Where this profile connects, without secrets (for the top bar)."""
        o = self.options
        if self.type == "sqlite":
            return o.get("path", ":memory:")
        if self.type == "oracle":
            return f"{o.get('user', '?')}@{o.get('dsn', '?')}"
        if "dsn" in o:
            return re.sub(r"(://[^:/@]+):[^@]*@", r"\1@", o["dsn"])  # drop an inline password
        host = f"{o.get('host', 'localhost')}:{o.get('port', 5432)}"
        return f"{o.get('user', '')}@{host}/{o.get('dbname', '')}".lstrip("@")

    def password(self) -> str | None:
        env = self.options.get("password_env")
        if env:
            if env not in os.environ:
                raise ConfigError(f"[{self.name}] environment variable {env} is not set")
            return os.environ[env]
        return self.options.get("password")


PANEL_TABS = ("history", "schema", "trace", "monitor", "bookmark", "ai", "settings")  # Alt-1..7, in this order


@dataclass
class UISettings:
    panel: str = "history"  # which tab of the right panel is shown first
    show_panel: bool = True
    editing_mode: str = "vi"  # vi | emacs  (input area key bindings)
    theme: str = "sky"  # the color theme (themes.THEMES): sky | forest | ember | dusk
    close_page: str = "delete"  # closing a shell page: delete (at once) | confirm (ask when it holds cells)


AI_EFFORTS = ("low", "medium", "high", "xhigh", "max")


@dataclass
class Subscription:
    """[subscriptions.NAME]: an Anthropic API key, with its own model / effort if given."""
    name: str
    api_key: str
    model: str | None = None
    effort: str | None = None


@dataclass
class AISettings:
    """[ai]: the AI tab's model, and which subscription it uses (`subscription = "NAME"`).
    The key is that subscription's, else `api_key` here, else the Anthropic SDK's own lookup
    (ANTHROPIC_API_KEY, an `ant auth login` profile)."""
    model: str = "claude-opus-5-5"
    effort: str = "medium"  # low | medium | high | xhigh | max
    api_key: str | None = None
    subscription: str | None = None  # the active one's name
    subscriptions: dict = field(default_factory=dict)  # name -> Subscription, in file order
    config_path: Path | None = None  # where they were read, so the client can pick one

    @property
    def active(self) -> "Subscription | None":
        return self.subscriptions.get(self.subscription) if self.subscription else None

    def effective(self) -> "AISettings":
        """These settings with the active subscription's key, model and effort applied."""
        sub = self.active
        if sub is None:
            return self
        return AISettings(model=sub.model or self.model, effort=sub.effort or self.effort, api_key=sub.api_key,
                          subscription=self.subscription, subscriptions=self.subscriptions,
                          config_path=self.config_path)

    def has_key(self) -> bool:
        """A key dbcli can see: the active subscription's, [ai] api_key or ANTHROPIC_API_KEY.
        (An `ant auth login` profile may also work, but is not checked here.)"""
        return bool(self.effective().api_key or os.environ.get("ANTHROPIC_API_KEY"))


@dataclass
class Config:
    profiles: list[Profile]
    abbreviations: dict[str, str]
    ui: UISettings = field(default_factory=UISettings)
    templates: dict[str, str] = field(default_factory=dict)
    ai: AISettings = field(default_factory=AISettings)


def load(path: Path = CONFIG_PATH) -> Config:
    data = tomllib.loads(path.read_text()) if path.exists() else {}
    profiles = []
    for name, opts in data.get("connections", {}).items():
        opts = dict(opts)
        kind = opts.pop("type", None)
        if kind not in TYPES:
            raise ConfigError(f"[connections.{name}] type must be one of {', '.join(TYPES)}")
        profiles.append(Profile(name, kind, opts))
    profiles.reverse()  # newest (lowest in the file) first: the first profile is the default connection
    ui_data = data.get("ui", {})
    panel = ui_data.get("panel", "history")
    if panel == "connections":  # that tab is gone: one terminal runs one connection
        panel = "history"
    if panel == "navigator":  # now the schema browser
        panel = "schema"
    if panel == "template":  # now the bookmarks (those with an @alias are the templates)
        panel = "bookmark"
    ui = UISettings(panel=panel,
                    show_panel=bool(ui_data.get("show_panel", True)),
                    editing_mode=ui_data.get("editing_mode", "vi"),
                    theme=str(ui_data.get("theme", "sky")),
                    close_page=str(ui_data.get("close_page", "delete")))
    if ui.editing_mode not in ("vi", "emacs"):
        raise ConfigError('[ui] editing_mode must be "vi" or "emacs"')
    if ui.close_page not in ("delete", "confirm"):
        raise ConfigError('[ui] close_page must be "delete" or "confirm"')
    from .themes import THEMES
    if ui.theme not in THEMES:
        raise ConfigError(f"[ui] theme must be one of {', '.join(THEMES)}")
    if ui.panel not in PANEL_TABS:
        raise ConfigError(f"[ui] panel must be one of {', '.join(PANEL_TABS)}")
    templates = data.get("templates", {})
    for name, body in templates.items():
        if not isinstance(body, str) or not re.fullmatch(r"[A-Za-z_][\w-]*", name):
            raise ConfigError(f"[templates] {name}: the name takes letters, digits, _ and -, the value is an SQL string")
    ai_data = data.get("ai", {})
    subs = {}
    for name, opts in data.get("subscriptions", {}).items():
        if not isinstance(opts, dict) or not isinstance(opts.get("api_key"), str) or not opts["api_key"]:
            raise ConfigError(f"[subscriptions.{name}] needs api_key = \"sk-ant-…\"")
        if opts.get("effort") is not None and opts["effort"] not in AI_EFFORTS:
            raise ConfigError(f"[subscriptions.{name}] effort must be one of {', '.join(AI_EFFORTS)}")
        subs[name] = Subscription(name, opts["api_key"], opts.get("model"), opts.get("effort"))
    ai = AISettings(model=str(ai_data.get("model", AISettings.model)),
                    effort=str(ai_data.get("effort", AISettings.effort)),
                    api_key=ai_data.get("api_key") or None,
                    subscription=ai_data.get("subscription") or None,
                    subscriptions=subs, config_path=path)
    if ai.effort not in AI_EFFORTS:
        raise ConfigError(f"[ai] effort must be one of {', '.join(AI_EFFORTS)}")
    return Config(profiles, {**DEFAULT_ABBREVIATIONS, **data.get("abbreviations", {})}, ui, templates, ai)
