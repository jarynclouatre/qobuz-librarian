"""String sentinels used to communicate intent across prompt boundaries.

The picker prompts and the top-level menu return these so callers can branch
without comparing magic literals scattered through every consumer file.
"""
from enum import Enum


class Mode(str, Enum):
    QUIT          = "quit"
    ALBUM         = "album"
    ARTIST        = "artist"
    WALK_QUEUE    = "walk_queue"
    ALBUM_WALK    = "album_walk"
    ALBUM_REPAIR  = "album_repair"
    UPGRADE       = "upgrade"
    MIGRATE       = "migrate"
    DOWNSAMPLE    = "downsample"
    LYRICS        = "lyrics"


# Picker sentinels - typed as plain str so existing `is`/`==` checks at
# call sites keep working without forcing every caller to import.
MORE = "__more__"
URL_QUERY = "__url__"
