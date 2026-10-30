"""Native tool interruption on a discovery-owned reader, never an owner connection."""
import sqlite3
from contextvars import ContextVar

from hermes_state_common import FTS_CJK_STALE_KEY, FTS_STALE_KEY
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
            if preserve_cjk:
                self._initialize_cjk(db)
            result = search(db)
            self.check()
            return result
        except sqlite3.Error:
            self.check()
            raise
        finally:
            conn.set_progress_handler(None, 0)
            conn.execute(f'PRAGMA busy_timeout={int(previous_ms)}')

    def _initialize_cjk(self, db):
        # A private reader must not silently turn a ready writer's CJK MATCH into
        # the semantically different LIKE fallback. Never create/rebuild an index.
        from hermes_state_fts import load_fts5_cjk_extension
        self.check()
        db._fts_cjk_loaded = load_fts5_cjk_extension(db._conn)
        self.check()
        if not db._fts_cjk_loaded:
            raise sqlite3.OperationalError('Ready CJK search tokenizer unavailable on discovery reader')
        markers = db._conn.execute('SELECT key FROM state_meta WHERE key IN (?, ?, ?)',
                                  (FTS_STALE_KEY, FTS_CJK_STALE_KEY, 'fts_cjk_rebuild_high_water')).fetchall()
        self.check()
        db._fts_cjk_available = not markers and db._fts_table_probe(db._conn.cursor(), 'messages_fts_cjk') is True
        self.check()
