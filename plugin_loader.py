"""
Plugin loader for pc98mount.

Discovers and imports Python modules from the ``plugins/`` directory
next to the main program.  Each plugin is a ``.py`` file (or a package
directory with ``__init__.py``) that registers its image formats,
partition detectors, or filesystem probers via ``registry.py``.

Plugin state (enabled / disabled) is persisted in ``98Bridge.config``
next to the main program.  Core plugins cannot be disabled.

Plugin lifecycle
----------------
::

    load_plugins()                 # initial scan at startup
    install_plugin_file(path)      # copy a .py into plugins/
    unload_plugin(mod_name)        # remove registrations + sys.modules
    reload_plugin(mod_name)        # unload → re-import from disk
    reload_all_plugins()           # unload everything → full rescan
    remove_plugin(mod_name)        # unload + delete the file from disk
    disable_plugin(name)           # unload + mark disabled in config
    enable_plugin(name)            # load + mark enabled in config
"""

from __future__ import annotations

import importlib
import importlib.util
import json
import logging
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

import registry as _registry

log = logging.getLogger("pc98mount.plugins")


# ── Internal bookkeeping ────────────────────────────────────────

@dataclass
class PluginInfo:
    """Metadata about one loaded or discovered plugin."""
    module_name: str      # e.g. "pc98mount_plugin_nhd_format"
    display_name: str     # e.g. "nhd_format"
    file_path: Path       # absolute path to the .py file or __init__.py
    is_package: bool      # True if the plugin is a directory package
    builtin: bool = False # True for core plugins (not removable/disableable)
    enabled: bool = True  # False if the user disabled this plugin

_loaded: Dict[str, PluginInfo] = {}
_discovered: Dict[str, PluginInfo] = {}   # all found on disk (loaded + disabled)


# ── Configuration ───────────────────────────────────────────────

_CONFIG_NAME = "98Bridge.config"


def _config_path() -> Path:
    return Path(__file__).resolve().parent / _CONFIG_NAME


def _load_config() -> dict:
    """Load the config file, returning a dict.  Returns empty dict
    if the file doesn't exist or is invalid."""
    path = _config_path()
    if not path.is_file():
        return {}
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as exc:
        log.warning(f"Could not read config {path}: {exc}")
        return {}


def _save_config(cfg: dict) -> None:
    """Write the config dict to disk as formatted JSON."""
    path = _config_path()
    try:
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(cfg, f, indent=2, ensure_ascii=False)
            f.write('\n')
        log.debug(f"Config saved to {path}")
    except OSError as exc:
        log.error(f"Could not save config {path}: {exc}")


def _get_disabled_set() -> set:
    """Return the set of display-names that are disabled."""
    cfg = _load_config()
    return set(cfg.get("disabled_plugins", []))


def _set_disabled(name: str) -> None:
    """Add *name* to the disabled set in the config."""
    cfg = _load_config()
    disabled = set(cfg.get("disabled_plugins", []))
    disabled.add(name)
    cfg["disabled_plugins"] = sorted(disabled)
    _save_config(cfg)


def _set_enabled(name: str) -> None:
    """Remove *name* from the disabled set in the config."""
    cfg = _load_config()
    disabled = set(cfg.get("disabled_plugins", []))
    disabled.discard(name)
    cfg["disabled_plugins"] = sorted(disabled)
    _save_config(cfg)


# ── Directories ─────────────────────────────────────────────────

def get_plugin_dir() -> Path:
    """Return the ``plugins/`` directory next to the program, creating
    it if it doesn't exist."""
    d = Path(__file__).resolve().parent / "plugins"
    d.mkdir(exist_ok=True)
    return d


def get_core_plugin_dir() -> Path:
    """Return the ``plugins/core/`` directory for built-in plugins."""
    return get_plugin_dir() / "core"


# ── Loading ─────────────────────────────────────────────────────

def _load_module_from_file(filepath: Path,
                           builtin: bool = False) -> Optional[PluginInfo]:
    """Import a single .py file as a module.  Returns ``PluginInfo``
    or ``None`` on failure."""
    mod_name = f"pc98mount_plugin_{filepath.stem}"
    if mod_name in _loaded:
        return _loaded[mod_name]

    log.info(f"Loading plugin: {filepath}")
    try:
        spec = importlib.util.spec_from_file_location(mod_name, filepath)
        if spec is None or spec.loader is None:
            log.warning(f"Cannot create module spec for {filepath}")
            return None
        module = importlib.util.module_from_spec(spec)
        sys.modules[mod_name] = module
        spec.loader.exec_module(module)
        info = PluginInfo(
            module_name=mod_name,
            display_name=filepath.stem,
            file_path=filepath.resolve(),
            is_package=False,
            builtin=builtin,
            enabled=True,
        )
        _loaded[mod_name] = info
        _discovered[mod_name] = info
        log.info(f"Loaded plugin: {mod_name} from {filepath}")
        return info
    except Exception:
        log.exception(f"Failed to load plugin {filepath}")
        sys.modules.pop(mod_name, None)
        return None


def _load_package(pkg_dir: Path,
                  builtin: bool = False) -> Optional[PluginInfo]:
    """Import a package (directory with __init__.py)."""
    init = pkg_dir / "__init__.py"
    if not init.is_file():
        return None
    mod_name = f"pc98mount_plugin_{pkg_dir.name}"
    if mod_name in _loaded:
        return _loaded[mod_name]

    log.info(f"Loading plugin package: {pkg_dir}")
    try:
        parent_str = str(pkg_dir.parent)
        if parent_str not in sys.path:
            sys.path.insert(0, parent_str)
        spec = importlib.util.spec_from_file_location(
            mod_name, str(init),
            submodule_search_locations=[str(pkg_dir)],
        )
        if spec is None or spec.loader is None:
            return None
        module = importlib.util.module_from_spec(spec)
        sys.modules[mod_name] = module
        spec.loader.exec_module(module)
        info = PluginInfo(
            module_name=mod_name,
            display_name=pkg_dir.name,
            file_path=init.resolve(),
            is_package=True,
            builtin=builtin,
            enabled=True,
        )
        _loaded[mod_name] = info
        _discovered[mod_name] = info
        log.info(f"Loaded plugin package: {mod_name}")
        return info
    except Exception:
        log.exception(f"Failed to load plugin package {pkg_dir}")
        sys.modules.pop(mod_name, None)
        return None


def _discover_file(filepath: Path, builtin: bool = False) -> PluginInfo:
    """Create a PluginInfo for a file found on disk (without loading)."""
    mod_name = f"pc98mount_plugin_{filepath.stem}"
    return PluginInfo(
        module_name=mod_name,
        display_name=filepath.stem,
        file_path=filepath.resolve(),
        is_package=False,
        builtin=builtin,
        enabled=False,
    )


def _scan_dir(directory: Path,
              builtin: bool = False) -> List[PluginInfo]:
    """Scan one directory for plugins.  Loads enabled ones, records
    disabled ones.  Returns all discovered PluginInfo objects."""
    found: list[PluginInfo] = []
    if not directory.is_dir():
        return found

    disabled = _get_disabled_set()

    for item in sorted(directory.iterdir()):
        if item.name.startswith(('_', '.')):
            continue

        if item.is_file() and item.suffix == '.py':
            name = item.stem
            if name in disabled and not builtin:
                info = _discover_file(item, builtin=builtin)
                _discovered[info.module_name] = info
                found.append(info)
                log.info(f"Skipping disabled plugin: {name}")
            else:
                info = _load_module_from_file(item, builtin=builtin)
                if info:
                    found.append(info)
        elif item.is_dir() and (item / "__init__.py").is_file():
            name = item.name
            if name in disabled and not builtin:
                info = _discover_file(item / "__init__.py", builtin=builtin)
                info.is_package = True
                info.display_name = name
                _discovered[info.module_name] = info
                found.append(info)
                log.info(f"Skipping disabled plugin package: {name}")
            else:
                info = _load_package(item, builtin=builtin)
                if info:
                    found.append(info)

    return found


def load_plugins() -> List[str]:
    """Scan the plugins directory and load all plugins found.

    Loads ``plugins/core/`` first (marked as built-in, not removable),
    then ``plugins/`` (user-installable).  Respects the disabled list
    in ``98Bridge.config``.

    Returns a list of module names that were successfully loaded.
    """
    loaded: list[str] = []

    # Core plugins first (built-in, always enabled)
    core_dir = get_core_plugin_dir()
    if core_dir.is_dir():
        log.info(f"Scanning core plugins: {core_dir}")
        for info in _scan_dir(core_dir, builtin=True):
            if info.enabled:
                loaded.append(info.module_name)

    # User plugins
    plugin_dir = get_plugin_dir()
    if plugin_dir.is_dir():
        log.info(f"Scanning user plugins: {plugin_dir}")
        for info in _scan_dir(plugin_dir, builtin=False):
            if info.enabled:
                loaded.append(info.module_name)

    if loaded:
        log.info(f"Loaded {len(loaded)} plugin(s): {', '.join(loaded)}")
    else:
        log.debug("No plugins found")

    return loaded


# ── Unloading ───────────────────────────────────────────────────

def unload_plugin(mod_name: str) -> bool:
    """Unload a plugin: remove its registry entries and drop the module.

    Returns True if the plugin was found and unloaded.
    """
    info = _loaded.pop(mod_name, None)
    if info is None:
        return False

    _registry.unregister_all_from_source(mod_name)
    sys.modules.pop(mod_name, None)
    log.info(f"Unloaded plugin: {mod_name}")
    return True


# ── Enable / Disable ───────────────────────────────────────────

def disable_plugin(mod_name: str) -> bool:
    """Disable a plugin: unload it and persist the disabled state.

    Core plugins cannot be disabled.  Returns True on success.
    """
    info = _loaded.get(mod_name) or _discovered.get(mod_name)
    if info is None:
        return False
    if info.builtin:
        log.warning(f"Cannot disable core plugin: {info.display_name}")
        return False

    unload_plugin(mod_name)
    _set_disabled(info.display_name)

    # Update the discovered entry
    if mod_name in _discovered:
        _discovered[mod_name].enabled = False
    log.info(f"Disabled plugin: {info.display_name}")
    return True


def enable_plugin(mod_name: str) -> Optional[PluginInfo]:
    """Enable a disabled plugin: load it and persist the enabled state.

    Returns the ``PluginInfo`` on success, or ``None`` on failure.
    """
    info = _discovered.get(mod_name)
    if info is None:
        return None

    _set_enabled(info.display_name)

    if info.is_package:
        loaded = _load_package(info.file_path.parent, builtin=info.builtin)
    else:
        loaded = _load_module_from_file(info.file_path, builtin=info.builtin)

    if loaded:
        loaded.enabled = True
        _discovered[mod_name] = loaded
        log.info(f"Enabled plugin: {info.display_name}")
    return loaded


def is_plugin_enabled(mod_name: str) -> bool:
    """Check if a plugin is currently enabled (loaded)."""
    return mod_name in _loaded


# ── Reload ──────────────────────────────────────────────────────

def reload_plugin(mod_name: str) -> Optional[PluginInfo]:
    """Unload a plugin and re-import it from disk.

    Returns the new ``PluginInfo`` on success, or ``None`` if the
    file no longer exists or failed to load.
    """
    info = _loaded.get(mod_name)
    if info is None:
        log.warning(f"Cannot reload unknown plugin {mod_name}")
        return None

    filepath = info.file_path
    is_pkg = info.is_package
    builtin = info.builtin

    unload_plugin(mod_name)

    if is_pkg:
        return _load_package(filepath.parent, builtin=builtin)
    else:
        if filepath.is_file():
            return _load_module_from_file(filepath, builtin=builtin)
        log.warning(f"Plugin file no longer exists: {filepath}")
        return None


def reload_all_plugins() -> List[str]:
    """Unload every plugin and rescan the plugins directory.

    Returns the list of module names loaded after the rescan.
    """
    for mod_name in list(_loaded.keys()):
        unload_plugin(mod_name)
    _discovered.clear()
    return load_plugins()


# ── Install / remove ───────────────────────────────────────────

def install_plugin_file(src_path: str | Path) -> Optional[PluginInfo]:
    """Copy a ``.py`` file into the plugins directory and load it.

    Returns the ``PluginInfo`` on success, or ``None`` on failure.
    """
    src = Path(src_path).resolve()
    if not src.is_file():
        log.error(f"Source file does not exist: {src}")
        return None
    if src.suffix != '.py':
        log.error(f"Not a Python file: {src}")
        return None

    dest_dir = get_plugin_dir()
    dest = dest_dir / src.name

    if dest.exists():
        log.warning(f"Overwriting existing plugin: {dest}")
    shutil.copy2(src, dest)
    log.info(f"Installed plugin: {src.name} → {dest}")

    # Make sure it's not in the disabled list
    _set_enabled(src.stem)

    return _load_module_from_file(dest)


def remove_plugin(mod_name: str) -> bool:
    """Unload a plugin and delete its file from disk.

    Returns True if the plugin was unloaded and deleted.
    """
    info = _loaded.get(mod_name) or _discovered.get(mod_name)
    if info is None:
        log.warning(f"Cannot remove unknown plugin: {mod_name}")
        return False

    filepath = info.file_path
    pkg_dir = filepath.parent if info.is_package else None

    unload_plugin(mod_name)
    _discovered.pop(mod_name, None)

    # Clean up config
    _set_enabled(info.display_name)

    try:
        if info.is_package and pkg_dir and pkg_dir.is_dir():
            shutil.rmtree(pkg_dir)
            log.info(f"Deleted plugin package: {pkg_dir}")
        elif filepath.is_file():
            filepath.unlink()
            log.info(f"Deleted plugin file: {filepath}")
        return True
    except OSError as exc:
        log.error(f"Failed to delete plugin file: {exc}")
        return False


# ── Query ───────────────────────────────────────────────────────

def get_loaded_plugins() -> List[str]:
    """Return the names of all currently loaded plugin modules."""
    return sorted(_loaded.keys())


def get_plugin_info(mod_name: str) -> Optional[PluginInfo]:
    """Return ``PluginInfo`` for a plugin (loaded or disabled)."""
    return _discovered.get(mod_name)


def get_all_plugin_info() -> List[PluginInfo]:
    """Return ``PluginInfo`` for every discovered plugin
    (both loaded and disabled)."""
    return [_discovered[k] for k in sorted(_discovered.keys())]
