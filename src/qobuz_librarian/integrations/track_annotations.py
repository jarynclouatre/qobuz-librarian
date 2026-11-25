"""Read the original track's annotations before replacing it with FLAC."""
import base64

import mutagen
from mutagen.apev2 import APENoHeaderError, APEv2
from mutagen.flac import Picture
from mutagen.id3 import ID3
from mutagen.mp3 import MP3
from mutagen.mp4 import MP4, AtomDataType, MP4Cover
from mutagen.oggopus import OggOpus
from mutagen.oggvorbis import OggVorbis

# Recording, release and encoder fields supplied by the replacement.
_ID3_RELEASE_FIELDS = frozenset("""
    TALB TBPM TCMP TCOM TCON TCOP TDOR TDRC TENC TEXT TFLT TIT2 TIT3
    TKEY TLAN TLEN TMED TPE1 TPE2 TPE3 TPE4 TPOS TPUB TRCK TSIZ TSO2
    TSOA TSOC TSOP TSOT TSRC TSSE
""".split())
_MP4_RELEASE_FIELDS = frozenset("""
    ©nam ©ART ©alb aART ©day ©gen gnre trkn disk ©wrt ©too ©enc
    cprt cpil tmpo rtng soaa soal soar soco sonm
""".split())


def _picture(data, mime, kind=3, description=""):
    if not data:
        raise ValueError("Empty embedded artwork")
    picture = Picture()
    picture.data, picture.mime = data, mime
    picture.type, picture.desc = kind, description
    return picture


def _id3_annotations(path, frames):
    # The MP3 reader ignores APE tags, and its decoded ID3 view drops junk
    # frames. Keep the original when the readable tags do not account for it.
    try:
        APEv2(path)
    except APENoHeaderError:
        pass
    else:
        raise ValueError("Additional APE tags on the MP3")
    with open(path, "rb") as file:
        file.seek(-128, 2)
        if file.read(3) == b"TAG":
            raise ValueError("Additional ID3v1 tags on the MP3")
    raw = ID3(path, known_frames={}, translate=False, load_v1=False)
    if frames is None or frames.unknown_frames or len(raw.unknown_frames) != len(frames):
        raise ValueError("Unrecognized ID3 frames")
    if len(frames.getall("COMM")) > 1 or len(frames.getall("USLT")) > 1:
        raise ValueError("Multiple ID3 comment or lyric versions")
    tags, pictures = [], []
    for frame in frames.values():
        key = frame.FrameID
        if key in _ID3_RELEASE_FIELDS:
            continue
        if key == "APIC":
            pictures.append(_picture(frame.data, frame.mime, frame.type, frame.desc))
        elif key == "TXXX" and frame.desc:
            tags.extend((frame.desc, text) for text in frame.text)
        elif key == "USLT" and not frame.desc:
            tags.append(("lyrics", frame.text))
        elif key == "COMM" and not frame.desc:
            tags.extend(("comment", text) for text in frame.text)
        elif key in ("TIT1", "TMOO"):
            tags.extend(("grouping" if key == "TIT1" else "mood", text) for text in frame.text)
        else:
            raise ValueError(f"Cannot carry ID3 frame {key}")
    return tags, pictures


def _mp4_annotations(atoms):
    if atoms._failed_atoms:
        raise ValueError("Unreadable MP4 metadata")
    tags, pictures = [], []
    for key, values in atoms.items():
        if key in _MP4_RELEASE_FIELDS:
            continue
        if key == "covr":
            for cover in values:
                mime = {MP4Cover.FORMAT_JPEG: "image/jpeg",
                        MP4Cover.FORMAT_PNG: "image/png"}[cover.imageformat]
                pictures.append(_picture(bytes(cover), mime))
        elif key in ("©cmt", "©grp", "©lyr"):
            name = {"©cmt": "comment", "©grp": "grouping", "©lyr": "lyrics"}[key]
            tags.extend((name, text) for text in values)
        elif key.startswith("----:com.apple.iTunes:"):
            name = key.split(":", 2)[2]
            for value in values:
                if value.dataformat != AtomDataType.UTF8:
                    raise ValueError(f"Cannot carry non-text MP4 metadata {name}")
                tags.append((name, value.decode("utf-8")))
        else:
            raise ValueError(f"Cannot carry MP4 metadata {key}")
    return tags, pictures


def _vorbis_annotations(comments, *, opus):
    tags, pictures = [], []
    # Invalid comments can be dropped or decoded with replacement characters.
    if (len(comments.write(framing=not opus)) != comments._size
            or getattr(comments, "_pad_data", b"")
            or any("\ufffd" in value for _, value in comments)):
        raise ValueError("Unreadable Vorbis comments")
    covers = comments.get("coverart", [])
    mimes = comments.get("coverartmime", [])
    if mimes and len(mimes) != len(covers):
        raise ValueError("Unmatched Vorbis cover art and MIME types")
    for i, cover in enumerate(covers):
        pictures.append(_picture(base64.b64decode(cover, validate=True),
                                 mimes[i] if mimes else ""))
    for key, value in comments:
        if key.lower() == "metadata_block_picture":
            picture = Picture(base64.b64decode(value, validate=True))
            if not picture.data:
                raise ValueError("Empty embedded artwork")
            pictures.append(picture)
        elif key.lower() not in ("coverart", "coverartmime"):
            tags.append((key, value))
    return tags, pictures


def read(path):
    source = mutagen.File(path)
    if isinstance(source, MP3):
        return _id3_annotations(path, source.tags)
    if isinstance(source, MP4) and source.tags is not None:
        return _mp4_annotations(source.tags)
    if isinstance(source, (OggVorbis, OggOpus)) and source.tags is not None:
        return _vorbis_annotations(source.tags, opus=isinstance(source, OggOpus))
    raise ValueError("Cannot read the original track's tags")
