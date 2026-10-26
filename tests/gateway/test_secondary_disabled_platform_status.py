"""An inactive secondary's disabled channel must not keep a prior Fatal on rescan.

Real YAML/secret-scope loading, reconcile, duplicate guard, adapter wiring and status
writer/projection; only the transport factory and unrelated plugin/hook setup are doubles.
Live and queued adapters retain the existing no-reload contract (#110203).
"""
import asyncio
import threading
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import pytest_asyncio

from gateway.config import GatewayConfig, Platform
from gateway.platforms.base import BasePlatformAdapter
from gateway.run import GatewayRunner
from gateway.status import GatewayLiveness, flush_runtime_status, publish_runtime_status, read_runtime_status
from hermes_cli.web_routers import status as status_api


class _Transport(BasePlatformAdapter):
    def __init__(self, config, platform):
        super().__init__(config, platform)
        self.connect_calls = 0
        self.disconnect_calls = 0

    async def connect(self, *, is_reconnect=False):
        self.connect_calls += 1
        if self.config.token == "synthetic-rejected":
            self._set_fatal_error("auth_failed", "Synthetic transport rejection", retryable=False)
            return False
        self._mark_connected()
        return True

    async def disconnect(self):
        self.disconnect_calls += 1
        self._mark_disconnected()

    async def send(self, *args, **kwargs):
        raise AssertionError("No external messages in this regression")

    async def get_chat_info(self, *args, **kwargs):
        raise AssertionError("No external transport requests in this regression")


def _config(home, *, enabled=True, token="synthetic-shared", extra=False):
    text = (
        "platforms:\n  discord:\n"
        f"    enabled: {str(enabled).lower()}\n    token: {token}\n"
    )
    if extra:
        text += "  telegram:\n    enabled: true\n    token: synthetic-rejected\n"
    (home / "config.yaml").write_text(text, encoding="utf-8")


@pytest_asyncio.fixture
async def secondary(tmp_path, monkeypatch):
    import gateway.run_adapters as adapters_module
    import gateway.run_profile_reconcile as reconcile_module
    import gateway.status as status_module

    source_root = Path(__file__).resolve().parents[2]
    for module in (adapters_module, reconcile_module, status_module, status_api):
        assert module.__file__ is not None
        assert Path(module.__file__).resolve().is_relative_to(source_root)
        print(f"candidate_module={module.__name__}:{module.__file__}")
    home = tmp_path / ".hermes"
    alpha = home / "profiles" / "alpha"
    beta = home / "profiles" / "beta"
    alpha.mkdir(parents=True)
    beta.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr("hermes_cli.profiles.get_active_profile_name", lambda: "default")
    # No plugin installation, external helpers, hooks or MCP discovery in this test.
    monkeypatch.setattr("gateway.run_startup.recover_left_core_in", lambda *a, **kw: None)
    monkeypatch.setattr("hermes_cli.plugins.discover_plugins", lambda: None)
    monkeypatch.setattr(status_api, "resolve_gateway_liveness", lambda **kw: GatewayLiveness(True, None, "health"))
    _config(alpha)
    (beta / "config.yaml").write_text("platforms: {}\n", encoding="utf-8")
    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(multiplex_profiles=True)
    runner._running = True
    runner._primary_profile_name = "default"
    runner.adapters = {}
    runner._profile_adapters = {}
    runner._failed_platforms = {}
    runner._profile_failed_platforms = {}
    runner._background_tasks = set()
    runner.pairing_store, runner.pairing_stores = MagicMock(), {}
    runner.session_store = object()
    runner._busy_text_mode = "queue"
    runner._platform_lock_takeover_on_start = False
    runner._register_config_hooks = lambda *a, **kw: None
    runner._subscribe_plugin_rewire = lambda *a, **kw: None
    runner._sync_voice_mode_state_to_adapter = lambda *a: None
    runner._note_served_profiles([("default", home), ("alpha", alpha), ("beta", beta)])
    made = []

    def factory(platform, config):
        adapter = _Transport(config, platform)
        made.append(adapter)
        return adapter

    runner._create_adapter = factory
    primary = _Transport(type("Config", (), {"token": "synthetic-shared"})(), Platform.DISCORD)
    runner.adapters[Platform.DISCORD] = primary
    publish_runtime_status(gateway_state="running", served_profiles=["default", "alpha", "beta"])
    try:
        yield runner, alpha, made
    finally:
        for tasks in runner._profile_failed_platforms.values():
            for task in tasks.values():
                task.cancel()
        tasks = list(runner._background_tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        for adapter in made:
            if adapter.is_connected:
                await runner._safe_adapter_disconnect(adapter, adapter.platform)
        assert flush_runtime_status()


async def _projected():
    assert flush_runtime_status()
    result = await status_api._resolve_gateway_status(None, None)
    return result["gateway_platforms"]


@pytest.mark.asyncio
@pytest.mark.parametrize("reenabled", ["connected", "duplicate_credential", "auth_failed"])
async def test_disabled_inactive_secondary_clears_fatal_and_can_reenable(secondary, reenabled):
    runner, alpha, made = secondary
    _config(alpha, extra=True)
    assert (await runner.reconcile_served_profiles())["rescanned"] == ["alpha"]
    before = await _projected()
    assert before["alpha:discord"]["error_code"] == "duplicate_credential"
    assert made[0].connect_calls == made[0].disconnect_calls == 0  # refused before transport
    assert before["alpha:telegram"]["error_code"] == "auth_failed"
    for key in ("telegram", "beta:discord"):
        publish_runtime_status(platform=key, platform_state="fatal", error_code="sentinel", error_message="Keep this error")
    publish_runtime_status(platform="alpha:discord", needs_attention=True, retrying_since="synthetic-previous-retry")
    assert flush_runtime_status()
    sentinels = {key: read_runtime_status()["platforms"][key] for key in ("telegram", "beta:discord")}

    _config(alpha, enabled=False, extra=True)
    result = await runner.reconcile_served_profiles()
    assert result["rescanned"] == ["alpha"]
    assert result["served_profiles"] == ["default", "alpha", "beta"]
    for rescan in range(3):
        if rescan:
            # Exercise another changed-file scan, not only the unchanged-signature fast path.
            with (alpha / "config.yaml").open("a", encoding="utf-8") as stream:
                stream.write("\n# another scan\n")
            assert (await runner.reconcile_served_profiles())["rescanned"] == ["alpha"]
        projected = await _projected()
        disabled = projected["alpha:discord"]
        assert disabled["state"] == "disabled"
        assert disabled["error_code"] is None and disabled["error_message"] is None
        assert disabled["needs_attention"] is False and disabled["retrying_since"] is None
        assert projected["alpha:telegram"]["error_code"] == "auth_failed"
        assert all(read_runtime_status()["platforms"][key] == value for key, value in sentinels.items())
        assert Platform.DISCORD not in runner._profile_adapters["alpha"]
        assert sum(adapter.platform == Platform.DISCORD for adapter in made) == 1
    assert (await runner.reconcile_served_profiles())["rescanned"] == []

    token = {"connected": "synthetic-unique", "duplicate_credential": "synthetic-shared", "auth_failed": "synthetic-rejected"}[reenabled]
    _config(alpha, token=token, extra=True)
    assert (await runner.reconcile_served_profiles())["rescanned"] == ["alpha"]
    restored = (await _projected())["alpha:discord"]
    assert restored["state"] == ("connected" if reenabled == "connected" else "fatal")
    assert restored["error_code"] == (None if reenabled == "connected" else reenabled)
    if reenabled == "connected":
        adapter = runner._profile_adapters["alpha"][Platform.DISCORD]
        assert adapter.connect_calls == 1
        assert adapter._runtime_status_platform_key == "alpha:discord"
        assert adapter._fatal_error_handler is not None and adapter._authorization_check is not None
    else:
        assert Platform.DISCORD not in runner._profile_adapters["alpha"]


@pytest.mark.asyncio
@pytest.mark.parametrize("occupied", ["live", "queued"])
async def test_disabled_rescan_does_not_relabel_live_or_queued_adapter(secondary, occupied):
    runner, alpha, made = secondary
    _config(alpha, token="synthetic-unique")
    assert (await runner.reconcile_served_profiles())["rescanned"] == ["alpha"]
    adapter = runner._profile_adapters["alpha"][Platform.DISCORD]
    assert adapter.is_connected
    task = None
    if occupied == "queued":
        runner._profile_adapters["alpha"].pop(Platform.DISCORD)
        await runner._safe_adapter_disconnect(adapter, Platform.DISCORD)
        task = asyncio.create_task(asyncio.Event().wait())
        runner._profile_failed_platforms["alpha"] = {Platform.DISCORD: task}
        publish_runtime_status(platform="alpha:discord", platform_state="retrying", error_code="transport_stale")
    before = (await _projected())["alpha:discord"]
    try:
        _config(alpha, enabled=False)
        assert (await runner.reconcile_served_profiles())["rescanned"] == ["alpha"]
        assert (await _projected())["alpha:discord"] == before
        assert len(made) == 1
        if occupied == "live":
            assert adapter.is_connected and adapter.disconnect_calls == 0
            assert runner._profile_adapters["alpha"][Platform.DISCORD] is adapter
        else:
            assert not task.done()
            assert runner._profile_failed_platforms["alpha"][Platform.DISCORD] is task
    finally:
        if task:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("catalog", ["explicit_pair", "all_known_platforms"])
async def test_disabled_unseen_platforms_stay_absent_and_pending_fatal_converges(secondary, monkeypatch, catalog):
    from gateway import status

    runner, alpha, made = secondary
    beta = alpha.parent / "beta"
    names = ("discord", "telegram") if catalog == "explicit_pair" else tuple(
        platform.value for platform in Platform if platform != Platform.LOCAL
    )
    disabled_yaml = "platforms:\n" + "".join(f"  {name}:\n    enabled: false\n" for name in names)
    assert (await _projected()) == {}
    for home in (alpha, beta):
        (home / "config.yaml").write_text(disabled_yaml, encoding="utf-8")
    for _ in range(2):
        result = await runner.reconcile_served_profiles()
        assert set(result["rescanned"]) == {"alpha", "beta"}
        for name, home in (("alpha", alpha), ("beta", beta)):
            loaded = runner._profile_configs[name]
            assert set(names) <= {platform.value for platform in loaded.platforms}
            assert all(not config.enabled for config in loaded.platforms.values())
            with (home / "config.yaml").open("a", encoding="utf-8") as stream:
                stream.write("\n# rescan disabled config\n")
        assert (await _projected()) == {}  # no phantom Desktop rows
        assert made == []

    entered, release = threading.Event(), threading.Event()
    write_json = status._write_json_file

    def delayed_write(path, payload):
        entered.set()
        assert release.wait(timeout=10)
        return write_json(path, payload)

    monkeypatch.setattr(status, "_write_json_file", delayed_write)
    claim = (Platform.DISCORD, "synthetic-claim")
    try:
        assert runner._refuse_duplicate_claim(claim, {claim: "default"}, "alpha", Platform.DISCORD, "credential")
        assert await asyncio.to_thread(entered.wait, 5)
        # The Fatal exists only in the canonical snapshot, not yet in the disk projection.
        assert read_runtime_status()["platforms"] == {}
        assert set((await runner.reconcile_served_profiles())["rescanned"]) == {"alpha", "beta"}
    finally:
        release.set()
        assert await asyncio.to_thread(flush_runtime_status)
    projected = await _projected()
    assert set(projected) == {"alpha:discord"}
    assert projected["alpha:discord"]["state"] == "disabled"
    assert projected["alpha:discord"]["error_code"] is None
    assert made == []
