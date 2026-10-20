"""Run streamrip's `rip` command with room for Qobuz's longer replies."""
import http.client
import sys

# Some Qobuz file servers answer with more than the 100 headers http.client
# accepts, and the track then arrives empty on every try.
http.client._MAXHEADERS = 1000

from streamrip.rip import rip  # noqa: E402

if __name__ == "__main__":
    sys.argv[0] = "rip"
    rip()
