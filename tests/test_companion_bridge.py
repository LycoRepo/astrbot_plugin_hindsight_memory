from pathlib import Path
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
    build_record_metadata,
    build_record_tags,
    find_companion_api,
)


class FakeRegistration:
    def __init__(self, provider_id: str) -> None:
        self.provider_id = provider_id


class FakeExtensionApi:
    memory_provider_api_version = SCHEMA

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.unregistered: list[FakeRegistration] = []

    def register_memory_provider(self, provider_id, **kwargs):
        self.calls.append({"provider_id": provider_id, **kwargs})
        return FakeRegistration(provider_id)

    def unregister_memory_provider(self, registration):
        self.unregistered.append(registration)
        return True


class FakeStar:
    def __init__(self, name: str, api, activated: bool = True) -> None:
        self.name = name
        self.root_dir_name = name
        self.activated = activated
        self.star_cls = type("Star", (), {"extension_api": api})


class FakeContext:
    def __init__(self, stars) -> None:
        self._stars = stars

    def get_all_stars(self):
        return self._stars


class FakeRetainClient:
    def __init__(self) -> None:
        self.retained: list[dict] = []

    async def retain(self, **kwargs):
        self.retained.append(kwargs)
        return {"status": "accepted"}


class FakePlugin:
    def __init__(self, context, *, enabled: bool = True, data_dir: Path | None = None,
                 config: dict | None = None) -> None:
        self._context = context
        self.salt = "salt-123"
        self._enabled = enabled
        self.client = FakeRetainClient()
        self.store = type("Store", (), {"data_dir": data_dir})()
        self.bank = "bank-1"
        self.config = dict(config or {})

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


class FindCompanionApiTests(unittest.TestCase):
    def test_finds_activated_companion_star(self):
        api = FakeExtensionApi()
        context = FakeContext([FakeStar("astrbot_plugin_private_companion", api)])
        self.assertIs(find_companion_api(context), api)

    def test_skips_inactive_star(self):
        api = FakeExtensionApi()
        context = FakeContext([FakeStar("astrbot_plugin_private_companion", api, activated=False)])
        self.assertIsNone(find_companion_api(context))

    def test_skips_mismatched_api_version(self):
        api = FakeExtensionApi()
        api.memory_provider_api_version = "companion.external-memory.v0"
        context = FakeContext([FakeStar("astrbot_plugin_private_companion", api)])
        self.assertIsNone(find_companion_api(context))

    def test_returns_none_without_registry(self):
        self.assertIsNone(find_companion_api(object()))


class RecordMappingTests(unittest.TestCase):
    def test_tags_are_persona_scoped_and_hashed(self):
        tags = build_record_tags("salt-123", record())
        self.assertEqual(tags[0], "scope:persona")
        self.assertIn("platform:aiocqhttp", tags)
        self.assertIn("source:companion", tags)
        self.assertTrue(any(tag.startswith("role:") for tag in tags))
        self.assertTrue(any(tag.startswith("installation:") for tag in tags))
        # 原始角色 ID 不应出现在 tag 里
        self.assertFalse(any("role-7" in tag for tag in tags))

    def test_metadata_carries_identity_fields(self):
        metadata = build_record_metadata(record())
        self.assertEqual(metadata["memory_id"], "mem-1")
        self.assertEqual(metadata["kind"], "persona_life")
        self.assertEqual(metadata["occurred_at"], "2026-10-31T09:00:00+08:00")

    def test_metadata_skips_unknown_types(self):
        metadata = build_record_metadata(record(confidence={"bad": "dict"}, importance=True))
        self.assertNotIn("confidence", metadata)
        self.assertNotIn("importance", metadata)


class BridgeRegistrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_register_declares_model_memory_and_callbacks(self):
        api = FakeExtensionApi()
        plugin = FakePlugin(FakeContext([FakeStar("astrbot_plugin_private_companion", api)]))
        bridge = CompanionMemoryBridge(plugin)

        self.assertTrue(await bridge.ensure_registered())
        self.assertEqual(len(api.calls), 1)
        call = api.calls[0]
        self.assertEqual(call["provider_id"], "astrbot_plugin_hindsight_memory")
        self.assertTrue(call["handles_model_memory"])
        self.assertIs(call["plugin"], plugin)
        self.assertTrue(callable(call["write"]))
        self.assertTrue(callable(call["is_active"]))
        # 与接口示例一致使用默认优先级
        self.assertEqual(call["priority"], 0)
        self.assertEqual(call["timeout_seconds"], 10.0)

    async def test_register_is_idempotent(self):
        api = FakeExtensionApi()
        plugin = FakePlugin(FakeContext([FakeStar("astrbot_plugin_private_companion", api)]))
        bridge = CompanionMemoryBridge(plugin)

        await bridge.ensure_registered()
        await bridge.ensure_registered()
        self.assertEqual(len(api.calls), 1)

    async def test_register_without_companion_is_noop(self):
        plugin = FakePlugin(FakeContext([]))
        bridge = CompanionMemoryBridge(plugin)

        self.assertFalse(await bridge.ensure_registered())
        self.assertFalse(bridge.registered)

    async def test_register_skipped_when_config_disabled(self):
        api = FakeExtensionApi()
        plugin = FakePlugin(FakeContext([FakeStar("astrbot_plugin_private_companion", api)]),
                            config={"companion_provider_enabled": False})
        bridge = CompanionMemoryBridge(plugin)

        self.assertFalse(await bridge.ensure_registered())
        self.assertEqual(api.calls, [])
        self.assertFalse(bridge.is_active())

    async def test_disabling_config_unregisters(self):
        api = FakeExtensionApi()
        plugin = FakePlugin(FakeContext([FakeStar("astrbot_plugin_private_companion", api)]))
        bridge = CompanionMemoryBridge(plugin)
        await bridge.ensure_registered()

        plugin.config["companion_provider_enabled"] = False
        self.assertFalse(await bridge.ensure_registered())
        self.assertEqual(len(api.unregistered), 1)
        self.assertFalse(bridge.registered)

    async def test_disconnect_unregisters(self):
        api = FakeExtensionApi()
        plugin = FakePlugin(FakeContext([FakeStar("astrbot_plugin_private_companion", api)]))
        bridge = CompanionMemoryBridge(plugin)
        await bridge.ensure_registered()

        await bridge.disconnect()
        self.assertEqual(len(api.unregistered), 1)
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

    def _bridge(self, enabled: bool = True) -> CompanionMemoryBridge:
        plugin = FakePlugin(FakeContext([]), enabled=enabled, data_dir=self.data_dir)
        return CompanionMemoryBridge(plugin)

    async def test_write_retains_record_once(self):
        bridge = self._bridge()
        memory_id = await bridge.write(record())

        self.assertEqual(memory_id, "mem-1")
        plugin = bridge.plugin
        self.assertEqual(len(plugin.client.retained), 1)
        retained = plugin.client.retained[0]
        self.assertEqual(retained["bank_id"], "bank-1")
        self.assertEqual(retained["content"], "今天写日记：午后在窗边看雨。")
        self.assertIn("scope:persona", retained["tags"])
        self.assertEqual(retained["timestamp"], "2026-10-31T09:00:00+08:00")

    async def test_write_is_idempotent_across_instances(self):
        first = self._bridge()
        await first.write(record())

        second = self._bridge()
        self.assertEqual(await second.write(record()), "mem-1")
        self.assertEqual(len(second.plugin.client.retained), 0)

    async def test_write_rejects_unknown_kind(self):
        bridge = self._bridge()
        self.assertIsNone(await bridge.write(record(kind="private_chat")))
        self.assertEqual(len(bridge.plugin.client.retained), 0)

    async def test_write_rejects_blank_content(self):
        bridge = self._bridge()
        self.assertIsNone(await bridge.write(record(content="   ")))

    async def test_write_rejects_missing_memory_id(self):
        bridge = self._bridge()
        self.assertIsNone(await bridge.write(record(memory_id="")))

    async def test_write_skipped_when_plugin_disabled(self):
        bridge = self._bridge(enabled=False)
        self.assertIsNone(await bridge.write(record()))
        self.assertEqual(len(bridge.plugin.client.retained), 0)

    async def test_write_survives_client_failure(self):
        bridge = self._bridge()

        async def boom(**kwargs):
            raise RuntimeError("hindsight down")

        bridge.plugin.client.retain = boom
        self.assertIsNone(await bridge.write(record()))


class WrittenMemoryIdStoreTests(unittest.TestCase):
    def test_round_trip_and_cap(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = WrittenMemoryIdStore(Path(tmp))
            store.add("a")
            store.add("b")
            self.assertTrue(WrittenMemoryIdStore(Path(tmp)).contains("b"))

            for index in range(1200):
                store.add(f"id-{index}")
            reloaded = WrittenMemoryIdStore(Path(tmp))
            self.assertTrue(reloaded.contains("id-1199"))
            self.assertFalse(reloaded.contains("a"))


if __name__ == "__main__":
    unittest.main()
