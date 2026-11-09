from pathlib import Path
import asyncio
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
PARENT = ROOT.parent
if str(PARENT) not in sys.path:
    sys.path.insert(0, str(PARENT))

from astrbot_plugin_hindsight_memory.companion_bridge import (  # noqa: E402
    SCHEMA,
    CompanionMemoryBridge,
    WrittenMemoryIdStore,
    build_record_context,
    build_record_metadata,
    build_record_tags,
    find_companion_api,
)


class FakeRegistration:
    def __init__(self, provider_id: str) -> None:
        self.provider_id = provider_id


class FakeExtensionApi:
    memory_provider_api_version = SCHEMA

    def __init__(self, *, fail_unregister: bool = False) -> None:
        self.calls: list[dict] = []
        self.unregistered: list[FakeRegistration] = []
        self.fail_unregister = fail_unregister

    def register_memory_provider(self, provider_id, **kwargs):
        self.calls.append({"provider_id": provider_id, **kwargs})
        return FakeRegistration(provider_id)

    def unregister_memory_provider(self, registration):
        if self.fail_unregister:
            raise RuntimeError("registry is gone")
        self.unregistered.append(registration)
        return True


class FakeCompanionHost:
    """陪伴插件实例的最小等价物：它把 extension_api 挂在自己身上。"""

    def __init__(self, api) -> None:
        self.extension_api = api


class FakeStar:
    def __init__(self, star_cls, name: str = "astrbot_plugin_private_companion",
                 activated: bool = True) -> None:
        self.name = name
        self.root_dir_name = name
        self.activated = activated
        self.star_cls = star_cls


class FakeContext:
    def __init__(self, stars) -> None:
        self._stars = list(stars)

    def get_all_stars(self):
        return self._stars


class FakeRetainClient:
    def __init__(self) -> None:
        self.retained: list[dict] = []
        self.fail = False

    async def retain(self, **kwargs):
        if self.fail:
            raise RuntimeError("hindsight down")
        self.retained.append(kwargs)
        return {"status": "accepted"}


class FakePlugin:
    def __init__(self, context, *, enabled: bool = True, data_dir: Path | None = None,
                 config: dict | None = None, bank: str = "bank-1") -> None:
        self._context = context
        self.salt = "salt-123"
        self._enabled = enabled
        self.client = FakeRetainClient()
        self.store = type("Store", (), {"data_dir": data_dir})()
        self.bank = bank
        # 陪伴接入开关默认关闭，测试里要显式打开
        self.config = {"companion_provider_enabled": True, "request_timeout_seconds": 8}
        self.config.update(config or {})

    def _base_enabled(self) -> bool:
        return self._enabled

    def _config_complete(self) -> bool:
        return True

    async def _client(self):
        return self.client

    def _bank_id(self) -> str:
        return self.bank


def record(**overrides):
    base = {
        "schema": SCHEMA,
        "source_plugin": "astrbot_plugin_private_companion",
        "memory_id": "mem-1",
        "kind": "persona_life",
        "content": "今天写日记：午后在窗边看雨。",
        "platform": "aiocqhttp",
        "occurred_at": "2026-10-31T09:00:00+08:00",
        "context": {
            "scope": {"installation_id": "inst-1", "platform": "aiocqhttp",
                      "kind": "private", "native_scope_id": "native-1"},
            "role": "role-7",
        },
    }
    base.update(overrides)
    return base


def companion_context(api=None):
    api = api or FakeExtensionApi()
    return FakeContext([FakeStar(FakeCompanionHost(api)), FakeStar(object(), name="other")]), api


class FindCompanionApiTests(unittest.TestCase):
    def test_finds_activated_companion_star(self):
        context, api = companion_context()
        self.assertIs(find_companion_api(context), api)

    def test_skips_inactive_star(self):
        api = FakeExtensionApi()
        context = FakeContext([FakeStar(FakeCompanionHost(api), activated=False)])
        self.assertIsNone(find_companion_api(context))

    def test_skips_mismatched_api_version(self):
        api = FakeExtensionApi()
        api.memory_provider_api_version = "companion.external-memory.v0"
        context = FakeContext([FakeStar(FakeCompanionHost(api))])
        self.assertIsNone(find_companion_api(context))

    def test_reports_rejection_reason(self):
        api = FakeExtensionApi()
        api.memory_provider_api_version = "v0"
        reasons: list[str] = []
        find_companion_api(FakeContext([FakeStar(FakeCompanionHost(api))]), on_reject=reasons.append)
        self.assertTrue(reasons and "version" in reasons[0])

    def test_returns_none_without_registry(self):
        self.assertIsNone(find_companion_api(object()))


class RecordMappingTests(unittest.TestCase):
    def test_tags_are_persona_scoped_and_hashed(self):
        tags = build_record_tags("salt-123", record())
        self.assertEqual(tags[0], "scope:persona")
        self.assertIn("platform:aiocqhttp", tags)
        self.assertIn("source:companion", tags)
        self.assertTrue(any(tag.startswith("role:") for tag in tags))
        self.assertFalse(any("role-7" in tag for tag in tags))

    def test_metadata_carries_identity_fields(self):
        metadata = build_record_metadata(record())
        self.assertEqual(metadata["memory_id"], "mem-1")
        self.assertEqual(metadata["kind"], "persona_life")

    def test_context_label_is_human_readable(self):
        self.assertIn("diary", build_record_context(record()))
        self.assertIn("proactive", build_record_context(record(kind="proactive_message")))


class BridgeRegistrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_register_declares_model_memory_and_callbacks(self):
        context, api = companion_context()
        plugin = FakePlugin(context)
        bridge = CompanionMemoryBridge(plugin)

        self.assertTrue(await bridge.ensure_registered())
        call = api.calls[0]
        self.assertEqual(call["provider_id"], "astrbot_plugin_hindsight_memory")
        self.assertTrue(call["handles_model_memory"])
        self.assertEqual(call["priority"], 0)
        # 8 秒请求超时 + 2 秒余量
        self.assertEqual(call["timeout_seconds"], 10.0)

    async def test_callback_timeout_follows_request_timeout(self):
        context, api = companion_context()
        plugin = FakePlugin(context, config={"request_timeout_seconds": 30})
        await CompanionMemoryBridge(plugin).ensure_registered()
        self.assertEqual(api.calls[0]["timeout_seconds"], 32.0)

    async def test_disabled_by_default(self):
        context, api = companion_context()
        plugin = FakePlugin(context)
        plugin.config = {"request_timeout_seconds": 8}
        bridge = CompanionMemoryBridge(plugin)

        self.assertFalse(bridge.config_enabled())
        self.assertFalse(await bridge.ensure_registered())
        self.assertEqual(api.calls, [])

    async def test_string_false_is_not_enabled(self):
        context, api = companion_context()
        bridge = CompanionMemoryBridge(FakePlugin(context, config={"companion_provider_enabled": "false"}))
        self.assertFalse(bridge.config_enabled())
        self.assertFalse(await bridge.ensure_registered())
        self.assertEqual(api.calls, [])

    async def test_register_is_idempotent(self):
        context, api = companion_context()
        bridge = CompanionMemoryBridge(FakePlugin(context))
        await bridge.ensure_registered()
        await bridge.ensure_registered()
        self.assertEqual(len(api.calls), 1)

    async def test_register_without_companion_is_noop(self):
        bridge = CompanionMemoryBridge(FakePlugin(FakeContext([])))
        self.assertFalse(await bridge.ensure_registered())

    async def test_reregisters_after_companion_reload(self):
        context, first_api = companion_context()
        plugin = FakePlugin(context)
        bridge = CompanionMemoryBridge(plugin)
        await bridge.ensure_registered()

        # 陪伴插件重载：换了一个新的 extension_api
        second_api = FakeExtensionApi()
        context._stars = [FakeStar(FakeCompanionHost(second_api))]
        self.assertTrue(await bridge.ensure_registered())
        self.assertEqual(len(first_api.unregistered), 1)
        self.assertEqual(len(second_api.calls), 1)

    async def test_unregisters_when_plugin_switch_turns_off(self):
        context, api = companion_context()
        plugin = FakePlugin(context)
        bridge = CompanionMemoryBridge(plugin)
        await bridge.ensure_registered()

        plugin._enabled = False
        self.assertFalse(await bridge.ensure_registered())
        self.assertEqual(len(api.unregistered), 1)

    async def test_disabling_config_unregisters(self):
        context, api = companion_context()
        plugin = FakePlugin(context)
        bridge = CompanionMemoryBridge(plugin)
        await bridge.ensure_registered()

        plugin.config["companion_provider_enabled"] = False
        self.assertFalse(await bridge.ensure_registered())
        self.assertEqual(len(api.unregistered), 1)

    async def test_failed_unregister_keeps_registration_for_retry(self):
        api = FakeExtensionApi(fail_unregister=True)
        context, _ = companion_context(api)
        bridge = CompanionMemoryBridge(FakePlugin(context))
        await bridge.ensure_registered()

        await bridge.disconnect()
        self.assertTrue(bridge.registered)
        api.fail_unregister = False
        await bridge.disconnect()
        self.assertFalse(bridge.registered)

    def test_is_active_follows_plugin_switch(self):
        plugin = FakePlugin(FakeContext([]), enabled=False)
        self.assertFalse(CompanionMemoryBridge(plugin).is_active())
        plugin._enabled = True
        self.assertTrue(CompanionMemoryBridge(plugin).is_active())


class BridgeWriteTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.data_dir = Path(self._tmp.name)

    def _bridge(self, *, enabled: bool = True, config: dict | None = None,
                bank: str = "bank-1") -> CompanionMemoryBridge:
        plugin = FakePlugin(FakeContext([]), enabled=enabled, data_dir=self.data_dir,
                            config=config, bank=bank)
        return CompanionMemoryBridge(plugin)

    async def test_write_retains_record_once(self):
        bridge = self._bridge()
        self.assertEqual(await bridge.write(record()), "mem-1")

        call = bridge.plugin.client.retained[0]
        self.assertEqual(call["bank_id"], "bank-1")
        self.assertIn("scope:persona", call["tags"])
        self.assertEqual(call["timestamp"], "2026-10-31T09:00:00+08:00")

    async def test_write_is_idempotent_across_instances(self):
        await self._bridge().write(record())
        second = self._bridge()
        self.assertEqual(await second.write(record()), "mem-1")
        self.assertEqual(second.plugin.client.retained, [])

    async def test_write_again_after_bank_change(self):
        await self._bridge(bank="bank-1").write(record())
        other = self._bridge(bank="bank-2")
        self.assertEqual(await other.write(record()), "mem-1")
        self.assertEqual(len(other.plugin.client.retained), 1)

    async def test_concurrent_duplicate_writes_only_retain_once(self):
        bridge = self._bridge()
        results = await asyncio.gather(*(bridge.write(record()) for _ in range(5)))
        self.assertEqual(results, ["mem-1"] * 5)
        self.assertEqual(len(bridge.plugin.client.retained), 1)

    async def test_rejects_hard_secret(self):
        bridge = self._bridge()
        self.assertIsNone(await bridge.write(record(content="密码：hunter2hunter2")))
        self.assertEqual(bridge.plugin.client.retained, [])

    async def test_rejects_personal_info_by_default(self):
        bridge = self._bridge()
        self.assertIsNone(await bridge.write(record(content="她留了手机号 13812345678")))
        self.assertEqual(bridge.plugin.client.retained, [])

    async def test_personal_info_allowed_when_opted_in(self):
        bridge = self._bridge(config={"companion_retain_personal_sensitive": True})
        self.assertEqual(await bridge.write(record(content="她留了手机号 13812345678")), "mem-1")
        self.assertEqual(len(bridge.plugin.client.retained), 1)

    async def test_write_rejects_unknown_kind(self):
        bridge = self._bridge()
        self.assertIsNone(await bridge.write(record(kind="private_chat")))

    async def test_write_rejects_blank_content(self):
        self.assertIsNone(await self._bridge().write(record(content="   ")))

    async def test_write_rejects_missing_memory_id(self):
        self.assertIsNone(await self._bridge().write(record(memory_id="")))

    async def test_write_skipped_when_plugin_disabled(self):
        bridge = self._bridge(enabled=False)
        self.assertIsNone(await bridge.write(record()))
        self.assertEqual(bridge.plugin.client.retained, [])

    async def test_write_survives_client_failure(self):
        bridge = self._bridge()
        bridge.plugin.client.fail = True
        self.assertIsNone(await bridge.write(record()))
        # 失败不记入账本，下一次仍然会尝试
        bridge.plugin.client.fail = False
        self.assertEqual(await bridge.write(record()), "mem-1")

    async def test_failed_write_is_queued_and_retried_on_next_success(self):
        bridge = self._bridge()
        bridge.plugin.client.fail = True
        self.assertIsNone(await bridge.write(record(memory_id="mem-a")))
        self.assertEqual(bridge.plugin.client.retained, [])

        bridge.plugin.client.fail = False
        self.assertEqual(await bridge.write(record(memory_id="mem-b")), "mem-b")
        # mem-a 在同一次调用里被顺带补写（当前这条先写，补写跟在后面）
        self.assertEqual(len(bridge.plugin.client.retained), 2)
        written_ids = [call["metadata"]["memory_id"] for call in bridge.plugin.client.retained]
        self.assertEqual(written_ids, ["mem-b", "mem-a"])

    async def test_crash_leftover_blocks_replay(self):
        # 模拟"写完一条、账本还停在 pending 就崩了"：重放必须跳过，不能写第二遍
        store = WrittenMemoryIdStore(self.data_dir)
        store.begin("bank-1", "mem-c")

        bridge = self._bridge()
        self.assertEqual(await bridge.write(record(memory_id="mem-c")), "mem-c")
        self.assertEqual(bridge.plugin.client.retained, [])


class ShutdownTests(unittest.IsolatedAsyncioTestCase):
    async def test_disconnect_waits_for_inflight_write(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        context, api = companion_context()
        plugin = FakePlugin(context, data_dir=Path(tmp.name))
        bridge = CompanionMemoryBridge(plugin)
        await bridge.ensure_registered()

        started = asyncio.Event()
        release = asyncio.Event()
        original = plugin.client.retain

        async def slow_retain(**kwargs):
            started.set()
            await release.wait()
            return await original(**kwargs)

        plugin.client.retain = slow_retain
        task = asyncio.create_task(bridge.write(record()))
        await started.wait()

        disconnect = asyncio.create_task(bridge.disconnect())
        await asyncio.sleep(0.05)
        self.assertFalse(disconnect.done(), "disconnect 应先等待在途写入")

        release.set()
        await task
        await disconnect
        self.assertEqual(len(api.unregistered), 1)


class WrittenMemoryIdStoreTests(unittest.TestCase):
    def test_round_trip_and_bank_isolation(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = WrittenMemoryIdStore(Path(tmp))
            self.assertTrue(store.begin("bank-1", "a"))
            store.commit("bank-1", "a")
            reloaded = WrittenMemoryIdStore(Path(tmp))
            self.assertTrue(reloaded.contains("bank-1", "a"))
            self.assertFalse(reloaded.contains("bank-2", "a"))

    def test_begin_twice_returns_false(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = WrittenMemoryIdStore(Path(tmp))
            self.assertTrue(store.begin("bank-1", "a"))
            # 第一次没提交就再来一次：说明上次可能已经写过，必须跳过
            self.assertFalse(store.begin("bank-1", "a"))

    def test_abort_allows_retry(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = WrittenMemoryIdStore(Path(tmp))
            store.begin("bank-1", "a")
            store.abort("bank-1", "a")
            self.assertTrue(store.begin("bank-1", "a"))

    def test_legacy_list_format_is_not_migrated(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "companion_written_ids.json"
            path.write_text('["legacy-1", "legacy-2"]', encoding="utf-8")
            store = WrittenMemoryIdStore(Path(tmp))
            # 旧格式没有 bank 信息，宁可不认，也不把记录写到错的 bank
            self.assertFalse(store.contains("bank-1", "legacy-1"))

    def test_cap_evicts_oldest(self):
        import astrbot_plugin_hindsight_memory.companion_bridge as cb
        original = cb.MAX_WRITTEN_IDS
        cb.MAX_WRITTEN_IDS = 20
        try:
            with tempfile.TemporaryDirectory() as tmp:
                store = WrittenMemoryIdStore(Path(tmp))
                for index in range(25):
                    store.begin("bank-1", f"id-{index}")
                    store.commit("bank-1", f"id-{index}")
                reloaded = WrittenMemoryIdStore(Path(tmp))
                self.assertTrue(reloaded.contains("bank-1", "id-24"))
                self.assertFalse(reloaded.contains("bank-1", "id-0"))
        finally:
            cb.MAX_WRITTEN_IDS = original


if __name__ == "__main__":
    unittest.main()
