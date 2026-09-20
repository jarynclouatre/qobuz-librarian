"""Lyric lookup must not turn a different performance into the studio take.

Stripping 'Live', 'Acoustic' and 'Instrumental' off a track title matched the
studio recording and embedded its words. The duration check cannot catch it:
an instrumental runs the same length as the take it came from.
"""
from qobuz_librarian.integrations import lyric_fetch


def test_performance_markers_survive_and_master_markers_fold():
    keeps = [
        "Wish You Were Here (Live)",
        "Layla (Acoustic)",
        "Blackbird (Demo)",
        "Redemption Song (Alternate Take)",
        "Hurt - Live at Folsom",
        # A suffix can name a different performance and a different master at
        # once; stripping it for the master lost the performance with it.
        "Wish You Were Here (Live Remastered 2009)",
        "Layla (Acoustic - 2011 Remaster)",
    ]
    for title in keeps:
        assert lyric_fetch._clean_title(title) == title, title
    folds = {
        "Come Together (Remastered 2009)": "Come Together",
        "Paranoid Android (Album Version)": "Paranoid Android",
        "God Only Knows (Mono)": "God Only Knows",
        "Bohemian Rhapsody - 2011 Remaster": "Bohemian Rhapsody",
        "Sabotage (Explicit)": "Sabotage",
    }
    for title, expected in folds.items():
        assert lyric_fetch._clean_title(title) == expected, title


def test_instrumental_is_recognised_only_as_a_version_marker():
    for title in ("Clocks (Instrumental)", "Clocks [Instrumental]",
                  "Clocks - Instrumental Version",
                  "Clocks - Instrumental - 2009 Remaster"):
        assert lyric_fetch.is_instrumental(title), title
    for title in ("Instrumental Jealousy", "Clocks"):
        assert not lyric_fetch.is_instrumental(title), title
