"""Keep the saved Library, New releases, Upgrade and Downsample views and
their nav dots in step with album changes from the terminal or the web app."""
from pathlib import Path

from qobuz_librarian import config as cfg
from qobuz_librarian import state_file
from qobuz_librarian.library import (
    candidate_premise,
    catalog,
    discovery,
    downsample_state,
    generation_state,
    library_scan_state,
    new_releases,
    scanner,
)
from qobuz_librarian.library import hidden as hidden_mod
from qobuz_librarian.library.tags import VA_NORMALIZED, normalize
from qobuz_librarian.quality import decision as quality_decision
from qobuz_librarian.quality import upgrade_state
from qobuz_librarian.ui_cli.logging import log, vlog
from qobuz_librarian.web import review_badges


def sync_badge(surface):
    """Point the nav dot at whatever that surface's own page would say.

    A saved view the generation authority holds stale shows the user nothing,
    so it has nothing to advertise however full its own store looks.
    """
    if surface == "upgrade":
        state_module = upgrade_state
    elif surface == "downsample":
        state_module = downsample_state
    else:
        return
    if not generation_state.output_is_current(surface):
        review_badges.clear_ready(surface)
        return
    saved = state_module.load()
    review_badges.sync_candidates(
        surface, saved, state_module.visible_candidates(saved))


def refresh_downsample_artist(artist_dir):
    if artist_dir is None:
        return
    result = downsample_state.update_artist(artist_dir)
    if not result.complete:
        err = next(iter(result.errors.values()), "unknown error")
        generation_state.mark_output_status(
            "downsample",
            "stale",
            reason="Downsample could not refresh after a local album change.",
        )
        log.info(f"  downsample view refresh skipped for {artist_dir.name}: {err}")
    sync_badge("downsample")


def forget_upgraded_album(album, album_dir):
    """Drop an album upgraded just before a stop, finished by the next start
    or found to need no upgrade from the saved Upgrade view, and from Library
    when ``album`` is given, without the Qobuz check a finished upgrade
    gets."""
    if (
        album is not None
        and generation_state.output_is_current("library")
        and not library_scan_state.remove_album(album.get("id"))
    ):
        generation_state.mark_output_status(
            "library",
            "stale",
            reason="An upgraded album could not be updated in the saved "
                   "Library view.",
        )
    if (
        not upgrade_state.remove_album_dir(album_dir)
        and generation_state.output_is_current("upgrade")
    ):
        generation_state.mark_output_status(
            "upgrade",
            "stale",
            reason="An upgraded album could not be updated in the saved "
                   "Upgrade view.",
        )
    try:
        sync_badge("upgrade")
    except Exception as exc:
        vlog(f"review dot update failed: {exc}")


def reseal_saved_reviews(fresh_seal):
    """Give each saved Upgrade and Downsample row the seal ``fresh_seal``
    returns for it, if any. A view whose saved results can't be rewritten
    is marked for a refresh, so it neither refuses an album for files the
    app put back nor leaves it out."""
    for surface, state in (("upgrade", upgrade_state),
                           ("downsample", downsample_state)):
        if (not state.reseal(fresh_seal)
                and generation_state.output_is_current(surface)):
            generation_state.mark_output_status(
                surface, "stale",
                reason="The app changed an album's files, and these saved "
                       "results could not be updated.")


def reseal_put_back_album(backup_receipt):
    """Seal again the saved rows over an album a put-back restored byte for
    byte, so the new inodes of a copy don't make the next run refuse it, and
    note it for the web app's own reviews."""
    reseal_saved_reviews(candidate_premise.sealer_after_put_back(backup_receipt))
    note_put_back(backup_receipt)


_PUT_BACK_LIMIT = 50


def _put_backs_file():
    return cfg.DATA_DIR / ".qobuz_put_backs.json"


def note_put_back(backup_receipt):
    """Keep the receipt of an album the terminal put back, with its files as
    they came back.

    A terminal run cannot reach the reviews the web app keeps, such as the
    picks of a web run a hard stop cut short, which still hold the seal from
    before. The web seals them again from these the next time it takes the
    library, and forgets them.
    """
    origin = backup_receipt.get("origin")
    after = candidate_premise.capture("upgrade", origin) if origin else None
    if after is None:
        return
    path = _put_backs_file()
    try:
        with state_file.store_lock(path):
            data = state_file.load_json_object(
                path, "the albums put back from the terminal",
                "which albums the web reviews should seal again") or {}
            notes = [note for note in data.get("put_backs") or ()
                     if type(note) is dict and note.get("origin") != origin]
            notes.append({"origin": origin, "receipt": backup_receipt,
                          "after": after["receipt"]})
            state_file.write_json(
                path, {"put_backs": notes[-_PUT_BACK_LIMIT:]}, indent=None)
    except (OSError, TypeError, ValueError) as exc:
        vlog(f"put-back note for the web app not saved: {exc}")


def take_put_backs():
    """The receipts ``note_put_back`` kept for albums whose files are still
    as they came back, forgetting every note."""
    path = _put_backs_file()
    if not path.exists():
        return []
    try:
        with state_file.store_lock(path):
            data = state_file.load_json_object(
                path, "the albums put back from the terminal",
                "which albums the web reviews should seal again") or {}
            path.unlink(missing_ok=True)
    except (OSError, ValueError) as exc:
        vlog(f"put-back notes from the terminal not read: {exc}")
        return []
    receipts = []
    for note in data.get("put_backs") or ():
        if type(note) is not dict or type(note.get("receipt")) is not dict:
            continue
        current = candidate_premise.capture("upgrade", note.get("origin"))
        if (current is not None and type(note.get("after")) is dict
                and candidate_premise.durable_receipts_match(
                    note["after"], current["receipt"])):
            receipts.append(note["receipt"])
    return receipts


def _artist_dir(change):
    changed_path = change.get("_resolved_post_dir") or change.get("dir")
    try:
        if changed_path:
            album_path = Path(changed_path)
            if album_path.exists():
                return (album_path.parent if album_path.is_dir()
                        else album_path.parent.parent)
        if change.get("album"):
            scanner.clear_scan_caches()
            album_dir = catalog.find_album_dir_filesystem(change["album"])
            if album_dir and album_dir.exists():
                return album_dir.parent
        if change.get("fallback_artist"):
            return discovery.resolve_artist_dir(change["fallback_artist"])
    except Exception as exc:
        vlog(f"saved-view artist folder lookup failed: {exc}")
    return None


def refresh_after_changes(changes, token, args, *, upgrade=True,
                          claims_album=False):
    """Bring the saved views up to date after ``changes``, each a download
    or other album result with its ``album``. With ``claims_album``, an
    imported album also loses its single-track mark.

    Only views that are current are touched, except that the saved Library
    results always drop each exact album, so a refresh derives its artist
    again rather than reuse them. New releases drops each exact album too;
    a change marked ``in_place`` (a repair of the album's own tracks) leaves
    both alone. Upgrade and Downsample scan each changed artist again,
    Upgrade only when ``upgrade`` is set and a token is at hand. A view that
    cannot be brought up to date is marked for a refresh instead, and nothing
    here raises.
    """
    if not changes:
        return
    for change in changes if claims_album else ():
        album = change.get("album") or {}
        if album.get("id") and change.get("imported"):
            try:
                hidden_mod.unmark_single(
                    (album.get("artist") or {}).get("name") or "?",
                    album.get("title") or "?",
                    album_id=album["id"],
                )
            except Exception as exc:
                vlog(f"single-track mark not cleared: {exc}")
    try:
        authority = generation_state.load()
        was_current = {
            surface: generation_state.output_is_current(
                surface, state=authority)
            for surface in generation_state.SURFACES
        }
        reserved = generation_state.reserve_revision() is not None
    except Exception as exc:
        reserved = False
        vlog(f"saved-view revision unavailable: {exc}")
    if not reserved:
        log.warning(
            "  The Library change revision could not be saved after files "
            "changed. Run a Library refresh before using saved results."
        )
        return

    stale = set()
    seen_albums = set()
    artist_dirs = {}
    artist_unknown = False
    scan_upgrade = was_current["upgrade"] and cfg.UPGRADE_SCAN_ENABLED
    for change in changes:
        album = change.get("album") or {}
        album_id = str(album.get("id") or "").strip()
        artist_id = str((album.get("artist") or {}).get("id") or "").strip()
        if (not change.get("in_place")
                and (album_id, artist_id) not in seen_albums):
            seen_albums.add((album_id, artist_id))
            try:
                updated = library_scan_state.remove_album(album_id)
            except Exception as exc:
                updated = False
                vlog(f"saved Library update failed: {exc}")
            if not updated and was_current["library"]:
                stale.add("library")
            if was_current["new_releases"] and album_id:
                try:
                    updated = new_releases.note_owned_album(album)
                except Exception as exc:
                    updated = False
                    vlog(f"saved New releases update failed: {exc}")
                if not updated:
                    stale.add("new_releases")
        if scan_upgrade or was_current["downsample"]:
            artist_dir = _artist_dir(change)
            if artist_dir is None:
                artist_unknown = True
            else:
                artist_dirs.setdefault(str(artist_dir), artist_dir)

    if artist_unknown:
        stale.update(
            surface for surface, scanned in (
                ("upgrade", scan_upgrade),
                ("downsample", was_current["downsample"]),
            ) if scanned
        )
    capped = None
    if scan_upgrade and artist_dirs and "upgrade" not in stale:
        if not upgrade or not token:
            stale.add("upgrade")
        else:
            try:
                capped = quality_decision.load_capped()
            except Exception as exc:
                stale.add("upgrade")
                vlog(f"Upgrade policy unavailable: {exc}")

    for artist_dir in artist_dirs.values():
        # Compilations have no single artist catalogue to compare.
        compilations = normalize(artist_dir.name) in VA_NORMALIZED
        for surface, scanned, update in (
            ("upgrade", scan_upgrade and not compilations,
             lambda d: upgrade_state.update_artist(
                 d, token=token, args=args, capped=capped)),
            ("downsample", was_current["downsample"],
             downsample_state.update_artist),
        ):
            if not scanned or surface in stale:
                continue
            try:
                result = update(artist_dir)
                error = next(iter(result.errors.values()), "incomplete")
                complete = result.complete
            except Exception as exc:
                error, complete = exc, False
            if not complete:
                stale.add(surface)
                vlog(f"saved {surface} view not updated for "
                     f"{artist_dir.name}: {error}")

    if stale:
        try:
            saved = generation_state.invalidate(
                sorted(stale),
                "An album changed, and these saved results could not be "
                "updated to match.",
            )
        except Exception as exc:
            saved = False
            vlog(f"saved-view stale marker failed: {exc}")
        if saved:
            log.warning(
                "  Some saved Library views need a refresh after this album "
                "change."
            )
        else:
            log.warning(
                "  Saved Library state could not be marked stale after files "
                "changed. Run a Library refresh before using saved results."
            )

    try:
        if cfg.UPGRADE_SCAN_ENABLED:
            sync_badge("upgrade")
        else:
            review_badges.clear_ready("upgrade")
        sync_badge("downsample")
    except Exception as exc:
        vlog(f"review dot update failed: {exc}")
