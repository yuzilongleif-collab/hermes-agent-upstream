"""Native tool interruption on a discovery-owned reader, never an owner connection."""
import sqlite3
from contextvars import ContextVar

from tools.interrupt import is_interrupted

discovery_active = ContextVar('session_search_discovery_active', default=False)


def discovery_db_path(db):
    path = getattr(db, 'db_path', None)
    if path is None or not str(path).strip() or str(path) == ':memory:':
        raise ValueError('Discovery requires a reopenable session database path')
    return path


class DiscoveryCancellation:
    def __init__(self):
        self.cancelled = False

    def check(self):
        self.cancelled = self.cancelled or is_interrupted()
        if self.cancelled:
            raise InterruptedError("Session search interrupted")

    def progress(self):
        try:
            self.check()
        except InterruptedError:
            return 1
        return 0

    def run(self, db, search, *, preserve_cjk=False):
        """Caller owns this RO object from construction through finally close."""
        self.check()
        conn = db._conn
        previous_ms = conn.execute('PRAGMA busy_timeout').fetchone()[0]
        conn.execute('PRAGMA busy_timeout=100')
        conn.set_progress_handler(self.progress, 1000)
        try:
            # The standalone opener now owns loading/readiness. Do not reload or
            # overwrite its marker decision with the borrowed owner's cached True.
            if preserve_cjk and not db._fts_cjk_loaded:
                raise sqlite3.OperationalError('Ready CJK search tokenizer unavailable on discovery reader')
            result = search(db)
            self.check()
            return result
        except sqlite3.Error:
            self.check()
            raise
        finally:
            conn.set_progress_handler(None, 0)
            conn.execute(f'PRAGMA busy_timeout={int(previous_ms)}')
