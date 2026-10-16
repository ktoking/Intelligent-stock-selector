"""Crash-safe publication for derived reports, never a substitute for the ledger."""
import os
from pathlib import Path
import tempfile


def atomic_write(path,text):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    temporary=None
    try:
        options={'mode':'wb'} if isinstance(text,bytes) else {'mode':'w','encoding':'utf-8'}
        with tempfile.NamedTemporaryFile(dir=path.parent,delete=False,**options) as f:
            temporary=Path(f.name);temporary.chmod(0o600)
            f.write(text);f.flush();os.fsync(f.fileno())
        temporary.replace(path)
        descriptor=os.open(path.parent,os.O_RDONLY)
        try:os.fsync(descriptor)
        finally:os.close(descriptor)
    finally:
        if temporary and temporary.exists():temporary.unlink()
