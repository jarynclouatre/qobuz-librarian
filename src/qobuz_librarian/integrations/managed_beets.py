"""Launch a managed Beets import only when its ownership gate is armed."""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path

_MODE_ENV = "QOBUZ_LIBRARIAN_OWNERSHIP_MODE"
_NONCE_ENV = "QOBUZ_LIBRARIAN_OWNERSHIP_NONCE"
_MANAGED_MODE = "managed-v3"
_PLUGIN_MODULE = "beetsplug.qobuz_ownership"
_SUPPORTED_BEETS_VERSION = "2.12.0"
_CONFIG_PROTOCOL_VERSION = 1


def _valid_invocation(args):
    return (
        len(args) >= 4
        and args[0] == "-c"
        and args[1].startswith("/proc/self/fd/")
        and args[1][len("/proc/self/fd/"):].isdecimal()
        and args[2] == "import"
        and all(isinstance(value, str) and value for value in args[3:])
    )


def _inspect_config():
    import beets
    from beets import IncludeLazyConfig
    from confuse import ConfigReadError, NotFoundError

    config_dir = os.environ.get("BEETSDIR", "")
    if (
        beets.__version__ != _SUPPORTED_BEETS_VERSION
        or sys.version_info < (3, 12)
        or not config_dir
        or not os.path.isabs(config_dir)
    ):
        raise RuntimeError("unsupported Beets runtime")
    resolved = IncludeLazyConfig("beets", "beets")
    resolved.read(user=False, defaults=True)
    resolved.set_file(
        Path(config_dir) / "config.yaml",
        base_for_paths=True,
    )
    try:
        for include in resolved["include"].sequence():
            resolved.set_file(include.as_filename())
    except NotFoundError:
        pass
    except ConfigReadError as exc:
        raise RuntimeError("Beets config could not be read") from exc
    resolved.add({"disabled_plugins": []})
    payload = {
        "version": _CONFIG_PROTOCOL_VERSION,
        "beets_version": beets.__version__,
        "python": [sys.version_info.major, sys.version_info.minor],
        "plugins": [str(value) for value in resolved["plugins"].as_str_seq()],
        "plugin_paths": [
            str(value)
            for value in resolved["pluginpath"].as_str_seq(split=False)
        ],
        "disabled": [
            str(value)
            for value in resolved["disabled_plugins"].as_str_seq()
        ],
        "musicbrainz_enabled": (
            resolved["musicbrainz"].flatten().get("enabled")
        ),
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    if len(encoded) > 64 * 1024:
        raise RuntimeError("Beets config response is too large")
    sys.stdout.buffer.write(encoded + b"\n")


def _install_qobuz_package():
    """Load this exact package without exposing its whole environment."""
    package_dir = Path(os.path.abspath(__file__)).parents[1]
    package_init = package_dir / "__init__.py"
    existing = sys.modules.get("qobuz_librarian")
    if existing is not None:
        try:
            if os.path.samefile(existing.__file__, package_init):
                return
        except (AttributeError, OSError, TypeError, ValueError):
            pass
        raise RuntimeError("another Qobuz Librarian package is already loaded")
    spec = importlib.util.spec_from_file_location(
        "qobuz_librarian",
        package_init,
        submodule_search_locations=[str(package_dir)],
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("Qobuz Librarian package could not be loaded")
    module = importlib.util.module_from_spec(spec)
    sys.modules["qobuz_librarian"] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        if sys.modules.get("qobuz_librarian") is module:
            del sys.modules["qobuz_librarian"]
        raise


def main(args=None):
    values = list(sys.argv[1:] if args is None else args)
    if values == ["--inspect-config"]:
        _inspect_config()
        return

    _install_qobuz_package()
    import beets
    from beets.ui import UserError
    from beets.ui import main as beets_main

    if values[:1] == ["--run-beets"]:
        if beets.__version__ != _SUPPORTED_BEETS_VERSION or sys.version_info < (3, 12):
            raise UserError("unsupported Beets runtime")
        beets_main(values[1:])
        return

    from beets import plugins

    nonce = os.environ.get(_NONCE_ENV, "")
    if (
        beets.__version__ != _SUPPORTED_BEETS_VERSION
        or sys.version_info < (3, 12)
        or os.environ.get(_MODE_ENV) != _MANAGED_MODE
        or len(nonce) != 64
        or any(value not in "0123456789abcdef" for value in nonce)
        or not _valid_invocation(values)
    ):
        raise UserError("invalid managed Beets invocation")

    expected_source = (
        Path(__file__).with_name("beets_plugins") / "qobuz_ownership.py"
    )
    original_load = plugins.load_plugins
    armed = False

    def load_required_plugins():
        nonlocal armed
        original_load()
        loaded = list(plugins.find_plugins())
        matches = [
            plugin
            for plugin in loaded
            if plugin.__class__.__module__ == _PLUGIN_MODULE
        ]
        try:
            expected_module = sys.modules[_PLUGIN_MODULE]
            source = Path(expected_module.__file__)
            correct_source = os.path.samefile(source, expected_source)
            correct_class = (
                len(matches) == 1
                and matches[0].__class__
                is getattr(expected_module, "QobuzOwnershipPlugin", None)
            )
        except (AttributeError, KeyError, OSError, TypeError, ValueError):
            correct_source = False
            correct_class = False
        arm = getattr(matches[0], "arm_managed_import", None) \
            if len(matches) == 1 else None
        if (
            armed
            or len(matches) != 1
            or not loaded
            or loaded[-1] is not matches[0]
            or not correct_source
            or not correct_class
            or not callable(arm)
            or arm(nonce) is not True
        ):
            raise UserError("managed Beets ownership gate is unavailable")
        armed = True

    plugins.load_plugins = load_required_plugins
    try:
        beets_main(values)
    finally:
        plugins.load_plugins = original_load


if __name__ == "__main__":
    main()
