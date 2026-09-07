"""Value shapes shared by Control Plane > Preferences (admin.py's BrandingBody,
global default) and Settings > Profile > Preferences (auth.py's PreferencesBody,
per-user override) so the two schemas can't silently drift apart."""

from typing import Literal

Language = Literal["en", "th"]
FontSize = Literal["small", "medium", "large"]
ChatBackground = Literal["solid", "dots", "grid"]
