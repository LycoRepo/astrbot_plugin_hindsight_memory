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

几个刻意的取舍：

* 宿主对 ``write`` **不重试也不缓冲**（``memory_push`` 的规矩），热路径上失败就结束；
  只有**确定没被接收**的失败会进本地队列，等下一次成功写入时按条补写。
* 结果未知（超时、连接中途断开、5xx、被宿主取消）**不补写**：远端可能已经收下，
  再写一遍就是同一条记忆出现两次。这类记录在账本里留成 ``pending``，宁可少一条
  ——原件仍在陪伴插件自己的库里，可以人工补。
* 身份字段只以带盐哈希写入：陪伴插件会把 ``native_scope_id``（私聊里就是 sender ID）
  拼进 ``session_id``，原样落库等于绕过正文过滤器上传身份，因此只写哈希。
* 幂等账本按 ``目标 + memory_id`` 记账，目标是 ``api_base|bank``：换 bank 或换服务地址
  就是换分区，同一条记录会重新写一次到新目的地，旧目标的记账不会被误认。
* 关闭某个会话的 ``/hindsight off`` 或对应 scope 开关时，本插件不再为那个会话召回，
  而陪伴插件已经把记忆让给了本插件——那个会话会暂时没有长期记忆。provider 注册是
  安装级、拿不到会话 scope，接口没有提供按 scope 让位的办法，README 里已写明。
"""

from __future__ import annotations

from collections.abc import Mapping
import asyncio
import hashlib
import json
import logging
from pathlib import Path
import time
from typing import Any
from uuid import uuid4

import httpx

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
DEFAULT_API_BASE = "https://api.hindsight.vectorize.io"
WRITTEN_IDS_FILE = "companion_written_ids.json"
PENDING_FILE = "companion_pending_records.json"
MAX_PENDING_RECORDS = 50
PENDING_TTL_SECONDS = 24 * 3600
ACCEPTED_KINDS = ("persona_life", "proactive_message")
CONFIG_KEY = "companion_provider_enabled"
PERSONAL_KEY = "companion_retain_personal_sensitive"
WARN_INTERVAL_SECONDS = 3600.0

# 补写预算：一次补写最多处理多少条、最多花多少秒。预算只是尽力而为——真被宿主
# 取消也不会丢队列（见 ``_flush_pending``），但先自己收手可以少浪费一次超时。
MAX_FLUSH_PER_PASS = 10
FLUSH_BUDGET_SECONDS = 6.0

# 关闭时等待在途写入（含挂在它们后面的补写）的上限。
SHUTDOWN_WAIT_SECONDS = 10.0

# 账本占位的结果（见 ``WrittenMemoryIdStore.begin``）。
BEGIN_RESERVED = "reserved"
BEGIN_SKIP = "skip"
BEGIN_UNAVAILABLE = "unavailable"

# 失败归类：只有确定没被接收的失败才允许回到队列。
OUTCOME_DEFINITE = "definite"
OUTCOME_UNKNOWN = "unknown"

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


def build_record_metadata(record: Mapping[str, Any], salt: str) -> dict[str, Any]:
    """组装写入 Hindsight 的 metadata。

    ``session_id`` 是唯一不能原样带走的字段：陪伴插件在 V2 里会把它拼成
    ``"private_companion_v2:proactive:<native_scope_id>"``（v7.0.13
    ``companion_v2/proactive/service.py:1532``），而私聊的 ``native_scope_id``
    就是 sender ID（``companion_v2/adapters/astrbot/native.py:870``）。原样写入
    等于绕过正文敏感过滤上传身份，违反本仓库"不发原始 sender ID"的约定，因此只写
    带盐哈希，并换成 ``session_id_hash`` 这个不会误读成原值的键名。
    """
    metadata: dict[str, Any] = {
        "source": "astrbot_plugin_private_companion",
        "source_plugin": str(record.get("source_plugin") or ""),
        "memory_id": str(record.get("memory_id") or ""),
        "kind": str(record.get("kind") or ""),
    }
    for key in ("occurred_at", "confidence", "importance"):
        value = record.get(key)
        if isinstance(value, (str, int, float)) and not isinstance(value, bool):
            metadata[key] = value
    session_id = record.get("session_id")
    if isinstance(session_id, str) and session_id.strip():
        metadata["session_id_hash"] = hash_identifier(salt, "session", session_id.strip())
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


def classify_write_failure(exc: BaseException) -> str:
    """把一次写入异常分成"确定没被接收"和"结果未知"。

    * :data:`OUTCOME_DEFINITE` —— 可以安全重排：4xx（服务端收到并明确拒绝）、
      连接阶段就失败的错（TCP 没建起来，请求没发出去）。
    * :data:`OUTCOME_UNKNOWN` —— 不能重排：超时、连接中途断开、5xx。远端可能
      已经收下这条记录，再写一遍就是同一条记忆出现两次。

    ``httpx`` 的 ``ConnectError`` / ``ConnectTimeout`` 保证请求没有发出去；而
    ``ReadError`` / ``WriteError`` / ``ReadTimeout`` 落在"发出去了但没拿到结果"那侧。
    """
    kind = getattr(exc, "kind", None)
    status = getattr(exc, "status_code", None)
    if kind == "http_status":
        # 4xx：服务端收到并明确拒绝，可以安全重排；5xx：服务端可能已经收下，
        # 但保存失败，属于结果未知。
        if isinstance(status, int) and 400 <= status < 500:
            return OUTCOME_DEFINITE
        return OUTCOME_UNKNOWN
    cause = exc.__cause__ or exc
    if isinstance(cause, (httpx.ConnectError, httpx.ConnectTimeout)):
        return OUTCOME_DEFINITE
    if kind in {"timeout", "network"}:
        return OUTCOME_UNKNOWN
    if isinstance(cause, (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError)):
        return OUTCOME_UNKNOWN
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
        return OUTCOME_UNKNOWN
    # 其余异常（参数、编码、本插件自己的取值错误）都在请求发出前就抛了。
    return OUTCOME_DEFINITE


def ledger_key(target: str, memory_id: str) -> str:
    """账本键：``<api_base>|<bank>|<memory_id>``。

    只按 ``bank + memory_id`` 记账不够：同一个 bank ID 换了服务地址，或者换了 bank
    但服务地址没变，都会让"在别处写过的记录"被当成本目标已经写过而跳过。把服务地址
    和 bank 一起放进键里，换目标就是换分区，旧记录不会被误认。
    """
    return f"{target}|{memory_id}"


class WrittenMemoryIdStore:
    """``目标 + memory_id`` 两阶段账本：先占位，写入成功才落 ``done``。

    这里的"目标"是 ``api_base|bank``。这样处理崩溃窗口：Hindsight 已接收但账本没来得
    及记 ``done`` 时，文件里会留下一条 ``pending``。重放看到 ``pending`` 就跳过——宁可
    少一条（原件仍在陪伴插件自己的库里，可以人工补），也不要同一篇日记出现两次。

    条目带写入时间戳：读入时丢掉超过 ``WRITTEN_ID_TTL_SECONDS`` 的，超过
    ``MAX_WRITTEN_IDS`` 再按最旧淘汰。
    """

    def __init__(self, data_dir: Path, target: str) -> None:
        self.path = data_dir / WRITTEN_IDS_FILE
        self.target = target
        self._entries: dict[str, dict[str, Any]] = self._load()

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
                    entries[self._migrate_key(key)] = {"state": state, "ts": stamp}
        # 更早的版本存的是不带 bank 的纯 ID 列表，无法映射到具体目标：不迁移。
        return entries

    def _migrate_key(self, key: str) -> str:
        """把上一版的 ``bank|memory_id`` 认领到当前目标。

        上一版键里只有 bank、没有服务地址：只有 bank 与当前目标一致时才认领，
        否则留在旧键上——它不会再被匹配到，30 天后随 TTL 过期，也不会被写到别的目标。
        """
        if key.count("|") != 1:
            return key
        bank, _, memory_id = key.partition("|")
        if bank != self.target.split("|", 1)[-1]:
            return key
        return ledger_key(self.target, memory_id)

    def contains(self, memory_id: str) -> bool:
        return ledger_key(self.target, memory_id) in self._entries

    def begin(self, memory_id: str) -> str:
        """占位，返回这次提交能不能继续。

        * :data:`BEGIN_SKIP` —— 这条记录已经写过，或者上次写到一半（``pending``）。
        * :data:`BEGIN_UNAVAILABLE` —— 占位没落盘。占位不落盘就等于没有防重保障，
          提交远端之后一旦崩溃，重放会把同一条写第二遍，所以这次不提交。
        * :data:`BEGIN_RESERVED` —— 占位已落盘，可以提交远端。
        """
        key = ledger_key(self.target, memory_id)
        if key in self._entries:
            return BEGIN_SKIP
        self._entries[key] = {"state": "pending", "ts": time.time()}
        if not self._flush():
            self._entries.pop(key, None)
            return BEGIN_UNAVAILABLE
        return BEGIN_RESERVED

    def commit(self, memory_id: str) -> None:
        self._entries[ledger_key(self.target, memory_id)] = {"state": "done", "ts": time.time()}
        self._trim()
        self._flush()

    def abort(self, memory_id: str) -> None:
        """写入失败：撤掉占位，允许下一次重试。"""
        if self._entries.pop(ledger_key(self.target, memory_id), None) is not None:
            self._flush()

    def _trim(self) -> None:
        if len(self._entries) > MAX_WRITTEN_IDS:
            keep = sorted(self._entries.items(), key=lambda item: item[1].get("ts", 0))[-MAX_WRITTEN_IDS:]
            self._entries = dict(keep)

    def _flush(self) -> bool:
        tmp = self.path.with_name(f"{self.path.name}.tmp")
        try:
            tmp.write_text(json.dumps(self._entries, ensure_ascii=False), encoding="utf-8")
            tmp.replace(self.path)
        except OSError as exc:
            # 已经成功的远端写入不因为账本写不进去而变成失败：这里只回报失败，
            # 由 ``begin`` 决定"占位没落盘就别提交远端"，``commit`` 则只记日志。
            _log_warning(f"written-id store could not be saved: {exc}")
            try:
                if tmp.exists():
                    tmp.unlink()
            except OSError as cleanup_exc:
                _log_warning(f"written-id temp file could not be removed: {cleanup_exc}")
            return False
        return True


class PendingRecordStore:
    """写失败的记录在这里短期排队，下一次成功写入时顺带补写。

    每条记录带自己的 ``id``，补写时**按条确认**：只有这条确实处理完（写成功、
    或结果未知决定不再重试）才删它。旧实现是"先从文件里清空整个队列，再逐条补"，
    宿主超时取消补写或进程中途重启时，后面没轮到的记录就跟着消失了。

    ``ts`` 记的是**第一次**失败的时间；补写再失败只加 ``attempts``、不刷新 ``ts``，
    因此 :data:`PENDING_TTL_SECONDS` 是从首次失败算起，而不是从最后一次重试算起。

    条目还带 ``target``（``api_base|bank``）：换 bank 或换服务地址之后，旧目标排队的
    记录不会被补写到新目标里去，也不会被当成"这个目标的失败记录"。上一版文件里没有
    ``target`` 的条目无法判断原目标，不认领也不补写（原件仍在陪伴插件自己的库里），
    随 24 小时 TTL 自然过期。
    """

    def __init__(self, data_dir: Path, target: str) -> None:
        self.path = data_dir / PENDING_FILE
        self.target = target
        self._warned_untargeted = False

    def add(self, record: Mapping[str, Any]) -> str:
        """入队一条记录，返回它的条目 id。"""
        return self._append(dict(record), ts=time.time())

    def peek(self, limit: int = MAX_FLUSH_PER_PASS) -> list[dict[str, Any]]:
        """按最旧优先列出当前目标、可以重试的条目；只读，不改动文件。"""
        return self._mine(self._load_all())[: max(1, int(limit))]

    def note_failure(self, item_id: str) -> None:
        """补写又失败：保留首次失败时间与排队顺序，只累加重试次数。"""
        items = self._load_all()
        for item in items:
            if item["id"] == item_id and item["target"] == self.target:
                item["attempts"] = int(item.get("attempts") or 1) + 1
                break
        else:
            return
        self._save(items)

    def drop(self, item_id: str) -> None:
        """这条已经确认处理完，从队列里删掉它。"""
        items = self._load_all()
        remaining = [item for item in items
                     if not (item["id"] == item_id and item["target"] == self.target)]
        if len(remaining) == len(items):
            return
        self._save(remaining)

    def _append(self, record: dict[str, Any], *, ts: float) -> str:
        item = {"id": uuid4().hex, "ts": ts, "attempts": 1, "target": self.target, "record": record}
        items = self._load_all()
        items.append(item)
        if len(items) > MAX_PENDING_RECORDS:
            items = items[-MAX_PENDING_RECORDS:]
        self._save(items)
        return item["id"]

    def _mine(self, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [item for item in items if item["target"] == self.target]

    def _load_all(self) -> list[dict[str, Any]]:
        """读出文件里全部未过期条目（含别的目标的）。

        别的目标的条目必须原样留着：换个 bank 再 `drop` 一条，不该把另一个 bank 排队的
        记录一起从文件里抹掉。上一版没有 ``target`` 的条目保留成空目标，随 TTL 自然过期，
        不猜它属于哪个 bank／服务地址，也不会被补写到当前目标。
        """
        now = time.time()
        items: list[dict[str, Any]] = []
        untargeted = 0
        for item in self._load():
            record = item.get("record")
            if not isinstance(record, dict):
                continue
            try:
                ts = float(item.get("ts") or now)
            except (TypeError, ValueError):
                ts = now
            if now - ts >= PENDING_TTL_SECONDS:
                continue
            item_id = item.get("id")
            if not isinstance(item_id, str) or not item_id:
                # 旧格式没有 id：用内容派生成稳定 id，保证同一文件多次读出的 id 一致。
                seed = json.dumps(record, ensure_ascii=False, sort_keys=True).encode("utf-8")
                item_id = "legacy-" + hashlib.sha256(seed).hexdigest()[:16]
            try:
                attempts = int(item.get("attempts") or 1)
            except (TypeError, ValueError):
                attempts = 1
            target = item.get("target")
            target = target if isinstance(target, str) else ""
            if not target:
                untargeted += 1
            items.append({"id": item_id, "ts": ts, "attempts": attempts,
                          "target": target, "record": record})
        if untargeted and not self._warned_untargeted:
            self._warned_untargeted = True
            _log_warning(
                f"ignoring {untargeted} pending companion record(s) without a target"
            )
        return items

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
        # 关闭流程的显式状态：置上之后 is_active() 立刻返回 False（注册表不会再选中
        # 这条注册），新写入也立刻停；成功重新注册才清掉。
        self._stopping = False
        self._target = self._target_of(plugin)
        data_dir = getattr(getattr(plugin, "store", None), "data_dir", None)
        self._written = WrittenMemoryIdStore(Path(data_dir), self._target) if data_dir is not None else None
        self._pending = PendingRecordStore(Path(data_dir), self._target) if data_dir is not None else None
        self._flushing = False

    @staticmethod
    def _target_of(plugin: Any) -> str:
        """写入目标身份：服务地址 + bank，账本与补写队列都按它分区。"""
        api_base = DEFAULT_API_BASE
        config = getattr(plugin, "config", None)
        if config is not None:
            try:
                api_base = str(config.get("api_base") or DEFAULT_API_BASE)
            except Exception:
                api_base = DEFAULT_API_BASE
        api_base = api_base.strip().rstrip("/") or DEFAULT_API_BASE
        try:
            bank = str(plugin._bank_id())
        except Exception:
            bank = ""
        return f"{api_base}|{bank}"

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
        """陪伴插件每次调用前都会问一次；这里把「插件开着、配置完整、没在关」如实反映出去。"""
        if self._stopping:
            # 关闭流程已经开始了：别让注册表把新写入派过来，也别让它把这条注册当可用。
            return False
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
        """幂等地注册。陪伴插件不在、未启用或接口版本不符时什么都不做。

        注册成功后清掉关闭状态；注销失败留下的旧注册也会在这里重试。
        """
        if not self.config_enabled() or not self._plugin_enabled():
            if self.registered:
                await self.disconnect()
            return False
        context = getattr(self.plugin, "_context", None)
        api = find_companion_api(context, on_reject=self._warn)
        if self.registered:
            if api is self._api and not self._stopping:
                return True
            # 陪伴插件重载后是一个新的 extension_api：旧注册不会跟过来，必须重新注册。
            # 另外，上一轮注销失败时 _stopping 会留着，这里顺带再试一次注销。
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
                # 与接口示例一致用默认优先级 0；若同时装了「我会牢牢记住你」，两个 provider
                # 会被按 provider_id 排序取第一个，建议只保留一个。
                priority=0,
                timeout_seconds=self._callback_timeout(),
            )
        except Exception as exc:
            self._warn(f"memory provider registration failed: {exc}")
            return False
        self._api, self._registration = api, registration
        self._stopping = False
        return True

    async def disconnect(self) -> None:
        """停止写入 → 等在途（含补写）结束 → 注销。

        顺序很重要：先置 ``_stopping``，``is_active()`` 立刻变 False，注册表不会再选
        这条注册派新写入；再等手上这次回调整条跑完（包含挂在后面的补写）；最后才注销。
        注销失败时保留引用、下次 ``ensure_registered`` 重试，但这条注册已经因为
        ``_stopping`` 失去被选中的资格，不会在关闭之后还被拿去写。
        """
        api, registration = self._api, self._registration
        if api is None or registration is None:
            return
        self._stopping = True
        await self._wait_idle(SHUTDOWN_WAIT_SECONDS)
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

        返回记录里的 ``memory_id`` 表示已接受；返回 ``None`` 表示这次没收下（未知类型、
        无法归属、含敏感内容、插件未启用、正在关闭、账本不可用或写入失败）。宿主不会
        重试：确定没被接收的失败已经排进本地补写队列，原始记录也仍在陪伴插件自己的库里。

        在途计数覆盖**整次回调**（含挂在后面的补写），关闭时才能等到全部结束再注销。
        """
        self._begin_write()
        try:
            return await self._write(record)
        except asyncio.CancelledError:
            # 宿主超时（默认 10 秒）或关停会取消这次回调。远端可能已经收下这条记录，
            # 所以账本里的 pending 占位保持原样：同 id 的下一次推送会被当成"写过"跳过。
            # 结果未知时宁可少一条，也不要写第二遍。
            _log_warning("companion record write cancelled; outcome unknown, ledger kept pending")
            raise
        finally:
            self._end_write()

    def _queue_for_retry(self, pending_item: str | None, record: Mapping[str, Any]) -> None:
        """把确定没写成的记录放回队列；本来就是从队列里取出来的只累加重试次数。"""
        if self._pending is None:
            return
        if pending_item is not None:
            self._pending.note_failure(pending_item)
        else:
            self._pending.add(record)

    def _drop_pending(self, pending_item: str | None) -> None:
        if pending_item is not None and self._pending is not None:
            self._pending.drop(pending_item)

    async def _write(self, record: Any, *, pending_item: str | None = None) -> str | None:
        """``write`` 的实现；``pending_item`` 是这次重试对应的队列条目 id。"""
        if self._stopping:
            return None
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
            self._drop_pending(pending_item)
            return None

        bank_id = str(self.plugin._bank_id())
        lock = self._lock_for(ledger_key(self._target, memory_id))
        accepted = False
        async with lock:
            state = BEGIN_RESERVED
            if self._written is not None:
                state = self._written.begin(memory_id)
            if state == BEGIN_SKIP:
                # 已经写过，或上次写到一半（崩溃/取消留下的 pending）：跳过，不重复写
                accepted = True
                self._drop_pending(pending_item)
            elif state == BEGIN_UNAVAILABLE:
                # 占位落不进磁盘就等于没有防重保障：交出去之后崩溃就是重复写入。
                self._warn("written-id ledger is not writable; companion record not submitted")
                self._queue_for_retry(pending_item, record)
                return None
            else:
                outcome = "ok"
                salt = str(getattr(self.plugin, "salt", "") or "")
                timestamp = record.get("occurred_at")
                try:
                    client = await self.plugin._client()
                    await client.retain(
                        bank_id=bank_id,
                        content=content.strip(),
                        tags=build_record_tags(salt, record),
                        metadata=build_record_metadata(record, salt),
                        context=build_record_context(record),
                        timestamp=str(timestamp) if isinstance(timestamp, str) and timestamp else None,
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    outcome = classify_write_failure(exc)
                    _log_warning(f"companion record write failed ({outcome}): {exc}")

                if outcome == "ok":
                    if self._written is not None:
                        self._written.commit(memory_id)
                    self._drop_pending(pending_item)
                    accepted = True
                elif outcome == OUTCOME_DEFINITE:
                    # 确定没被接收：撤掉占位、回队列，等下一次成功写入时再试
                    if self._written is not None:
                        self._written.abort(memory_id)
                    self._queue_for_retry(pending_item, record)
                else:
                    # 结果未知：占位留成 pending 当"可能写过了"，也不回队列
                    self._drop_pending(pending_item)
                    self._warn("companion record outcome unknown after failure; not retried")

        # 补写在锁外进行：同一 memory_id 的重试要重新拿这把锁，持锁调用会自己等自己
        if accepted:
            await self._flush_pending()
            return memory_id
        return None

    async def _flush_pending(self) -> None:
        """补写之前失败的记录。

        队列按条确认：只有处理完的条目才从文件里删掉。宿主超时取消、进程中途重启时
        没轮到的条目原样留在队列里，等下一次成功写入继续——不会像"先清空再逐条补"
        那样一次取消就丢掉整批。

        预算是一次补写最多 ``MAX_FLUSH_PER_PASS`` 条、最多 ``FLUSH_BUDGET_SECONDS``
        秒，并且不超过宿主给的这次回调超时（留 3 秒余量给主写入和收尾）。预算用尽时
        剩下的条目留在队列里等下一次；真被取消也只是"这次没补完"，不会再丢记录。
        """
        if self._pending is None or self._flushing:
            return
        self._flushing = True
        try:
            budget = min(FLUSH_BUDGET_SECONDS, max(1.0, self._callback_timeout() - 3.0))
            deadline = time.monotonic() + budget
            for item in self._pending.peek(MAX_FLUSH_PER_PASS):
                if self._stopping:
                    break
                if time.monotonic() >= deadline:
                    _log_warning("pending companion records left for the next write: flush budget exhausted")
                    break
                await self._write(item["record"], pending_item=item["id"])
        finally:
            self._flushing = False
