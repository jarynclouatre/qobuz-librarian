"""CLI equivalent of the web Settings page's Behaviour form.

Saves through settings_store.save(), the same writer the web page and the
downsample walk's keep-originals prompt already call, so a CLI-only
deployment can set download quality, downsample policy and the behaviour
toggles without ever opening the web UI.
"""
from qobuz_librarian.ui_cli.ask import ask
from qobuz_librarian.ui_cli.colors import C, banner, fmt
from qobuz_librarian.ui_cli.logging import log
from qobuz_librarian.ui_cli.prompts import confirm
from qobuz_librarian.web import settings_store

_QUALITY_CHOICES = ["4", "3", "2"]


def _pick_quality(current_value):
    labels = settings_store.ENUM_OPTION_LABELS["STREAMRIP_QUALITY"]
    print()
    log.info(fmt(C.WHITE, "  Download quality:"))
    for i, choice in enumerate(_QUALITY_CHOICES, 1):
        marker = " (current)" if choice == current_value else ""
        log.info(fmt(C.GRAY, f"    {i}) {labels[choice]}{marker}"))
    current_label = labels.get(current_value, "current")
    r = ask(f"  Choice (Enter = keep {current_label}): ")
    if r is None:
        return None
    if r == "":
        return current_value
    if r in ("1", "2", "3"):
        return _QUALITY_CHOICES[int(r) - 1]
    log.info(fmt(C.GRAY, "  Not a listed choice; quality left unchanged."))
    return current_value


def _pick_downsample_policy(current_value):
    if current_value in ("keep", "delete"):
        default_yes = current_value == "keep"
        note = f" (current: {current_value})"
    else:
        default_yes = True
        note = " (not chosen yet)"
    keep = confirm(
        f"  Keep restorable backups when downsampling?{note}",
        default_yes=default_yes, auto_yes=False, on_eof=None)
    if keep is None:
        return current_value
    return "keep" if keep else "delete"


def _show_current(values):
    log.info(fmt(C.GRAY, "  Current values:"))
    quality_label = settings_store.ENUM_OPTION_LABELS["STREAMRIP_QUALITY"].get(
        values.get("STREAMRIP_QUALITY"), "?")
    log.info(fmt(C.GRAY, f"    Download quality: {quality_label}"))
    policy = values.get("DOWNSAMPLE_KEEP_ORIGINALS") or "not chosen yet"
    log.info(fmt(C.GRAY, f"    Downsample policy: {policy}"))
    for key, label, _ in settings_store.BEHAVIOR_FIELDS:
        state = "on" if values.get(key) else "off"
        log.info(fmt(C.GRAY, f"    {label}: {state}"))


def run_settings_mode(args):
    """Set download quality, downsample policy and behaviour toggles from
    the terminal, saved the same way the web Settings page saves them."""
    banner("Settings: quality, downsample policy, behaviour toggles")
    values = settings_store.current()

    if getattr(args, "dry_run", False):
        _show_current(values)
        return 0

    changes = {}

    quality = _pick_quality(values.get("STREAMRIP_QUALITY"))
    if quality is None:
        log.info(fmt(C.GRAY, "\n  Cancelled; nothing changed."))
        return 0
    if quality != values.get("STREAMRIP_QUALITY"):
        changes["STREAMRIP_QUALITY"] = quality

    policy = _pick_downsample_policy(values.get("DOWNSAMPLE_KEEP_ORIGINALS"))
    if policy != values.get("DOWNSAMPLE_KEEP_ORIGINALS"):
        changes["DOWNSAMPLE_KEEP_ORIGINALS"] = policy

    print()
    log.info(fmt(C.WHITE, "  Behaviour toggles (Enter keeps the current value):"))
    for key, label, help_text in settings_store.BEHAVIOR_FIELDS:
        current_on = bool(values.get(key))
        answer = confirm(f"    {label} - {help_text}",
                         default_yes=current_on, auto_yes=False, on_eof=None)
        if answer is None:
            continue
        if answer != current_on:
            changes[key] = answer

    if not changes:
        log.info(fmt(C.GRAY, "\n  Nothing changed."))
        return 0

    ok, warnings = settings_store.save(changes)
    print()
    if ok is None:
        log.warning(fmt(C.RED,
            f"  ✗  {warnings[0] if warnings else 'Invalid setting.'}"))
        return 1
    if ok is False:
        log.warning(fmt(C.RED,
            "  ✗  Couldn't save; the settings file could not be written."))
        return 1
    for w in warnings:
        log.info(fmt(C.YELLOW, f"  ⚠  {w}"))
    log.info(fmt(C.GREEN, "  ✓ Saved."))
    return 0
