"""
Shared type aliases.

Both :class:`~bb_sapi.client.SapiClient` and
:class:`~bb_sapi.entities.mediaclip.MediaClip` define a method named ``list``,
which shadows the builtin inside those class bodies — so annotations there
cannot spell ``list[...]`` directly. These aliases are resolved here, at module
level, where the builtin is still the builtin.
"""
from __future__ import annotations

from typing import Any

JsonDict = dict[str, Any]
JsonDicts = list[JsonDict]
StrList = list[str]

__all__ = ["JsonDict", "JsonDicts", "StrList"]
