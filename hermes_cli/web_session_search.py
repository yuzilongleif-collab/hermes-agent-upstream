"""Cancellation for one HTTP search's freshly opened, exclusively owned RO handle.

This is deliberately not a policy for Agent session_search or its shared writer.
SQLite progress checks bound VM work, not a blocked OS read or a Python UDF.
"""
import asyncio
import sqlite3
import threading
import time
from collections.abc import Callable

from fastapi import HTTPException, Request
from hermes_state_errors import is_fts_scoped_corruption_error, is_malformed_db_error, is_transient_sqlite_error
from hermes_state_readpool import remaining_read_timeout

SEARCH_TIMEOUT_SECONDS = 25.0  # below the Desktop HTTP client's 30-second wait
_TIMEOUT_DETAIL = {"code": "session_search_timeout", "message": "Session search did not finish in time"}


class SearchControl:
    def __init__(self):
        self.stopped = threading.Event()
        self.deadline = time.monotonic() + SEARCH_TIMEOUT_SECONDS

    def check(self):
        if self.stopped.is_set():
            raise RuntimeError("Session search was abandoned")
        if time.monotonic() >= self.deadline:
            raise TimeoutError("Session search deadline expired")

    def run(self, db, search: Callable):
        """Only _with_db(read_only=True)'s request-owned SessionDB may enter."""
        self.check()
        if not db.read_only:
            raise ValueError("Search cancellation requires an exclusively owned read-only store")
        conn = db._conn
        previous_ms = conn.execute("PRAGMA busy_timeout").fetchone()[0]
        timeout_ms = int(remaining_read_timeout(self.deadline, 0.1) * 1000)
        conn.execute(f"PRAGMA busy_timeout={timeout_ms}")
        conn.set_progress_handler(
            lambda: int(self.stopped.is_set() or time.monotonic() >= self.deadline), 1000)
        try:
            result = search(db)
            self.check()
            return result
        except sqlite3.Error as exc:
            self.check()
            if is_malformed_db_error(exc) or is_fts_scoped_corruption_error(exc):
                from hermes_cli.web_routers._common import corrupt_store_status
                raise corrupt_store_status(db.db_path, exc) from exc
            raise
        finally:
            conn.set_progress_handler(None, 0)
            conn.execute(f"PRAGMA busy_timeout={int(previous_ms)}")


async def _disconnected(request: Request):
    # GET has no body consumer. Own its receive channel until disconnect, without polling.
    while True:
        if (await request.receive())["type"] == "http.disconnect":
            return


def _consume(task):
    if not task.cancelled():
        task.exception()


async def await_search(run: Callable, control: SearchControl, request: Request | None):
    """Stop SQLite on disconnect, handler cancellation, or the HTTP-only deadline.

    Shield the worker from Future cancellation: it owns connection cleanup, and
    its eventual exception must be retrieved even after the HTTP caller leaves.
    """
    def guarded_run():
        control.check()  # queued work that has expired must not even open the store
        return run()

    worker = asyncio.create_task(asyncio.to_thread(guarded_run))
    watcher = asyncio.create_task(_disconnected(request)) if request is not None else None
    try:
        waiting = {worker, watcher} if watcher is not None else {worker}
        done, _ = await asyncio.wait(waiting, timeout=max(0, control.deadline - time.monotonic()),
                                     return_when=asyncio.FIRST_COMPLETED)
        if watcher in done:
            watcher.result()
            raise HTTPException(status_code=499, detail="Session search was cancelled")
        if worker in done:
            try:
                return worker.result()
            except TimeoutError as exc:
                raise HTTPException(status_code=504, detail=_TIMEOUT_DETAIL) from exc
            except sqlite3.Error as exc:
                if is_transient_sqlite_error(exc):
                    raise HTTPException(status_code=503, detail="Session store is busy (disk I/O or lock)") from exc
                raise
        raise HTTPException(status_code=504, detail=_TIMEOUT_DETAIL)
    finally:
        control.stopped.set()
        worker.add_done_callback(_consume)
        if watcher is not None:
            watcher.cancel()
            await asyncio.gather(watcher, return_exceptions=True)
