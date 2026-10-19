"""Regression for #134866 Lead A: undiscovered sockets still own real FDs.

No TLS, database or network service is needed: the real SDK/httpx close chain
must not release a socketpair FD from the guard's non-owner thread.
"""

import os
import socket
import stat
import threading
from types import SimpleNamespace

import httpx
from openai import OpenAI, Stream
import pytest

from agent import auxiliary_client as aux


class _SocketBody(httpx.SyncByteStream):
    _sock: socket.socket | None = None

    def __init__(self, sock):
        self.sock = sock
        self.close_threads = []

    def __iter__(self):
        yield self.sock.recv(1)

    def close(self):
        self.close_threads.append(threading.get_ident())
        self.sock.close()


class _BrokenSocketLookup:
    def get_extra_info(self, name):
        raise RuntimeError("backend cannot expose its socket")


class _BrokenResponseLookup:
    def __init__(self, stream):
        self.stream = stream

    @property
    def response(self):
        raise RuntimeError("wrapper response is unavailable")

    def close(self):
        self.stream.close()


def _on_stranger(call):
    errors = []

    def run():
        try:
            call()
        except BaseException as exc:
            errors.append(exc)

    thread = threading.Thread(target=run)
    thread.start()
    thread.join(5)
    assert not thread.is_alive(), "attempt cancellation blocked"
    return errors


@pytest.mark.platforms("posix")
@pytest.mark.parametrize("network_stream,response_body,broken_response", [
    pytest.param(None, False, False, id="missing"),
    pytest.param(lambda sock: SimpleNamespace(hidden_transport=sock), False, False, id="wrapped"),
    pytest.param(lambda sock: _BrokenSocketLookup(), False, False, id="lookup-error"),
    pytest.param(None, False, True, id="response-error"),
    pytest.param(lambda sock: SimpleNamespace(get_extra_info=lambda name: sock), False, False, id="known"),
    pytest.param(None, True, False, id="response-body"),
])
def test_stranger_preserves_sdk_transport_fd_until_owner_release(
    tmp_path, network_stream, response_body, broken_response,
):
    owner_tid = threading.get_ident()
    left, right = socket.socketpair()
    body = _SocketBody(left)
    response = httpx.Response(200, stream=body, request=httpx.Request("GET", "https://example.test"))
    with OpenAI(api_key="test", http_client=httpx.Client(trust_env=False)) as client:
        stream = Stream(cast_to=object, response=response, client=client)
        if network_stream is not None:
            response.extensions["network_stream"] = network_stream(left)
        if response_body:
            body._sock = left
        guarded = _BrokenResponseLookup(stream) if broken_response else stream
        guard = aux._CodexStreamGuard(client, 30)
        guard.adopt_stream(guarded)
        fd = left.fileno()
        before = os.fstat(fd)
        try:
            errors = _on_stranger(lambda: guard.close_attempt_stream("test cancel"))
            # Plant an unrelated open after cancellation, before checking the original FD.
            # A premature close can recycle its number; socket mode + inode must both survive.
            with (tmp_path / "unrelated-file").open("wb"):
                after = os.fstat(fd)
                assert stat.S_ISSOCK(after.st_mode), "non-owner released the transport FD"
                assert (after.st_dev, after.st_ino) == (before.st_dev, before.st_ino)
            assert not errors, errors
            assert body.close_threads == []
            guard.release_stream(guarded)
            assert body.close_threads == [owner_tid]
            assert response.is_closed
            assert left.fileno() == -1
        finally:
            guard.release_stream(guarded)
            guard.finish()
            left.close()
            right.close()


def test_owner_can_close_unknown_transport_directly():
    threads = []
    stream = SimpleNamespace(close=lambda: threads.append(threading.get_ident()))
    guard = aux._CodexStreamGuard(None, 30)
    guard.adopt_stream(stream)
    guard.close_attempt_stream("owner close")
    assert threads == [threading.get_ident()]
    guard.finish()


def test_unknown_sdk_stream_unwinds_at_its_read_timeout(monkeypatch):
    """Unknown transport discovery must not defeat the finite read timeout or owner finally."""
    owner_tid = threading.get_ident()
    left, right = socket.socketpair()
    left.settimeout(0.5)
    body = _SocketBody(left)
    response = httpx.Response(200, stream=body, request=httpx.Request("GET", "https://example.test"))
    with OpenAI(api_key="test", http_client=httpx.Client(trust_env=False)) as client:
        stream = Stream(cast_to=object, response=response, client=client)
        # Replace only the external request, not the SDK iterator/close chain or guard.
        monkeypatch.setattr(client.responses, "create", lambda **kwargs: stream)
        adapter = aux._CodexCompletionsAdapter(client, "test-model")
        try:
            with pytest.raises(TimeoutError, match="no-progress timeout"):
                adapter.create(messages=[], timeout=30, no_progress_timeout=0.05)
            assert body.close_threads == [owner_tid]
            assert response.is_closed
        finally:
            response.close()
            left.close()
            right.close()


def test_socketless_cancel_returns_to_host_and_owner_eventually_cleans_up(monkeypatch):
    """The protected host is bounded even when a truly socketless read cannot be woken.

    An Event models a finite provider read timeout; only the reader closes its
    stream after that boundary. No fabricated socket-discovery result is used.
    """
    entered = threading.Event()
    provider_timeout = threading.Event()
    closed = threading.Event()
    cancel = threading.Event()
    threads = {}
    outcome = []
    timer_finished = threading.Event()
    original_timeout = aux._CodexStreamGuard._close_client_on_timeout

    def on_timeout(guard):
        try:
            original_timeout(guard)
        finally:
            timer_finished.set()

    monkeypatch.setattr(aux._CodexStreamGuard, "_close_client_on_timeout", on_timeout)

    class Socketless:
        def __iter__(self):
            threads["reader"] = threading.get_ident()
            entered.set()
            assert provider_timeout.wait(5), "test did not release bounded read"
            raise httpx.ReadTimeout("finite provider timeout")

        def close(self):
            threads["close"] = threading.get_ident()
            closed.set()

    stream = Socketless()
    real_client = SimpleNamespace(
        api_key="test", base_url="https://example.test/codex",
        responses=SimpleNamespace(create=lambda **kwargs: stream),
        close=lambda: None,
    )
    wrapper = aux.CodexAuxiliaryClient(real_client, "test-model")

    def host():
        try:
            with aux.aux_interrupt_protection(cancel_event=cancel):
                aux._relay_sync_completion(wrapper, {"model": "test-model", "messages": [], "timeout": 0.5})
        except BaseException as exc:
            outcome.append(exc)

    thread = threading.Thread(target=host)
    thread.start()
    try:
        assert entered.wait(5)
        cancel.set()
        thread.join(5)
        assert not thread.is_alive(), "host cancellation waited for socketless reader"
        assert len(outcome) == 1 and isinstance(outcome[0], aux.AuxiliaryExplicitCancellation)
        assert timer_finished.wait(5), "cancelled attempt watchdog did not finish"
        assert not closed.is_set()
    finally:
        provider_timeout.set()
        thread.join(5)
        assert closed.wait(5), "owner did not close after the read returned"
    assert threads["reader"] == threads["close"]


@pytest.mark.platforms("posix")
def test_socket_discovery_racing_owner_release_does_not_touch_reused_fd(tmp_path):
    discovered = threading.Event()
    resume = threading.Event()
    left, right = socket.socketpair()
    errors = []

    def lookup(name):
        discovered.set()
        assert resume.wait(5)
        return left

    stream = SimpleNamespace(
        response=SimpleNamespace(extensions={"network_stream": SimpleNamespace(get_extra_info=lookup)}),
        close=left.close,
    )
    guard = aux._CodexStreamGuard(None, 30)
    guard.adopt_stream(stream)

    def cancel():
        try:
            guard.close_attempt_stream("racing cancel")
        except BaseException as exc:
            errors.append(exc)

    thread = threading.Thread(target=cancel)
    thread.start()
    try:
        assert discovered.wait(5)
        guard.release_stream(stream)
        with (tmp_path / "reused-file").open("wb") as filler:
            before = os.fstat(filler.fileno())
            resume.set()
            thread.join(5)
            assert not thread.is_alive()
            assert os.fstat(filler.fileno()) == before
        assert not errors, errors
    finally:
        resume.set()
        thread.join(5)
        left.close()
        right.close()
        guard.finish()
