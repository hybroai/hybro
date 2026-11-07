"""Instance settings: the Settings page's curated view and edits of config.json."""

from instance_settings.protocols import SettingsAccess, SettingsRequestError
from instance_settings.service import InstanceSettings

__all__ = ["InstanceSettings", "SettingsAccess", "SettingsRequestError"]
