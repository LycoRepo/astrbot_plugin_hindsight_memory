"""把 Hindsight 注册为陪伴插件（astrbot_plugin_private_companion）的外部记忆 Provider。

接口版本是 ``companion.external-memory.v1``：第三方插件从陪伴插件实例的
``extension_api`` 上注册回调即可，不需要改名为某个指定插件、也不需要继承内部类。

本模块只负责三件事：

1. 找到在跑的陪伴插件并注册（``register_memory_provider``）；
2. 注册时声明 ``handles_model_memory=True`` —— 记忆的注入与检索本来就在
   ``on_llm_request`` / ``on_llm_response`` 里完成，因此由 Hindsight 承担模型侧记忆，
   让陪伴插件自己的内置记忆注入与记忆工具让位，避免模型同时收到两份叙事；
3. 承接陪伴插件产出的日记、梦境和主动消息（``write``），按 ``memory_id`` 幂等写入。

召回（``read``）不注册：本插件的召回走 AstrBot 的 LLM 钩子，不走 provider 的 read 回调。
统计（``summary``）暂不注册：Hindsight Cloud 没有对应的计数接口，接口本身允许省略。

几个刻意的取舍，与接口契约一致：

* 宿主对 ``write`` **不重试也不缓冲**（``memory_push`` 的规矩），所以这里失败即放弃，
  原始记录仍在陪伴插件自己的库里；补写属于维护工作，不该压在热路径上。
* 幂等账本按 ``bank + memory_id`` 记账：换 bank 之后同一条记录会重新写一次到新目的地。
* 崩溃窗口（Hindsight 已接收、账本还没落盘）无法彻底消除——Hindsight 没有幂等键。
  这里选择"宁可重放一次，也不丢记录"：重放的记录靠 ``memory_id`` 仍可辨认。
* 关闭某个会话的 ``/hindsight off`` 或对应 scope 开关时，本插件不再为那个会话召回，
  而陪伴插件已经把记忆让给了本插件——那个会话会暂时没有长期记忆。provider 注册是
  安装级、拿不到会话 scope，接口没有提供按 scope 让位的办法，README 里已写明。
"""

from __future__ import annotations

from collections.abc import Mapping
import asyncio
import json
import logging
from pathlib import Path
import time
from typing import Any

from .retention_policy import (
    _bool_config,
    _has_hard_sensitive_secret,
    _has_personal_sensitive,
)
from .scope import hash_identifier


SCHEMA = "companion.external-memory.v1"
COMPANION_PLUGIN = "astrbot_plugin_private_companion"
PROVIDER_ID = "astrbot_plugin_hindsight_memory"
DISPLAY_NAME = "Hindsight Memory"
WRITTEN_IDS_FILE = "companion_written_ids.json"
PENDING_FILE = "companion_pending_records.json"
MAX_PENDING_RECORDS = 50
PENDING_TTL_SECONDS = 24 * 3600
ACCEPTED_KINDS = ("persona_life", "proactive_message")
CONFIG_KEY = "companion_provider_enabled"
PERSONAL_KEY = "companion_retain_personal_sensitive"
WARN_INTERVAL_SECONDS = 3600.0

# 幂等账本：按时间保留 30 天，超过 MAX 条再按最旧淘汰。
MAX_WRITTEN_IDS = 5000
WRITTEN_ID_TTL_SECONDS = 30 * 24 * 3600

# 回调超时：至少 10 秒，并且比插件自己的 Hindsight 请求超时更宽，否则宿主会先放弃。
MIN_WRITE_TIMEOUT_SECONDS = 10.0
TIMEOUT_MARGIN_SECONDS = 2.0

# 并发写同一个 memory_id 时用锁串行化；锁表超过这个规模就整体清空（都是空闲锁）。
MAX_WRITE_LOCKS = 512


def _log_warning(message: str) -> None:
    logger = logging.getLogger(__name__)
    logger.warning(f"Hindsight {message}")


def find_companion_api(context: Any, *, on_reject: Any = None) -> Any | None:
    """在已启用的星里找陪伴插件，返回它暴露的 ``extension_api``。

    接口版本不匹配时直接放弃：宁可这次不协同，也不调用来历不明的同名方法。
    ``on_reject`` 会在"找到了陪伴插件但用不了"时收到一句原因，便于限流告警。
    """
    getter = getattr(context, "get_all_stars", None)
    if not callable(getter):
        return None
    try:
        result: Any = getter()
    except Exception as exc:
        if on_reject is not None:
            on_reject(f"companion star discovery failed: {exc}")
        return None
    if not isinstance(result, (list, tuple)):
        return None
    for star in result:
        try:
            if not bool(getattr(star, "activated", True)):
                continue
            names = (getattr(star, "root_dir_name", "") or "", getattr(star, "name", "") or "")
        except Exception:
            continue
        if COMPANION_PLUGIN not in names:
            continue
        api = getattr(getattr(star, "star_cls", None), "extension_api", None)
        version = getattr(api, "memory_provider_api_version", None)
        if version != SCHEMA:
            if on_reject is not None:
                on_reject(f"companion extension_api version is {version!r}, expected {SCHEMA!r}")
            return None
        if not callable(getattr(api, "register_memory_provider", None)):
            if on_reject is not None:
                on_reject("companion extension_api has no register_memory_provider")
            return None
        return api
    return None


def _clean_tag_value(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    return "".join(ch if (ch.isalnum() or ch in "-_.") else "_" for ch in text)[:64]


def record_scopes(record: Mapping[str, Any]) -> Mapping[str, Any]:
    context = record.get("context")
    if not isinstance(context, Mapping):
        return {}
    return context


def build_record_tags(salt: str, record: Mapping[str, Any]) -> list[str]:
    """按记录的归属生成 tag。

    陪伴插件的日记/梦境是角色自己的生活记录，主动消息也属于角色，因此统一落在
    ``scope:persona`` 这一档：不按某个私聊用户召回，避免把角色日记当成用户记忆。
    平台和角色仍各自打 tag，方便以后按角色或平台取用。
    """
    context = record_scopes(record)
    raw_scope = context.get("scope")
    scope: Mapping[str, Any] = raw_scope if isinstance(raw_scope, Mapping) else {}
    platform = _clean_tag_value(record.get("platform") or scope.get("platform") or "unknown")
    tags = ["scope:persona", f"platform:{platform}", "source:companion"]
    role = _clean_tag_value(context.get("role") or "")
    if role:
        tags.append(f"role:{hash_identifier(salt, 'role', role)}")
    installation = _clean_tag_value(scope.get("installation_id") or "")
    if installation:
        tags.append(f"installation:{hash_identifier(salt, 'installation', installation)}")
    return tags


def build_record_metadata(record: Mapping[str, Any]) -> dict[str, Any]:
    metadata: dict[str, Any] = {
        "source": "astrbot_plugin_private_companion",
        "source_plugin": str(record.get("source_plugin") or ""),
        "memory_id": str(record.get("memory_id") or ""),
        "kind": str(record.get("kind") or ""),
    }
    for key in ("occurred_at", "confidence", "importance", "session_id"):
        value = record.get(key)
        if isinstance(value, (str, int, float)) and not isinstance(value, bool):
            metadata[key] = value
    tags = record.get("tags")
    if isinstance(tags, (list, tuple)):
        cleaned = [str(item) for item in tags if isinstance(item, (str, int, float))][:16]
        if cleaned:
            metadata["record_tags"] = json.dumps(cleaned, ensure_ascii=False)
    return metadata


def build_record_context(record: Mapping[str, Any]) -> str:
    """给 Hindsight 抽取提示的短标签；沿用插件里 ``_retain_context`` 的写法。"""
    kind = str(record.get("kind") or "")
    if kind == "persona_life":
        return "Chatbot long-term memory: the character's own diary or dream record"
    if kind == "proactive_message":
        return "Chatbot long-term memory: the character's proactive message"
    return "Chatbot long-term memory"


class WrittenMemoryIdStore:
    """``bank + memory_id`` 两阶段账本：先占位，写入成功才落 ``done``。

    这样处理崩溃窗口：Hindsight 已接收但账本没来得及记 ``done`` 时，文件里会留下一条
    ``pending``。重放看到 ``pending`` 就跳过——宁可少一条（原件仍在陪伴插件自己的库里，
    可以人工补），也不要同一篇日记在记忆里出现两次。

    条目带写入时间戳：读入时丢掉超过 ``WRITTEN_ID_TTL_SECONDS`` 的，超过
    ``MAX_WRITTEN_IDS`` 再按最旧淘汰。
    """

    def __init__(self, data_dir: Path) -> None:
        self.path = data_dir / WRITTEN_IDS_FILE
        self._entries: dict[str, dict[str, Any]] = self._load()

    @staticmethod
    def key(bank_id: str, memory_id: str) -> str:
        return f"{bank_id}|{memory_id}"

    def _load(self) -> dict[str, dict[str, Any]]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        now = time.time()
        entries: dict[str, dict[str, Any]] = {}
        if isinstance(raw, dict):
            for key, value in raw.items():
                if not isinstance(key, str):
                    continue
                if isinstance(value, dict):
                    state = str(value.get("state") or "done")
                    stamp = float(value.get("ts") or now)
                else:
                    # 上一版存的是 key -> 时间戳，统一按已完成处理
                    state, stamp = "done", float(value) if isinstance(value, (int, float)) else now
                if now - stamp < WRITTEN_ID_TTL_SECONDS:
                    entries[key] = {"state": state, "ts": stamp}
        # 更早的版本存的是不带 bank 的纯 ID 列表，无法映射到具体 bank：不迁移。
        return entries

    def contains(self, bank_id: str, memory_id: str) -> bool:
        return self.key(bank_id, memory_id) in self._entries

    def begin(self, bank_id: str, memory_id: str) -> bool:
        """占位。返回 False 表示这条记录已经写过或正在写，调用方应当跳过。"""
        key = self.key(bank_id, memory_id)
        if key in self._entries:
            return False
        self._entries[key] = {"state": "pending", "ts": time.time()}
        self._flush()
        return True

    def commit(self, bank_id: str, memory_id: str) -> None:
        self._entries[self.key(bank_id, memory_id)] = {"state": "done", "ts": time.time()}
        self._trim()
        self._flush()

    def abort(self, bank_id: str, memory_id: str) -> None:
        """写入失败：撤掉占位，允许下一次重试。"""
        if self._entries.pop(self.key(bank_id, memory_id), None) is not None:
            self._flush()

    def _trim(self) -> None:
        if len(self._entries) > MAX_WRITTEN_IDS:
            keep = sorted(self._entries.items(), key=lambda item: item[1].get("ts", 0))[-MAX_WRITTEN_IDS:]
            self._entries = dict(keep)

    def _flush(self) -> None:
        tmp = self.path.with_name(f"{self.path.name}.tmp")
        try:
            tmp.write_text(json.dumps(self._entries, ensure_ascii=False), encoding="utf-8")
            tmp.replace(self.path)
        except OSError as exc:
            # 账本写不进去不该让已经成功的写入变成失败（否则宿主那边就是一次失败）。
            _log_warning(f"written-id store could not be saved: {exc}")
            try:
                if tmp.exists():
                    tmp.unlink()
            except OSError as cleanup_exc:
                _log_warning(f"written-id temp file could not be removed: {cleanup_exc}")


class PendingRecordStore:
    """写失败的记录在这里短期排队，下一次成功写入时顺带补写。

    接口契约要求 host 不重试，但这条旁路的失败没必要直接丢：留下的是一份精简副本，
    上限 ``MAX_PENDING_RECORDS`` 条、超过 ``PENDING_TTL_SECONDS`` 丢弃，避免无限堆积。
    """

    def __init__(self, data_dir: Path) -> None:
        self.path = data_dir / PENDING_FILE

    def add(self, record: Mapping[str, Any]) -> None:
        items = self._load()
        items.append({"ts": time.time(), "record": dict(record)})
        if len(items) > MAX_PENDING_RECORDS:
            items = items[-MAX_PENDING_RECORDS:]
        self._save(items)

    def drain(self) -> list[dict[str, Any]]:
        """取出可以重试的记录并清空文件（重试失败的会重新入队）。"""
        items = self._load()
        self._save([])
        now = time.time()
        return [item["record"] for item in items
                if isinstance(item.get("record"), dict) and now - float(item.get("ts") or now) < PENDING_TTL_SECONDS]

    def _load(self) -> list[dict[str, Any]]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        return [item for item in raw if isinstance(item, dict)] if isinstance(raw, list) else []

    def _save(self, items: list[dict[str, Any]]) -> None:
        tmp = self.path.with_name(f"{self.path.name}.tmp")
        try:
            tmp.write_text(json.dumps(items, ensure_ascii=False), encoding="utf-8")
            tmp.replace(self.path)
        except OSError as exc:
            _log_warning(f"pending record store could not be saved: {exc}")
            try:
                if tmp.exists():
                    tmp.unlink()
            except OSError as cleanup_exc:
                _log_warning(f"pending temp file could not be removed: {cleanup_exc}")


class CompanionMemoryBridge:
    """陪伴插件的记忆 Provider 注册与写入通道。"""

    def __init__(self, plugin: Any) -> None:
        self.plugin = plugin
        self._api: Any | None = None
        self._registration: Any | None = None
        self._warned_at = 0.0
        self._write_locks: dict[str, asyncio.Lock] = {}
        self._inflight = 0
        self._idle = asyncio.Event()
        self._idle.set()
        data_dir = getattr(getattr(plugin, "store", None), "data_dir", None)
        self._written = WrittenMemoryIdStore(Path(data_dir)) if data_dir is not None else None
        self._pending = PendingRecordStore(Path(data_dir)) if data_dir is not None else None
        self._flushing = False

    # ---- 配置 ----

    def _cfg_bool(self, key: str, default: bool) -> bool:
        config = getattr(self.plugin, "config", None)
        if config is None:
            return default
        try:
            return _bool_config(config, key, default)
        except Exception:
            return default

    def config_enabled(self) -> bool:
        """「接入陪伴插件」开关；默认关闭——升级不会改变已有安装的行为。"""
        return self._cfg_bool(CONFIG_KEY, False)

    def personal_allowed(self) -> bool:
        """是否允许把含个人信息（邮箱/手机号/身份证/地址）的记录写进 Hindsight。"""
        return self._cfg_bool(PERSONAL_KEY, False)

    def _warn(self, message: str) -> None:
        """限流告警：同一小时内只留一条，避免每轮刷屏。"""
        now = time.time()
        if now - self._warned_at < WARN_INTERVAL_SECONDS:
            return
        self._warned_at = now
        _log_warning(message)

    # ---- 注册生命周期 ----

    def _plugin_enabled(self) -> bool:
        checker = getattr(self.plugin, "_base_enabled", None)
        if not callable(checker):
            return True
        try:
            return bool(checker())
        except Exception:
            return False

    def is_active(self) -> bool:
        """陪伴插件每次调用前都会问一次；这里把「插件开着且配置完整」如实反映出去。"""
        if not self.config_enabled() or not self._plugin_enabled():
            return False
        for name in ("_base_enabled", "_config_complete"):
            checker = getattr(self.plugin, name, None)
            if not callable(checker):
                continue
            try:
                if not checker():
                    return False
            except Exception:
                return False
        return True

    @property
    def registered(self) -> bool:
        return self._api is not None and self._registration is not None

    def _callback_timeout(self) -> float:
        """回调超时跟着插件自己的请求超时走，避免宿主先于 Hindsight 放弃。"""
        request_timeout = 8.0
        config = getattr(self.plugin, "config", None)
        if config is not None:
            try:
                request_timeout = float(config.get("request_timeout_seconds", 8.0) or 8.0)
            except (TypeError, ValueError):
                request_timeout = 8.0
        return max(MIN_WRITE_TIMEOUT_SECONDS, request_timeout + TIMEOUT_MARGIN_SECONDS)

    async def ensure_registered(self) -> bool:
        """幂等地注册。陪伴插件不在、未启用或接口版本不符时什么都不做。"""
        if not self.config_enabled() or not self._plugin_enabled():
            if self.registered:
                await self.disconnect()
            return False
        context = getattr(self.plugin, "_context", None)
        api = find_companion_api(context, on_reject=self._warn)
        if self.registered:
            if api is self._api:
                return True
            # 陪伴插件重载后是一个新的 extension_api：旧注册不会跟过来，必须重新注册。
            await self.disconnect()
        if api is None:
            return False
        try:
            registration = api.register_memory_provider(
                PROVIDER_ID,
                plugin=self.plugin,
                display_name=DISPLAY_NAME,
                write=self.write,
                is_active=self.is_active,
                handles_model_memory=True,
                # 与接口示例一致用默认优先级 0；若同时装了「我会牢牢记住你」，
                # 两个 provider 会被按 provider_id 排序取第一个，建议只保留一个。
                priority=0,
                timeout_seconds=self._callback_timeout(),
            )
        except Exception as exc:
            self._warn(f"memory provider registration failed: {exc}")
            return False
        self._api, self._registration = api, registration
        return True

    async def disconnect(self) -> None:
        """注销注册；失败时保留引用，下一次还会重试。"""
        api, registration = self._api, self._registration
        if api is None or registration is None:
            return
        await self._wait_idle(5.0)
        unregister = getattr(api, "unregister_memory_provider", None)
        if callable(unregister):
            try:
                unregister(registration)
            except Exception as exc:
                # 保留引用：注册表可能还挂着这条记录，下一次 ensure_registered 会再试。
                self._warn(f"memory provider unregister failed: {exc}")
                return
        self._api = self._registration = None

    # ---- 并发与在途写入 ----

    def _lock_for(self, key: str) -> asyncio.Lock:
        lock = self._write_locks.get(key)
        if lock is None:
            if len(self._write_locks) > MAX_WRITE_LOCKS:
                self._write_locks.clear()
            lock = asyncio.Lock()
            self._write_locks[key] = lock
        return lock

    def _begin_write(self) -> None:
        self._inflight += 1
        self._idle.clear()

    def _end_write(self) -> None:
        self._inflight = max(0, self._inflight - 1)
        if self._inflight == 0:
            self._idle.set()

    async def _wait_idle(self, timeout: float) -> None:
        if self._inflight == 0:
            return
        try:
            await asyncio.wait_for(self._idle.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            self._warn(f"shutdown wait for {self._inflight} in-flight write(s) timed out")

    # ---- 写入 ----

    def _reject_reason(self, content: str) -> str:
        """复用抓取/保存路径的同一套判定：硬凭据一律不写，个人信息默认不写。"""
        if _has_hard_sensitive_secret(content):
            return "hard_sensitive"
        if not self.personal_allowed() and _has_personal_sensitive(content):
            return "personal_sensitive"
        return ""

    async def write(self, record: Any) -> str | None:
        """接收陪伴插件的日记、梦境与主动消息，写入 Hindsight。

        返回记录里的 ``memory_id`` 表示已接受；返回 ``None`` 表示拒收（未知类型、
        无法归属、含敏感内容、插件未启用或客户端失败）。宿主不会重试，原始记录仍在
        它自己库里。
        """
        if not isinstance(record, Mapping):
            return None
        schema = record.get("schema")
        if schema is not None and schema != SCHEMA:
            return None
        if record.get("kind") not in ACCEPTED_KINDS:
            return None
        content = record.get("content")
        if not isinstance(content, str) or not content.strip():
            return None
        memory_id = record.get("memory_id")
        if not isinstance(memory_id, str) or not memory_id.strip():
            return None
        if not self.is_active():
            return None
        reason = self._reject_reason(content)
        if reason:
            self._warn(f"companion record rejected ({reason})")
            return None

        bank_id = str(self.plugin._bank_id())
        lock = self._lock_for(WrittenMemoryIdStore.key(bank_id, memory_id))
        accepted = False
        async with lock:
            if self._written is not None and not self._written.begin(bank_id, memory_id):
                # 已经写过，或者上次写到一半（崩溃前的 pending）：跳过，不重复写
                accepted = True
            else:
                salt = str(getattr(self.plugin, "salt", "") or "")
                timestamp = record.get("occurred_at")
                written = False
                self._begin_write()
                try:
                    client = await self.plugin._client()
                    await client.retain(
                        bank_id=bank_id,
                        content=content.strip(),
                        tags=build_record_tags(salt, record),
                        metadata=build_record_metadata(record),
                        context=build_record_context(record),
                        timestamp=str(timestamp) if isinstance(timestamp, str) and timestamp else None,
                    )
                    written = True
                except Exception as exc:
                    _log_warning(f"companion record write failed: {exc}")
                finally:
                    self._end_write()

                if written:
                    if self._written is not None:
                        self._written.commit(bank_id, memory_id)
                    accepted = True
                else:
                    if self._written is not None:
                        self._written.abort(bank_id, memory_id)
                    if self._pending is not None:
                        self._pending.add(record)

        # 补写在锁外进行：同一 memory_id 的重试要重新拿这把锁，持锁调用会自己等自己
        if accepted:
            await self._flush_pending()
            return memory_id
        return None

    async def _flush_pending(self) -> None:
        """补写之前失败的记录；一次只跑一层，避免和正常写入互相递归。"""
        if self._pending is None or self._flushing:
            return
        records = self._pending.drain()
        if not records:
            return
        self._flushing = True
        try:
            for record in records:
                await self.write(record)
        finally:
            self._flushing = False
