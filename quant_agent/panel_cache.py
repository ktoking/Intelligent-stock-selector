"""Bounded reuse of immutable published panels, invalidated by file metadata."""
from functools import lru_cache
from pathlib import Path
import threading

from research.daily_factor_lab.core import load_panel as _load_panel

_LOCK=threading.RLock()


@lru_cache(maxsize=8)
def _cached(directory,manifest):
    panel=_load_panel(directory)
    for values in panel.fields.values():values.setflags(write=False)
    panel.stock_mask.setflags(write=False)
    return panel


def load_panel(directory):
    path=Path(directory).resolve()
    files=sorted(path.glob('*.pkl'))+[path/'universe.json',path/'security_types.json']
    manifest=tuple((p.name,p.stat().st_size,p.stat().st_mtime_ns) for p in files)
    with _LOCK:return _cached(str(path),manifest)
