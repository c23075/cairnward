"""Configuration: files, defaults, validation and privacy settings."""

from cairnward.config.loader import Config, ConfigError, load_config
from cairnward.config.paths import Paths, default_paths
from cairnward.config.privacy import PRESETS, SETTINGS, EffectivePrivacy, resolve_privacy

__all__ = [
    "PRESETS",
    "SETTINGS",
    "Config",
    "ConfigError",
    "EffectivePrivacy",
    "Paths",
    "default_paths",
    "load_config",
    "resolve_privacy",
]
