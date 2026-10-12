"""Lyric lookup must not turn a different performance into the studio take.

Stripping 'Live', 'Acoustic' and 'Instrumental' off a track title matched the
studio recording and embedded its words. The duration check cannot catch it:
an instrumental runs the same length as the take it came from.
"""
from qobuz_librarian.integrations import lyric_fetch


def test_lyric_searches_keep_the_performance_a_title_names():
    keeps = [
        "Wish You Were Here (Live)",
        # A suffix can name a different performance and a different master at
        # once; stripping it for the master lost the performance with it.
        "Layla (Acoustic - 2011 Remaster)",
    ]
    for title in keeps:
        assert lyric_fetch._clean_title(title) == title, title
    folds = {
        "Come Together (Remastered 2009)": "Come Together",
        "Bohemian Rhapsody - 2011 Remaster": "Bohemian Rhapsody",
    }
    for title, expected in folds.items():
        assert lyric_fetch._clean_title(title) == expected, title
    # "Instrumental" counts only as a version marker, not inside a title.
    assert lyric_fetch.is_instrumental("Clocks - Instrumental - 2009 Remaster")
    assert not lyric_fetch.is_instrumental("Instrumental Jealousy")
    assert lyric_fetch.is_instrumental("La La (Backing Track)")
    assert lyric_fetch.is_instrumental("Friends (Track - No Vocal)")
    assert lyric_fetch.is_instrumental("Perfect Circle Instrumentals")
    assert not lyric_fetch.is_instrumental("Lady Madonna (Backing Vocals From Take 3)")

    # A missed track is searched again with a plainer title and credit, but
    # still as the same performance.
    class Track:
        tags = {"title": ["Wanksta (feat. G-Unit) (Soundtrack Version)"],
                "artist": ["50 Cent, G-Unit"]}
    assert lyric_fetch.fallback_queries(Track) == ["Wanksta 50 Cent", "Wanksta G-Unit"]
    Track.tags = {"title": ["Know (Live Version)"], "artist": ["System Of A Down, X"]}
    assert lyric_fetch.fallback_queries(Track)[0] == "Know (Live Version) System Of A Down"
