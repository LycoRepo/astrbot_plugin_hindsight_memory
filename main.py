from __future__ import annotations

from collections.abc import AsyncIterable, Awaitable, Iterable
from datetime import datetime, timezone
import inspect
from typing import Any, Protocol, TypeVar

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import LLMResponse, ProviderRequest
from astrbot.api.star import Context, Star, StarTools
from astrbot.core.agent.message import TextPart

from .commands import PluginStateStore, build_help_text, run_manual_recall_for_tag_sets
from .companion_bridge import CompanionMemoryBridge
from .conversation_context import DEFAULT_CONTEXT_TURNS, recent_turns, short_query_with_context
from .hindsight_client import HindsightClient, HindsightClientError
from .memory_ai import (
    build_recall_query_prompt,
    memory_ai_enabled,
    memory_ai_fallback_to_current_provider,
    memory_ai_provider_id,
    parse_recall_queries,
)
from .memory_formatter import extract_memories, extract_memory_texts, format_recall_results
from .retention_policy import (
    RetainDecision,
    apply_ai_retention_results,
    build_ai_retention_prompt,
    decide_retention,
    dedupe_action,
    is_trivial_message,
    precheck_ai_retention,
    should_write_raw_conversation,
)
from .scope import MemoryScope, MemoryScopes, MissingScopeIdentityError, build_scopes_from_event


PLUGIN_NAME = "astrbot_plugin_hindsight_memory"
RECENT_CONTEXT_EXTRA_KEY = "hindsight_recent_context"
T = TypeVar("T")


class ConfigLike(Protocol):
    def get(self, key: str, default: object = None) -> object:
        ...


class TextResponseLike(Protocol):
    completion_text: str


class HindsightMemoryPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig | None = None) -> None:
        super().__init__(context)
        self._context = context
        self.config = config or {}
        self.store = PluginStateStore(StarTools.get_data_dir())
        self.salt = self.store.get_or_create_salt()
        self.hindsight_client: HindsightClient | None = None
        self.hindsight_client_signature: tuple[str, str, int] | None = None
        self.companion_bridge = CompanionMemoryBridge(self)

    async def initialize(self):
        # 陪伴插件通常已经加载完；注册一次之后由 on_llm_request 兜底重试。
        await self.companion_bridge.ensure_registered()

    async def terminate(self):
        await self.companion_bridge.disconnect()
        if self.hindsight_client is not None:
            await self.hindsight_client.aclose()

    @filter.on_llm_request()
    async def on_llm_request(self, event: AstrMessageEvent, req: ProviderRequest):
        bridge = getattr(self, "companion_bridge", None)
        if bridge is not None:
            await bridge.ensure_registered()
        if not self._base_enabled():
            return
        scopes = self._scopes(event)
        if scopes is None:
            return
        if not self._scope_type_enabled(scopes.primary) or not self.store.is_scope_enabled(scopes.primary.scope_key):
            return
        if not self._config_complete():
            return

        recent = recent_turns(getattr(req, "contexts", None), self._context_turns())
        _set_event_extra(event, RECENT_CONTEXT_EXTRA_KEY, recent)

        query = _event_text(event)
        if not query or is_trivial_message(query):
            return

        try:
            queries = await self._recall_queries(event, query, recent)
            formatted = await run_manual_recall_for_tag_sets(
                await self._client(),
                bank_id=self._bank_id(),
                query=query,
                tag_sets=[recall_scope.tags for recall_scope in scopes.recall_scopes],
                limit=self._recall_limit(),
                item_max_chars=self._recall_item_max_chars(),
                max_extract_depth=self._memory_extract_max_depth(),
                queries=queries,
                empty_message="",
                guidance=True,
            )
        except HindsightClientError as exc:
            _log_warning(f"Hindsight recall failed: {exc}")
            return

        if not formatted:
            return
        if not hasattr(req, "extra_user_content_parts"):
            _log_warning("ProviderRequest does not support extra_user_content_parts; skip Hindsight injection.")
            return

        req.extra_user_content_parts.append(_temporary_text_part(formatted))

    @filter.on_llm_response()
    async def on_llm_response(self, event: AstrMessageEvent, resp: LLMResponse):
        if not self._base_enabled() or not _bool_config(self.config, "retain_enabled", True):
            return
        scopes = self._scopes(event)
        if scopes is None:
            return
        if not self._scope_type_enabled(scopes.primary) or not self.store.is_scope_enabled(scopes.primary.scope_key):
            return
        if not self._config_complete():
            return

        user_text = _event_text(event)
        assistant_text = _response_text(resp)
        if not assistant_text and getattr(resp, "tools_call_name", None):
            _log_debug("Hindsight retain skipped: tool call step")
            return
        event_time = _event_datetime(event)
        recent = _get_event_extra(event, RECENT_CONTEXT_EXTRA_KEY)
        decisions = await self._retention_decisions(
            event,
            user_text,
            assistant_text,
            scopes.primary.scope_type,
            event_time,
            recent if isinstance(recent, list) else [],
        )
        retained = [decision for decision in decisions if decision.should_retain]
        if not retained:
            _log_debug(f"Hindsight retain skipped: {', '.join(decision.reason for decision in decisions)}")
            return

        timestamp = _iso_utc(event_time)
        written: set[tuple[str, str]] = set()
        try:
            client = await self._client()
            for decision in retained:
                content = self._retain_content(user_text, assistant_text, decision)
                if not content:
                    continue
                for retain_scope in scopes.retain_scopes:
                    if retain_scope.scope_type not in decision.target_scope_types:
                        continue
                    if (retain_scope.scope_type, content) in written:
                        continue
                    written.add((retain_scope.scope_type, content))
                    retain_action = await self._retain_dedupe_action(client, content, retain_scope, decision)
                    if retain_action == "duplicate":
                        _log_debug(f"Hindsight retain skipped duplicate: {retain_scope.scope_type}")
                        continue
                    await client.retain(
                        bank_id=self._bank_id(),
                        content=content,
                        tags=retain_scope.tags,
                        metadata=self._retain_metadata(retain_scope.metadata, decision, retain_action),
                        context=_retain_context(retain_scope.scope_type, decision, content),
                        timestamp=timestamp,
                    )
        except HindsightClientError as exc:
            _log_warning(f"Hindsight retain failed: {exc}")

    @filter.command_group("hindsight")
    def hindsight(self, event: AstrMessageEvent):
        pass

    @hindsight.command("status")
    async def hindsight_status(self, event: AstrMessageEvent):
        """检查 Hindsight Memory 配置和连接状态。"""
        scopes = self._scopes(event)
        if scopes is None:
            yield event.plain_result("无法确定当前会话 scope，已跳过 Hindsight 状态检查。")
            return
        scope = scopes.primary
        lines = [
            "Hindsight Memory 状态",
            f"全局启用：{'是' if self._base_enabled() else '否'}",
            f"当前 scope：{scope.scope_type}",
            f"当前会话：{'开启' if self.store.is_scope_enabled(scope.scope_key) else '关闭'}",
            f"配置完整：{'是' if self._config_complete() else '否'}",
        ]
        if self._config_complete():
            status = await (await self._client()).check_status(self._bank_id())
            lines.append(f"连接检查：{status.message}")
        else:
            lines.append("连接检查：跳过，请先填写 api_key 和 bank_id。")
        yield event.plain_result("\n".join(lines))

    @hindsight.command("recall")
    async def hindsight_recall(self, event: AstrMessageEvent, query: str):
        """手动检索当前会话 scope 下的 Hindsight 记忆。"""
        if not self._base_enabled():
            yield event.plain_result("Hindsight Memory 当前未启用。")
            return
        if not self._config_complete():
            yield event.plain_result("配置不完整，请先填写 api_key 和 bank_id。")
            return

        scopes = self._scopes(event)
        if scopes is None:
            yield event.plain_result("无法确定当前会话 scope，已跳过手动检索。")
            return
        scope = scopes.primary
        if not self._scope_type_enabled(scope):
            yield event.plain_result(f"当前 {scope.scope_type} scope 的记忆未启用。")
            return
        if not self.store.is_scope_enabled(scope.scope_key):
            yield event.plain_result("当前会话记忆已关闭。")
            return

        try:
            queries = await self._recall_queries(event, query)
            result = await run_manual_recall_for_tag_sets(
                await self._client(),
                bank_id=self._bank_id(),
                query=query,
                tag_sets=[recall_scope.tags for recall_scope in scopes.recall_scopes],
                limit=self._recall_limit(),
                item_max_chars=self._recall_item_max_chars(),
                max_extract_depth=self._memory_extract_max_depth(),
                queries=queries,
            )
        except HindsightClientError as exc:
            _log_warning(f"Hindsight manual recall failed: {exc}")
            result = f"手动检索失败：{exc}"
        yield event.plain_result(result)

    @hindsight.command("on")
    async def hindsight_on(self, event: AstrMessageEvent):
        """启用当前会话的 Hindsight 记忆。"""
        scopes = self._scopes(event)
        if scopes is None:
            yield event.plain_result("无法确定当前会话 scope，未修改 Hindsight 记忆开关。")
            return
        scope = scopes.primary
        self.store.set_scope_enabled(scope.scope_key, True)
        yield event.plain_result("已启用当前会话 Hindsight 记忆。")

    @hindsight.command("off")
    async def hindsight_off(self, event: AstrMessageEvent):
        """关闭当前会话的 Hindsight 记忆。"""
        scopes = self._scopes(event)
        if scopes is None:
            yield event.plain_result("无法确定当前会话 scope，未修改 Hindsight 记忆开关。")
            return
        scope = scopes.primary
        self.store.set_scope_enabled(scope.scope_key, False)
        yield event.plain_result("已关闭当前会话 Hindsight 记忆。")

    @hindsight.command("help")
    async def hindsight_help(self, event: AstrMessageEvent):
        """显示 Hindsight Memory 命令帮助。"""
        scopes = self._scopes(event)
        if scopes is None:
            yield event.plain_result(build_help_text(None))
            return
        scope = scopes.primary
        yield event.plain_result(build_help_text(self.store.is_scope_enabled(scope.scope_key)))

    async def _client(self) -> HindsightClient:
        signature = self._client_signature()
        if self.hindsight_client is not None and self.hindsight_client_signature == signature:
            return self.hindsight_client

        if self.hindsight_client is not None:
            await self.hindsight_client.aclose()

        api_base, api_key, timeout_seconds = signature
        self.hindsight_client = HindsightClient(
            api_base=api_base,
            api_key=api_key,
            timeout_seconds=timeout_seconds,
        )
        self.hindsight_client_signature = signature
        return self.hindsight_client

    def _client_signature(self) -> tuple[str, str, int]:
        return (
            str(self.config.get("api_base") or "https://api.hindsight.vectorize.io"),
            str(self.config.get("api_key") or ""),
            self._request_timeout_seconds(),
        )

    def _scopes(self, event: AstrMessageEvent) -> MemoryScopes | None:
        try:
            scopes = build_scopes_from_event(event, self.salt)
        except MissingScopeIdentityError as exc:
            _log_warning(f"Hindsight scope unavailable: {exc}")
            return None
        if scopes.primary.scope_type == "private" and _event_looks_like_group(event):
            _log_debug("Hindsight scope fallback: group-like event has no group_id; using private scope.")
        return scopes

    def _base_enabled(self) -> bool:
        return _bool_config(self.config, "enabled", True)

    def _config_complete(self) -> bool:
        return bool(str(self.config.get("api_key") or "").strip() and self._bank_id())

    def _bank_id(self) -> str:
        return str(self.config.get("bank_id") or "").strip()

    def _recall_limit(self) -> int:
        try:
            return max(1, int(self.config.get("recall_limit") or 5))
        except (TypeError, ValueError):
            return 5

    def _recall_item_max_chars(self) -> int:
        try:
            return max(1, int(self.config.get("recall_item_max_chars") or 360))
        except (TypeError, ValueError):
            return 360

    def _memory_extract_max_depth(self) -> int:
        try:
            return max(0, int(self.config.get("memory_extract_max_depth") or 4))
        except (TypeError, ValueError):
            return 4

    def _context_turns(self) -> int:
        try:
            return max(0, int(self.config.get("context_turns", DEFAULT_CONTEXT_TURNS)))
        except (TypeError, ValueError):
            return DEFAULT_CONTEXT_TURNS

    def _request_timeout_seconds(self) -> int:
        try:
            return max(1, int(self.config.get("request_timeout_seconds") or 8))
        except (TypeError, ValueError):
            return 8

    def _scope_type_enabled(self, scope: MemoryScope) -> bool:
        if scope.scope_type in {"group_shared", "group_member"}:
            return _bool_config(self.config, "enable_group_memory", True)
        return _bool_config(self.config, "enable_private_memory", True)

    async def _retention_decisions(
        self,
        event: AstrMessageEvent,
        user_text: str,
        assistant_text: str,
        primary_scope_type: str,
        event_time: datetime | None = None,
        recent_context: list[dict[str, str]] | None = None,
    ) -> list[RetainDecision]:
        if memory_ai_enabled(self.config):
            base_decision = precheck_ai_retention(user_text, assistant_text, primary_scope_type, self.config)
            if not base_decision.should_retain:
                return [base_decision]

            current_time = (event_time or _event_datetime(event)).isoformat(timespec="minutes")
            prompt = build_ai_retention_prompt(
                user_text, assistant_text, primary_scope_type, current_time, recent_context
            )
            try:
                response_text = await _call_memory_ai(self._context, event, prompt, self.config, "retention")
            except (AttributeError, RuntimeError, TypeError, ValueError) as exc:
                _log_warning(f"Hindsight AI retention fallback to rules: {exc}")
            else:
                ai_decisions = apply_ai_retention_results(base_decision, response_text, primary_scope_type, self.config)
                if ai_decisions is not None:
                    return ai_decisions
                _log_warning("Hindsight AI retention returned invalid JSON; fallback to rules.")

        return [decide_retention(user_text, assistant_text, primary_scope_type, self.config)]

    async def _recall_queries(
        self,
        event: AstrMessageEvent,
        query: str,
        recent_context: list[dict[str, str]] | None = None,
    ) -> list[str]:
        recent = list(recent_context or [])
        if not memory_ai_enabled(self.config):
            contextual_query = short_query_with_context(query, recent)
            return [query, contextual_query] if contextual_query else [query]
        prompt = build_recall_query_prompt(query, recent)
        try:
            response_text = await _call_memory_ai(self._context, event, prompt, self.config, "recall")
        except (AttributeError, RuntimeError, TypeError, ValueError) as exc:
            _log_warning(f"Hindsight AI recall query expansion fallback to original query: {exc}")
            return [query]
        queries = parse_recall_queries(response_text, query)
        if not queries:
            _log_warning("Hindsight AI recall query expansion returned no queries; fallback to original query.")
            return [query]
        return queries

    async def _retain_dedupe_action(
        self,
        client: HindsightClient,
        content: str,
        retain_scope: MemoryScope,
        decision: RetainDecision,
    ) -> str:
        if not _bool_config(self.config, "retain_dedupe_enabled", True):
            return "not_checked"
        try:
            raw = await client.recall(bank_id=self._bank_id(), query=content, tags=retain_scope.tags)
            existing_texts = extract_memory_texts(
                raw,
                limit=self._retain_dedupe_limit(),
                max_extract_depth=self._memory_extract_max_depth(),
            )
        except HindsightClientError as exc:
            _log_warning(f"Hindsight retain dedupe failed; continue writing: {exc}")
            return "dedupe_failed"
        return dedupe_action(content, existing_texts, self._retain_dedupe_threshold(), decision.memory_type)

    def _retain_dedupe_limit(self) -> int:
        try:
            return max(1, int(self.config.get("retain_dedupe_limit") or 5))
        except (TypeError, ValueError):
            return 5

    def _retain_dedupe_threshold(self) -> float:
        try:
            return min(1.0, max(0.0, float(self.config.get("retain_dedupe_threshold", 0.85))))
        except (TypeError, ValueError):
            return 0.85

    def _retain_metadata(
        self,
        base_metadata: dict[str, Any],
        decision: RetainDecision,
        retain_action: str,
    ) -> dict[str, Any]:
        metadata = dict(base_metadata)
        metadata.update(
            {
                "retention_reason": decision.reason,
                "retention_sensitivity": decision.sensitivity,
                "retention_type": decision.memory_type,
                "retention_source": decision.source,
                "retention_confidence": str(decision.confidence),
                "retention_action": retain_action,
            }
        )
        return metadata

    def _retain_content(self, user_text: str, assistant_text: str, decision: RetainDecision) -> str:
        if decision.memory_text and not should_write_raw_conversation(decision, self.config):
            return decision.memory_text
        parts: list[str] = []
        if decision.keep_user and _bool_config(self.config, "retain_user_message", True) and user_text:
            parts.append(f"User said: {user_text}")
        if decision.keep_assistant and _bool_config(self.config, "retain_assistant_message", True) and assistant_text:
            parts.append(f"Assistant replied: {assistant_text}")
        return "\n".join(parts)


def _event_text(event: AstrMessageEvent) -> str:
    return str(getattr(event, "message_str", "") or "").strip()


def _response_text(resp: LLMResponse | TextResponseLike | str | bytes | None) -> str:
    if resp is None:
        return ""
    if isinstance(resp, bytes):
        return resp.decode("utf-8", errors="replace").strip()
    if isinstance(resp, str):
        return resp.strip()
    return str(getattr(resp, "completion_text", "") or "").strip()


def _set_event_extra(event: AstrMessageEvent, key: str, value: object) -> None:
    set_extra = getattr(event, "set_extra", None)
    if callable(set_extra):
        set_extra(key, value)


def _get_event_extra(event: AstrMessageEvent, key: str) -> object:
    get_extra = getattr(event, "get_extra", None)
    if not callable(get_extra):
        return None
    try:
        return get_extra(key, None)
    except TypeError:
        return get_extra(key)


def _event_datetime(event: AstrMessageEvent) -> datetime:
    """Local-timezone time of the message, falling back to now."""
    raw = getattr(getattr(event, "message_obj", None), "timestamp", None)
    if isinstance(raw, (int, float)) and not isinstance(raw, bool) and raw > 0:
        try:
            return datetime.fromtimestamp(raw, timezone.utc).astimezone()
        except (OverflowError, OSError, ValueError):
            pass
    return datetime.now(timezone.utc).astimezone()


def _iso_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _retain_context(scope_type: str, decision: RetainDecision, content: str) -> str:
    """Short label that Hindsight injects into its fact extraction prompt."""
    if scope_type == "group_shared":
        subject = "shared fact about a group chat"
    elif scope_type == "group_member":
        subject = "fact about one member of a group chat"
    else:
        subject = "fact about the user in a private chat"
    if content == decision.memory_text and decision.memory_type != "conversation":
        return f"Chatbot long-term memory: {subject} ({decision.memory_type})"
    return f"Chatbot conversation turn: {subject}"


def _event_looks_like_group(event: AstrMessageEvent) -> bool:
    method = getattr(event, "get_message_type", None)
    if callable(method):
        try:
            return str(method()).lower() == "group"
        except TypeError:
            return False
    message_obj = getattr(event, "message_obj", None)
    message_type = getattr(message_obj, "type", None) or getattr(message_obj, "message_type", None)
    return str(message_type).lower() == "group"


def _temporary_text_part(text: str) -> TextPart:
    part = _new_text_part(text)
    mark_as_temp = getattr(part, "mark_as_temp", None)
    if not callable(mark_as_temp):
        raise TypeError("TextPart does not support mark_as_temp()")
    marked = mark_as_temp()
    return part if marked is None else marked


def _new_text_part(text: str) -> TextPart:
    signature = inspect.signature(TextPart)
    parameters = signature.parameters
    accepts_text_keyword = "text" in parameters or any(
        parameter.kind == inspect.Parameter.VAR_KEYWORD for parameter in parameters.values()
    )
    if accepts_text_keyword:
        return TextPart(text=text)
    accepts_positional_text = any(
        parameter.kind
        in {
            inspect.Parameter.POSITIONAL_ONLY,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            inspect.Parameter.VAR_POSITIONAL,
        }
        for parameter in parameters.values()
    )
    if accepts_positional_text:
        return TextPart(text)
    raise TypeError("TextPart constructor does not accept a text value")


async def _call_memory_ai(
    context: Context,
    event: AstrMessageEvent,
    prompt: str,
    config: ConfigLike,
    task: str,
) -> str:
    provider_id = await _resolve_memory_ai_provider_id(context, event, config)
    if not provider_id:
        raise RuntimeError("no memory AI provider selected")

    selected_provider_id = memory_ai_provider_id(config)
    system_prompt = f"You support long-term memory {task}. Return JSON only."
    try:
        return await _llm_generate_text(context, provider_id, prompt, system_prompt)
    except (AttributeError, RuntimeError, TypeError, ValueError) as exc:
        if not selected_provider_id or not memory_ai_fallback_to_current_provider(config):
            raise
        fallback_provider_id = await _current_chat_provider_id(context, event)
        if not fallback_provider_id or fallback_provider_id == selected_provider_id:
            raise
        _log_warning(f"Hindsight memory AI selected provider failed; fallback to current provider: {exc}")
        return await _llm_generate_text(context, fallback_provider_id, prompt, system_prompt)


async def _llm_generate_text(context: Context, provider_id: str, prompt: str, system_prompt: str) -> str:
    llm_generate = getattr(context, "llm_generate", None)
    if not callable(llm_generate):
        raise RuntimeError("AstrBot context does not support llm_generate")
    attempts = (
        {"chat_provider_id": provider_id, "prompt": prompt, "system_prompt": system_prompt},
        {"chat_provider_id": provider_id, "prompt": prompt},
    )
    last_error: Exception | None = None
    for kwargs in attempts:
        try:
            response = await _maybe_await(llm_generate(**kwargs))
            response_text = await _generated_text(response)
            if response_text.strip():
                return response_text
        except TypeError as exc:
            last_error = exc
            continue
    raise RuntimeError(f"llm_generate call failed: {last_error}")


async def _resolve_memory_ai_provider_id(context: Context, event: AstrMessageEvent, config: ConfigLike) -> str:
    selected_provider_id = memory_ai_provider_id(config)
    if selected_provider_id:
        return selected_provider_id
    return await _current_chat_provider_id(context, event)


async def _current_chat_provider_id(context: Context, event: AstrMessageEvent) -> str:
    umo = str(getattr(event, "unified_msg_origin", "") or "")
    method = getattr(context, "get_current_chat_provider_id", None)
    if not callable(method):
        return ""
    try:
        provider_id = method(umo=umo)
    except TypeError:
        provider_id = method(umo)
    return str(await _maybe_await(provider_id) or "").strip()


async def _maybe_await(value: Awaitable[T] | T) -> T:
    if inspect.isawaitable(value):
        return await value
    return value


async def _generated_text(response: object) -> str:
    response_text = _response_text(response)
    if response_text:
        return response_text
    if isinstance(response, AsyncIterable):
        chunks: list[str] = []
        async for chunk in response:
            chunk_text = await _generated_text(chunk)
            if chunk_text:
                chunks.append(chunk_text)
        return "".join(chunks).strip()
    if isinstance(response, Iterable) and not isinstance(response, (str, bytes, bytearray, dict)):
        chunks = []
        for chunk in response:
            chunk_text = _response_text(chunk)
            if chunk_text:
                chunks.append(chunk_text)
        return "".join(chunks).strip()
    return str(response or "").strip()


def _bool_config(config: ConfigLike, key: str, default: bool) -> bool:
    value = config.get(key, default)
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() not in {"0", "false", "no", "off", ""}
    return bool(value)


def _log_warning(message: str) -> None:
    if logger is not None:
        logger.warning(message)


def _log_debug(message: str) -> None:
    if logger is not None:
        logger.debug(message)
