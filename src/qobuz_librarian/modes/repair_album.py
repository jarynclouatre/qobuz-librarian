"""Whole-album Repair for tracks that cannot be refilled by recording ID."""
import contextlib
from pathlib import Path

from qobuz_librarian import repair_log
from qobuz_librarian.api import client as api_client
from qobuz_librarian.api.auth import QobuzAccess
from qobuz_librarian.api.search import get_album
from qobuz_librarian.integrations import beets as beets_mod
from qobuz_librarian.library import backup as backup_mod
from qobuz_librarian.library.scanner import read_album_dir
from qobuz_librarian.modes import process as process_mode
from qobuz_librarian.modes.repair import RepairRecovery, RepairRecoveryRequired
from qobuz_librarian.ui_cli.errors import plural
from qobuz_librarian.ui_cli.logging import log


def redownload_damaged_album(payload, token, args, *, recovery_checkpoint=None,
                             expected_album_receipt=None, staging_lock=contextlib.nullcontext):
    return _RedownloadRun(payload, token, args, recovery_checkpoint,
                          expected_album_receipt, staging_lock).run()


class _RedownloadRun:
    """One whole-album Repair re-download, its original held in a backup until checked."""

    def __init__(self, payload, token, args, recovery_checkpoint, expected_album_receipt, staging_lock):
        self.args = args
        self.staging_lock = staging_lock
        self.payload = payload
        self.damaged_files = [d["file"] for d in payload.get("damaged") or []]
        self.token = token
        self.recovery_checkpoint = recovery_checkpoint
        self.expected_album_receipt = expected_album_receipt

    def run(self):
        refused = self._set_aside()
        if refused is not None:
            return refused
        refused = self._save_backup_record()
        if refused is not None:
            return refused
        self._download()
        if self.backup:
            try:
                self._settle_backup()
            except BaseException as exc:
                self._stopped_after_landing(exc)
                raise
        return self.result

    def _set_aside(self):
        log.info("  The damaged file can't be verified by its ID, so the whole "
                 "album is being re-downloaded fresh from Qobuz.")
        self.full = get_album(self.payload["album_id"], self.token)
        self.album_dir = Path(self.payload["album_dir"])
        self.catalogue_snapshot = beets_mod.capture_beets_album_entries(self.album_dir) \
            if self.album_dir.exists() else None
        if self.album_dir.exists() and self.catalogue_snapshot is None:
            log.info("  Couldn't identify this album's Beets entries; "
                     "leaving Repair alone.")
            return {"imported": False, "n_ok": 0,
                    "result": "catalogue_snapshot_failed"}
        expected_generation = getattr(self.token, "credential_generation", "")
        if expected_generation:
            self.token = api_client.authorize_qobuz_action(
                QobuzAccess.DOWNLOAD_ACTION,
                expected_generation=expected_generation,
            ).token
        self.backup = None
        if self.album_dir.exists():
            self.backup = (
                backup_mod.backup_album_dir(
                    self.album_dir,
                    expected_receipt=self.expected_album_receipt,
                )
                if self.expected_album_receipt is not None
                else backup_mod.backup_album_dir(self.album_dir)
            )
        if self.backup is not None and not self.backup.complete:
            process_mode._recover_incomplete_upgrade_backup(
                self.backup, self.album_dir, operation="repair backup")
            log.info("  The backup was interrupted, so the repair was stopped. "
                     "See the recovery message above.")
            return {"imported": False, "n_ok": 0, "result": "backup_failed"}
        if self.backup is None and (self.expected_album_receipt is not None
                                    or self.album_dir.exists()):
            log.info("  Couldn't move the existing folder aside; left this album "
                     "alone. See the log above.")
            return {"imported": False, "n_ok": 0, "result": "backup_failed"}

    def _pin_repair_recovery(self, note):
        if (self.backup is not None and self.backup.exists()
                and not backup_mod.pin_unverified_upgrade_backup(self.backup, note)):
            backup_mod.warn_pin_failed(self.backup)

    def _checkpoint_recovery(self, stage, reason, *, retained=True, required=False):
        recovery = RepairRecovery(
            backup=self.backup,
            album_dir=self.album_dir,
            stage=stage,
            reason=reason,
            retained=retained,
        )
        if self.recovery_checkpoint is None:
            return recovery, True
        try:
            persisted = self.recovery_checkpoint(recovery) is True
        except Exception as exc:
            persisted = False
            log.info(f"  Couldn't save the Repair recovery record: {exc}")
        if required and not persisted:
            log.info("  Repair stopped before downloading the replacement "
                     "because its recovery record could not be saved.")
        return recovery, persisted

    def _stopped_after_landing(self, exc):
        # A stop once the replacement reached the library: the original
        # stays in its backup until the new album is checked, and Beets
        # drops the entries the new files replaced at their paths.
        if self.backup is None or not self.backup.exists() or not self.album_dir.exists():
            return
        self._pin_repair_recovery("repair backup kept, re-download was interrupted")
        if not backup_mod.pending_repair_import(self.backup):
            with contextlib.suppress(KeyboardInterrupt):
                beets_mod.forget_replaced_beets_entries(folder=self.album_dir)
        recovery, _ = self._checkpoint_recovery(
            "refill",
            "Repair stopped after the replacement reached the library, "
            "before it was checked.",
        )
        raise RepairRecoveryRequired(recovery, exc) from exc

    def _save_backup_record(self):
        if self.backup is not None:
            _recovery, recovery_saved = self._checkpoint_recovery(
                "backup",
                "The original album is held while its replacement is downloaded.",
                required=True,
            )
            if not recovery_saved:
                if backup_mod.restore_failed_album_repair(self.backup, self.album_dir):
                    self._checkpoint_recovery(
                        "resolved",
                        "The original album was restored before Repair stopped.",
                        retained=False,
                    )
                    return {
                        "imported": False,
                        "n_ok": 0,
                        "result": "recovery_record_failed",
                    }
                self._pin_repair_recovery(
                    "repair backup kept, recovery record and automatic restore "
                    "did not complete")
                recovery, _ = self._checkpoint_recovery(
                    "restore",
                    "The recovery record could not be saved and automatic "
                    "restoration did not complete.",
                )
                cause = OSError("Repair recovery record could not be saved")
                raise RepairRecoveryRequired(recovery, cause) from cause

    def _download(self):
        try:
            with self.staging_lock():
                self.result = process_mode.process_album(
                    self.full, self.args, allow_force=False, already_confirmed=True,
                    token=self.token, cover_backup=self.backup,
                    rebuild_dir=self.album_dir if self.backup is not None else None,
                    repair_damaged=self.damaged_files) or {}
        except Exception as exc:
            if self.backup:
                if backup_mod.restore_failed_album_repair(self.backup, self.album_dir):
                    self._checkpoint_recovery(
                        "resolved",
                        "The original album was restored after the replacement "
                        "download stopped.",
                        retained=False,
                    )
                else:
                    self._pin_repair_recovery(
                        "repair backup kept, automatic restore after an error did "
                        "not complete")
                    recovery, _ = self._checkpoint_recovery(
                        "restore",
                        "The replacement download stopped and automatic "
                        "restoration did not complete.",
                    )
                    raise RepairRecoveryRequired(recovery, exc) from exc
            raise
        except BaseException as exc:
            self._stopped_after_landing(exc)
            raise
        self.imported_ok = bool(self.result.get("imported")) and self.result.get("n_ok", 0) > 0

    def _settle_backup(self):
        kept = self.result.get("repair_kept", [])
        if self.imported_ok and process_mode._upgrade_replacement_verified(
                self.full, self.album_dir, self.backup, self.damaged_files):
            # Carry useful companions, then retire the original's backup when
            # every file it holds is verifiably superseded in the new album.
            carried = process_mode._carry_non_audio_from_backup(
                self.full, self.album_dir, self.backup, replacement_dir=self.album_dir,
                damaged=self.damaged_files, **({"kept": kept} if kept else {}))
            if carried is not None:
                self._retire_original(carried)
            else:
                self.result["repair_unverified"] = True
                self._pin_repair_recovery(
                    "repair backup kept, replacement not confirmed written to disk")
                log.info("  Re-download landed, but the rebuilt album or its "
                         "companions couldn't be confirmed written to disk; keeping your "
                         f"backup at {self.backup}.")
                self._checkpoint_recovery(
                    "verification",
                    "The replacement landed, but its album or companion files "
                    "could not be confirmed written to disk.",
                )
        elif self.imported_ok:
            # Imported, but a decode pass alone doesn't prove the re-rip kept
            # every track, a truncated or short result could be WORSE than
            # the damaged original it replaced.
            self.result["repair_unverified"] = True
            self._pin_repair_recovery(
                "repair backup kept, replacement could not be verified "
                "complete")
            # A track the replacement lacks keeps its entry, and Restore
            # needs it.
            if not kept and not backup_mod.pending_repair_import(self.backup):
                beets_mod.forget_replaced_beets_entries(folder=self.album_dir)
            log.info("  Re-download landed but couldn't be verified as complete "
                     f"as the original; keeping your backup at {self.backup}.")
            self._checkpoint_recovery(
                "verification",
                "The replacement landed but could not be verified as complete "
                "as the original album.",
            )
        else:
            log.info("  Re-download didn't complete. Restoring the original "
                     "album folder.")
            if backup_mod.restore_failed_album_repair(self.backup, self.album_dir):
                self._checkpoint_recovery(
                    "resolved",
                    "The replacement did not complete, so the original album "
                    "was restored.",
                    retained=False,
                )
            else:
                self._pin_repair_recovery(
                    "repair backup kept, automatic restore did not complete")
                self._checkpoint_recovery(
                    "restore",
                    "The replacement did not complete and automatic "
                    "restoration was incomplete.",
                )

    def _retire_original(self, carried):
        replacement_path, replacement_receipt, left = carried
        originals = read_album_dir(Path(self.backup), walk_errors=[])
        kept = self.result.get("repair_kept", [])
        if any(replacement_receipt["tree"]["files"].get(path, {}).get("sha256")
               != self.backup.receipt["tree"]["files"][path]["sha256"] for path in kept):
            reason = "A healthy original changed before Repair finished."
            self.result["repair_unverified"] = True
            self._pin_repair_recovery(reason)
            self._checkpoint_recovery("verification", reason)
            log.warning(f"  {reason} Keeping the backup at {self.backup}.")
            return
        if not beets_mod.retire_replaced_beets_entries(
            self.catalogue_snapshot,
            replacement_path,
            process_mode._replacement_audio_paths(
                replacement_path,
                replacement_receipt,
            ),
            **({"kept_paths": [replacement_path / path for path in kept],
                "repair_token": self.backup.receipt["token"]} if kept else {}),
        ):
            self.result["repair_unverified"] = True
            self._pin_repair_recovery(
                "repair backup kept; the old Beets entries "
                "could not be removed")
            log.info("  Re-download verified, but the old "
                     "Beets entries couldn't be removed; "
                     f"keeping the backup at {self.backup}.")
            self._checkpoint_recovery(
                "verification",
                "The re-download verified, but the old Beets "
                "entries could not be removed.",
            )
        elif left:
            self.result["companions_kept"] = left
            log.info(process_mode._keep_backup_for_companions(
                self.backup, left))
            one = len(left) == 1
            self._checkpoint_recovery(
                "verification",
                "The re-download verified. The original album's "
                f"{', '.join(left)} {'is' if one else 'are'} not in the new "
                f"album, so {'it is' if one else 'they are'} kept here.",
            )
        elif backup_mod.retire_rebuilt_album(
                self.backup, replacement_path, replacement_receipt, self.damaged_files):
            self._checkpoint_recovery(
                "resolved",
                "The re-download verified, so the original album's "
                "backup was removed.",
                retained=False,
            )
            log.info("  Re-download verified; removed the original "
                     "album's backup.")
        else:
            current = backup_mod.capture_album_tree_identity(replacement_path)
            if current is None or current != backup_mod.album_tree_identity(replacement_receipt):
                self.result["repair_unverified"] = True
                reason = "The replacement changed or could not be read after verification."
            else:
                reason = ("The re-download verified, but the original album's "
                          "backup could not be proven redundant.")
            self._pin_repair_recovery(reason)
            log.info(f"  {reason} Keeping the backup at {self.backup}.")
            self._checkpoint_recovery("verification", reason)
        if not self.result.get("repair_unverified"):
            if kept:
                log.info(f"  Kept {plural(len(kept), 'healthy original')} at the same quality.")
            repair_log.append_repair_log([
                {"artist": self.payload["artist_name"],
                 "album": self.album_dir.name, "title": track["title"]}
                for track in originals
                if not kept or Path(track["path"]).relative_to(self.backup).as_posix() not in kept])
