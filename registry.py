"""
Plugin registry for pc98mount.

Provides a central place to register:
  - **Image formats**      – DiskImage subclasses keyed by file extension(s)
  - **Partition detectors** – functions that return ``PartitionEntry`` lists
  - **Filesystem probers**  – classes that can parse a filesystem on a
                              partition (currently just FAT, but extensible)

Built-in formats register themselves when their modules are first imported.
Third-party plugins do the same via the decorators or the ``register_*``
helpers exported here, and are discovered automatically by
``plugin_loader.py`` at startup.

Every registration records the *source* module name so that all entries
from a given plugin can be removed cleanly when the plugin is unloaded.

Quick-start for plugin authors
------------------------------
::

    from registry import register_image_format, register_partition_detector
    from disk_image import DiskImage

    class MyImage(DiskImage):
        ...

    register_image_format(
        extensions=['.xyz', '.abc'],
        opener=lambda path: MyImage(path),
        label='XYZ Images',
        group_label='XYZ Images',
    )

    def detect_my_partition(disk_image):
        ...
        return [PartitionEntry(...)]

    register_partition_detector('MyScheme', detect_my_partition, priority=50)

Or use the convenience decorators::

    @image_format(extensions=['.xyz'], label='XYZ Images')
    class MyImage(DiskImage):
        ...

    @partition_detector('MyScheme', priority=50)
    def detect_my_partition(disk_image):
        ...
"""

from __future__ import annotations

import inspect
import logging
from dataclasses import dataclass
from typing import (
    Any, Callable, Dict, List, Optional, Sequence,
)

log = logging.getLogger("pc98mount.registry")


# ── Caller-detection helper ─────────────────────────────────────

def _caller_module_name() -> str:
    """Walk the call stack and return the ``__name__`` of the first
    frame that is *not* inside this module.  Used to auto-tag every
    registration with its source module."""
    for frame_info in inspect.stack():
        mod = inspect.getmodule(frame_info.frame)
        if mod is not None and mod.__name__ != __name__:
            return mod.__name__
    return "__unknown__"


# ── Image-format registry ───────────────────────────────────────

@dataclass
class ImageFormatEntry:
    """Metadata for one registered image format."""
    extensions: List[str]        # e.g. ['.d88', '.d68', '.d77']
    opener: Callable[[str], Any] # path → DiskImage instance
    label: str                   # human-readable, e.g. "D88 Images"
    group_label: str             # used for file-dialog groups
    priority: int = 50           # lower = matched first (for ambiguous ext)
    source: str = ""             # module that registered this entry

_image_formats: List[ImageFormatEntry] = []
_ext_to_format: Dict[str, ImageFormatEntry] = {}
_fallback_opener: Optional[Callable[[str], Any]] = None


def _rebuild_ext_map():
    """Rebuild ``_ext_to_format`` from the current ``_image_formats``."""
    _ext_to_format.clear()
    for entry in _image_formats:
        for ext in entry.extensions:
            existing = _ext_to_format.get(ext)
            if existing is None or entry.priority < existing.priority:
                _ext_to_format[ext] = entry


def register_image_format(
    *,
    extensions: Sequence[str],
    opener: Callable[[str], Any],
    label: str,
    group_label: str | None = None,
    priority: int = 50,
    source: str | None = None,
) -> ImageFormatEntry:
    """Register an image format.

    Parameters
    ----------
    extensions : sequence of str
        File extensions **with leading dot**, e.g. ``['.d88', '.d68']``.
    opener : callable
        ``opener(path) → DiskImage`` — constructs and returns the image.
    label : str
        Short human-readable label (shown in UI lists).
    group_label : str, optional
        File-dialog filter label.  Defaults to *label*.
    priority : int
        Lower value = higher priority when two formats claim the same
        extension.
    source : str, optional
        Module name that owns this entry.  Auto-detected if omitted.
    """
    entry = ImageFormatEntry(
        extensions=[e.lower() for e in extensions],
        opener=opener,
        label=label,
        group_label=group_label or label,
        priority=priority,
        source=source or _caller_module_name(),
    )
    _image_formats.append(entry)
    _image_formats.sort(key=lambda e: e.priority)
    _rebuild_ext_map()

    log.debug(f"Registered image format: {label} {entry.extensions} "
              f"[source={entry.source}]")
    return entry


def unregister_image_format(entry: ImageFormatEntry) -> bool:
    """Remove a previously registered image format.  Returns True if found."""
    try:
        _image_formats.remove(entry)
        _rebuild_ext_map()
        log.debug(f"Unregistered image format: {entry.label}")
        return True
    except ValueError:
        return False


def set_fallback_opener(opener: Callable[[str], Any]) -> None:
    """Set the opener used when no extension matches."""
    global _fallback_opener
    _fallback_opener = opener


def open_image(path: str) -> Any:
    """Auto-detect format by extension and return a DiskImage.

    Falls back to the registered fallback opener (typically RawImage)
    when no extension matches.
    """
    lower = path.lower()
    for ext, entry in sorted(
        _ext_to_format.items(), key=lambda kv: -len(kv[0])
    ):
        if lower.endswith(ext):
            log.info(f"Opening {path!r} as {entry.label}")
            return entry.opener(path)

    if _fallback_opener is not None:
        log.info(f"Opening {path!r} with fallback opener")
        return _fallback_opener(path)

    raise ValueError(f"No registered image format for {path!r}")


def get_image_formats() -> List[ImageFormatEntry]:
    """Return all registered image formats (sorted by priority)."""
    return list(_image_formats)


def get_supported_extensions() -> List[str]:
    """Return all registered extensions (without dot, deduplicated)."""
    seen: set[str] = set()
    result: list[str] = []
    for entry in _image_formats:
        for ext in entry.extensions:
            bare = ext.lstrip('.')
            if bare not in seen:
                seen.add(bare)
                result.append(bare)
    return result


# ── Partition-detector registry ─────────────────────────────────

@dataclass
class PartitionDetectorEntry:
    """Metadata for one registered partition detector."""
    name: str
    detector: Callable  # (disk_image) → list[PartitionEntry]
    priority: int = 50  # lower = tried first
    source: str = ""

_partition_detectors: List[PartitionDetectorEntry] = []


def register_partition_detector(
    name: str,
    detector: Callable,
    priority: int = 50,
    source: str | None = None,
) -> PartitionDetectorEntry:
    """Register a partition-table detector.

    Detectors are tried in priority order (lowest first).  The first
    one that returns a non-empty list wins.
    """
    entry = PartitionDetectorEntry(
        name=name, detector=detector, priority=priority,
        source=source or _caller_module_name(),
    )
    _partition_detectors.append(entry)
    _partition_detectors.sort(key=lambda e: e.priority)
    log.debug(f"Registered partition detector: {name} (pri={priority}) "
              f"[source={entry.source}]")
    return entry


def unregister_partition_detector(entry: PartitionDetectorEntry) -> bool:
    """Remove a previously registered detector.  Returns True if found."""
    try:
        _partition_detectors.remove(entry)
        log.debug(f"Unregistered partition detector: {entry.name}")
        return True
    except ValueError:
        return False


def get_partition_detectors() -> List[PartitionDetectorEntry]:
    """Return all registered detectors (sorted by priority)."""
    return list(_partition_detectors)


# ── Filesystem-prober registry ──────────────────────────────────

@dataclass
class FilesystemProberEntry:
    """Metadata for one registered filesystem prober."""
    name: str
    prober: Callable  # (disk_image) → filesystem instance or raises
    priority: int = 50
    source: str = ""

_filesystem_probers: List[FilesystemProberEntry] = []


def register_filesystem_prober(
    name: str,
    prober: Callable,
    priority: int = 50,
    source: str | None = None,
) -> FilesystemProberEntry:
    """Register a filesystem prober.

    A prober is ``prober(disk_image) → filesystem_obj``.  It should
    raise on failure so that the next prober can be tried.
    """
    entry = FilesystemProberEntry(
        name=name, prober=prober, priority=priority,
        source=source or _caller_module_name(),
    )
    _filesystem_probers.append(entry)
    _filesystem_probers.sort(key=lambda e: e.priority)
    log.debug(f"Registered filesystem prober: {name} "
              f"[source={entry.source}]")
    return entry


def unregister_filesystem_prober(entry: FilesystemProberEntry) -> bool:
    """Remove a previously registered prober.  Returns True if found."""
    try:
        _filesystem_probers.remove(entry)
        log.debug(f"Unregistered filesystem prober: {entry.name}")
        return True
    except ValueError:
        return False


def get_filesystem_probers() -> List[FilesystemProberEntry]:
    return list(_filesystem_probers)


def probe_filesystem(disk_image: Any) -> Any:
    """Try each registered filesystem prober until one succeeds.

    Returns ``None`` if no prober could parse the image.
    """
    for entry in _filesystem_probers:
        try:
            fs = entry.prober(disk_image)
            log.info(f"Filesystem detected: {entry.name}")
            return fs
        except Exception as exc:
            log.debug(f"{entry.name} prober failed: {exc}")
    return None


# ── Bulk unregister ─────────────────────────────────────────────

def unregister_all_from_source(source: str) -> int:
    """Remove every registration whose ``source`` matches *source*.

    Returns the number of entries removed.
    """
    count = 0
    for entry in [e for e in _image_formats if e.source == source]:
        _image_formats.remove(entry)
        count += 1
    if count:
        _rebuild_ext_map()

    for entry in [e for e in _partition_detectors if e.source == source]:
        _partition_detectors.remove(entry)
        count += 1

    for entry in [e for e in _filesystem_probers if e.source == source]:
        _filesystem_probers.remove(entry)
        count += 1

    if count:
        log.info(f"Unregistered {count} entries from source {source!r}")
    return count


def get_registrations_for_source(source: str) -> dict:
    """Return a summary of what *source* has registered.

    Returns a dict with keys ``'formats'``, ``'detectors'``,
    ``'probers'``, each mapping to a list of labels/names.
    """
    return {
        'formats':   [e.label for e in _image_formats
                      if e.source == source],
        'detectors': [e.name for e in _partition_detectors
                      if e.source == source],
        'probers':   [e.name for e in _filesystem_probers
                      if e.source == source],
    }


# ── Convenience decorators ──────────────────────────────────────

def image_format(
    *,
    extensions: Sequence[str],
    label: str,
    group_label: str | None = None,
    priority: int = 50,
):
    """Class decorator that registers a DiskImage subclass.

    The class itself is used as the opener (called with ``cls(path)``).

    Usage::

        @image_format(extensions=['.xyz'], label='XYZ Images')
        class XYZImage(DiskImage):
            ...
    """
    def _decorator(cls):
        register_image_format(
            extensions=extensions,
            opener=cls,
            label=label,
            group_label=group_label,
            priority=priority,
        )
        return cls
    return _decorator


def partition_detector(name: str, *, priority: int = 50):
    """Function decorator that registers a partition detector.

    Usage::

        @partition_detector('MyScheme', priority=30)
        def detect_my_scheme(disk_image):
            ...
    """
    def _decorator(fn):
        register_partition_detector(name, fn, priority)
        return fn
    return _decorator


def filesystem_prober(name: str, *, priority: int = 50):
    """Function decorator that registers a filesystem prober.

    Usage::

        @filesystem_prober('MyFS')
        def probe_myfs(disk_image):
            return MyFilesystem(disk_image)
    """
    def _decorator(fn):
        register_filesystem_prober(name, fn, priority)
        return fn
    return _decorator
