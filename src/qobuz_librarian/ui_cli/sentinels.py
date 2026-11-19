"""What the top-level menu and the picker prompts return: a ``Mode``, or one
of the string sentinels below."""
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
    SETTINGS      = "settings"


# Picker sentinels - typed as plain str so existing `is`/`==` checks at
# call sites keep working without forcing every caller to import.
MORE = "__more__"
URL_QUERY = "__url__"
NO_ANSWER = "__no_answer__"
