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
"""

from __future__ import annotations

from collections.abc import Mapping
import json
import logging
from pathlib import Path
from typing import Any

from .scope import hash_identifier


SCHEMA = "companion.external-memory.v1"
COMPANION_PLUGIN = "astrbot_plugin_private_companion"
PROVIDER_ID = "astrbot_plugin_hindsight_memory"
DISPLAY_NAME = "Hindsight Memory"
WRITTEN_IDS_FILE = "companion_written_ids.json"
MAX_WRITTEN_IDS = 1000
ACCEPTED_KINDS = ("persona_life", "proactive_message")
# 写一条记录要过一趟 Hindsight Cloud；5 秒（接口示例的默认值）在网络稍慢时会超时，
# 而宿主对超时的写入不重试，所以这里给得宽一些。
WRITE_TIMEOUT_SECONDS = 10.0
CONFIG_KEY = "companion_provider_enabled"


def _log_warning(message: str) -> None:
    logger = logging.getLogger(__name__)
    logger.warning(f"Hindsight {message}")


def find_companion_api(context: Any) -> Any | None:
    """在已启用的星里找陪伴插件，返回它暴露的 ``extension_api``。

    接口版本不匹配时直接放弃：宁可这次不协同，也不调用来历不明的同名方法。
    """
    getter = getattr(context, "get_all_stars", None)
    if not callable(getter):
        return None
    try:
        result: Any = getter()
    except Exception:
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
        if getattr(api, "memory_provider_api_version", None) != SCHEMA:
            continue
        if not callable(getattr(api, "register_memory_provider", None)):
            continue
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
    """``memory_id`` 幂等表：同一条记录重放时不再写第二遍。"""

    def __init__(self, data_dir: Path) -> None:
        self.path = data_dir / WRITTEN_IDS_FILE
        self._ids: list[str] = self._load()

    def _load(self) -> list[str]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        if not isinstance(raw, list):
            return []
        return [str(item) for item in raw if isinstance(item, str)]

    def contains(self, memory_id: str) -> bool:
        return memory_id in self._ids

    def add(self, memory_id: str) -> None:
        self._ids.append(memory_id)
        if len(self._ids) > MAX_WRITTEN_IDS:
            self._ids = self._ids[-MAX_WRITTEN_IDS:]
        tmp = self.path.with_name(f"{self.path.name}.tmp")
        try:
            tmp.write_text(json.dumps(self._ids, ensure_ascii=False), encoding="utf-8")
            tmp.replace(self.path)
        except OSError as exc:  # 幂等表写不进去不该影响记忆写入
            _log_warning(f"written-id store could not be saved: {exc}")
            if tmp.exists():
                tmp.unlink()


class CompanionMemoryBridge:
    """陪伴插件的记忆 Provider 注册与写入通道。"""

    def __init__(self, plugin: Any) -> None:
        self.plugin = plugin
        self._api: Any | None = None
        self._registration: Any | None = None
        self._failure_logged = False
        data_dir = getattr(getattr(plugin, "store", None), "data_dir", None)
        self._written = WrittenMemoryIdStore(Path(data_dir)) if data_dir is not None else None

    # ---- 注册生命周期 ----

    def config_enabled(self) -> bool:
        """配置项 ``companion_provider_enabled``；缺省视为开启。"""
        config = getattr(self.plugin, "config", None)
        if config is None:
            return True
        try:
            return bool(config.get(CONFIG_KEY, True))
        except Exception:
            return True

    def is_active(self) -> bool:
        """陪伴插件每次调用前都会问一次；这里把「插件开着且配置完整」如实反映出去。"""
        if not self.config_enabled():
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

    async def ensure_registered(self) -> bool:
        """幂等地注册。陪伴插件不在、未启用或接口版本不符时什么都不做。"""
        if not self.config_enabled():
            if self.registered:
                await self.disconnect()
            return False
        if self.registered:
            return True
        context = getattr(self.plugin, "_context", None)
        api = find_companion_api(context)
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
                timeout_seconds=WRITE_TIMEOUT_SECONDS,
            )
        except Exception as exc:
            if not self._failure_logged:
                _log_warning(f"memory provider registration failed: {exc}")
                self._failure_logged = True
            return False
        self._api, self._registration = api, registration
        self._failure_logged = False
        return True

    async def disconnect(self) -> None:
        api, registration = self._api, self._registration
        self._api = self._registration = None
        if api is None or registration is None:
            return
        unregister = getattr(api, "unregister_memory_provider", None)
        if not callable(unregister):
            return
        try:
            unregister(registration)
        except Exception as exc:
            _log_warning(f"memory provider unregister failed: {exc}")

    # ---- 写入 ----

    async def write(self, record: Any) -> str | None:
        """接收陪伴插件的日记、梦境与主动消息，写入 Hindsight。

        返回记录里的 ``memory_id`` 表示已接受；返回 ``None`` 表示拒收（未知类型、
        无法归属、插件未启用或客户端失败）。宿主不会重试，原始记录仍在它自己库里。
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
        if self._written is not None and self._written.contains(memory_id):
            return memory_id

        salt = str(getattr(self.plugin, "salt", "") or "")
        timestamp = record.get("occurred_at")
        try:
            client = await self.plugin._client()
            await client.retain(
                bank_id=self.plugin._bank_id(),
                content=content.strip(),
                tags=build_record_tags(salt, record),
                metadata=build_record_metadata(record),
                context=build_record_context(record),
                timestamp=str(timestamp) if isinstance(timestamp, str) and timestamp else None,
            )
        except Exception as exc:
            _log_warning(f"companion record write failed: {exc}")
            return None

        if self._written is not None:
            self._written.add(memory_id)
        return memory_id
