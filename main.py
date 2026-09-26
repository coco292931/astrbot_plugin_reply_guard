"""群聊回复守卫：关键词精准回复、回复概率门、敏感词拦截、引用图。"""

from __future__ import annotations

import os
import random
import re
import time
from typing import Any, Iterable

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Image as AstrImage
from astrbot.api.message_components import Plain, Reply
from astrbot.api.provider import LLMResponse
from astrbot.api.star import Context, Star, register
from astrbot.core.utils.astrbot_path import get_astrbot_data_path

from .core.keyword_store import KeywordStore
from .core.quote_render import (
    MessageSegment,
    QuoteMessage,
    ReplyMessage,
    render_quote_card,
)

PLUGIN_NAME = "astrbot_plugin_reply_guard"
MAX_QUOTE_MESSAGES = 10


def _as_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on", "y"}
    return default


def _as_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_int(value: Any, default: int) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _as_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    if isinstance(value, str):
        items: list[str] = []
        for chunk in value.replace("，", ",").replace("\n", ",").split(","):
            chunk = chunk.strip()
            if chunk:
                items.append(chunk)
        return items
    return []


_QUOTE_COMMAND_RE = re.compile(r"^[/／]?q(?:\s+(\d{1,2}))?$", re.IGNORECASE)


def _parse_quote_command(text: str) -> int | None:
    """认 /q、/q 3 这种写法，唤醒前缀有没有被剥掉都认。"""
    match = _QUOTE_COMMAND_RE.match((text or "").strip())
    if not match:
        return None
    return max(1, min(_as_int(match.group(1) or 1, 1), MAX_QUOTE_MESSAGES))


def _flatten(config: dict) -> dict[str, Any]:
    """面板的分组配置拍平成 key_subkey。"""
    flat: dict[str, Any] = {}
    for key, value in (config or {}).items():
        if isinstance(value, dict):
            for sub_key, sub_value in value.items():
                flat[f"{key}_{sub_key}"] = sub_value
        else:
            flat[key] = value
    return flat


@register(
    PLUGIN_NAME,
    "coco",
    "群聊回复守卫：关键词精准回复 / 回复概率门 / 敏感词拦截 / 引用图",
    "0.1.4",
    "https://github.com/coco292931/astrbot_plugin_reply_guard",
)
class ReplyGuardPlugin(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        cfg = _flatten(config if isinstance(config, dict) else {})
        self.config = cfg

        try:
            self.data_root = get_astrbot_data_path()
        except Exception:
            self.data_root = "/AstrBot/data"
        self.data_dir = os.path.join(self.data_root, "plugin_data", PLUGIN_NAME)
        self.avatar_dir = os.path.join(self.data_dir, "avatars")
        self.cache_dir = os.path.join(self.data_dir, "cache")
        for path in (self.data_dir, self.avatar_dir, self.cache_dir):
            try:
                os.makedirs(path, exist_ok=True)
            except OSError as e:
                logger.warning(f"[reply_guard] 创建目录失败 {path}: {e}")

        # ---- 关键词回复 ----
        self.enable_keyword_reply = _as_bool(cfg.get("enable_keyword_reply"), True)
        xlsx_path = str(cfg.get("keyword_xlsx_path") or "").strip()
        if not xlsx_path:
            xlsx_path = os.path.join(self.data_dir, "keywords.xlsx")
        elif not os.path.isabs(xlsx_path):
            xlsx_path = os.path.join(self.data_root, xlsx_path)
        self.keyword_scope = _as_list(cfg.get("keyword_scope"))
        self.store = KeywordStore(
            xlsx_path,
            sheet=str(cfg.get("keyword_sheet") or ""),
            reload_interval=_as_int(cfg.get("keyword_reload_interval"), 60),
            skip_header=_as_bool(cfg.get("keyword_has_header"), False),
        )

        # ---- 回复概率 ----
        self.enable_probability = _as_bool(cfg.get("enable_probability"), True)
        self.probability = min(
            1.0, max(0.0, _as_float(cfg.get("probability_value"), 1.0))
        )
        self.probability_mode = str(cfg.get("probability_mode") or "group_only")
        self.probability_scope = _as_list(cfg.get("probability_scope_list"))

        # ---- 敏感词拦截 ----
        self.enable_sensitive_filter = _as_bool(
            cfg.get("enable_sensitive_filter"), True
        )
        self.sensitive_words = _as_list(cfg.get("sensitive_words"))
        self.sensitive_scope = _as_list(cfg.get("sensitive_scope"))

        # ---- 引用图 ----
        self.enable_quote = _as_bool(cfg.get("enable_quote"), True)
        self.quote_font_path = str(cfg.get("quote_font_path") or "").strip()
        self.quote_proxy = str(cfg.get("quote_proxy") or "").strip()
        self.quote_scope = _as_list(cfg.get("quote_scope"))

    async def initialize(self) -> None:
        if self.enable_keyword_reply:
            self.store.load(force=True)
            if self.store.error:
                logger.warning(f"[reply_guard] 关键词表未就绪: {self.store.error}")

    # ------------------------------------------------------------------ 通用

    @staticmethod
    def _in_scope(event: AstrMessageEvent, scope: Iterable[str]) -> bool:
        scope_list = list(scope or [])
        if not scope_list:
            return True
        if "*" in scope_list:
            return True
        group_id = str(event.get_group_id() or "")
        sender_id = str(event.get_sender_id() or "")
        umo = str(getattr(event, "unified_msg_origin", "") or "")
        return any(item in (group_id, sender_id, umo) for item in scope_list)

    def _probability_applies(self, event: AstrMessageEvent) -> bool:
        is_group = bool(event.get_group_id())
        mode = self.probability_mode
        if mode == "group_only" and not is_group:
            return False
        if mode == "private_only" and is_group:
            return False
        return self._in_scope(event, self.probability_scope)

    def _find_sensitive(self, content: str) -> str:
        if not content or not self.sensitive_words:
            return ""
        lowered = content.lower()
        for word in self.sensitive_words:
            if word in content or word.lower() in lowered:
                return word
        return ""

    # ------------------------------------------------------------- 消息入口

    @filter.event_message_type(
        filter.EventMessageType.GROUP_MESSAGE
        | filter.EventMessageType.PRIVATE_MESSAGE,
        priority=120,
    )
    async def on_message(self, event: AstrMessageEvent):
        try:
            if str(event.get_sender_id()) == str(event.get_self_id()):
                return

            text = (event.message_str or "").strip()

            # 0) 引用图：/q [数量]
            if self.enable_quote and self._in_scope(event, self.quote_scope):
                quote_count = _parse_quote_command(text)
                if quote_count is not None:
                    event.should_call_llm(False)
                    async for result in self._quote_results(event, quote_count):
                        yield result
                    return

            # 1) 关键词精准匹配：命中就按表格回，且不进历史
            if self.enable_keyword_reply and self._in_scope(
                event, self.keyword_scope
            ):
                self.store.reload_if_needed()
                reply = self.store.match(text)
                if reply:
                    logger.info(
                        f"[reply_guard] 关键词命中「{text}」 umo={event.unified_msg_origin}"
                    )
                    event.should_call_llm(False)
                    yield event.plain_result(reply)
                    return

            # 2) 概率门：没过就直接掐掉，消息也不落盘
            if not self.enable_probability:
                return
            if not self._probability_applies(event):
                return
            roll = random.random()
            if roll >= self.probability:
                event.should_call_llm(False)
                logger.debug(
                    f"[reply_guard] 概率未通过 roll={roll:.4f} p={self.probability:.4f}"
                )
                return
        except Exception as e:
            logger.error(f"[reply_guard] 消息处理异常: {e}", exc_info=True)

    # --------------------------------------------------------- 敏感词拦截

    @filter.on_llm_response()
    async def on_llm_response(
        self, event: AstrMessageEvent, response: LLMResponse
    ) -> None:
        if not self.enable_sensitive_filter or not self.sensitive_words:
            return
        if not self._in_scope(event, self.sensitive_scope):
            return

        hit = self._find_sensitive(self._collect_response_text(response))
        if not hit:
            return

        logger.info(
            f"[reply_guard] LLM 回复命中敏感词「{hit}」，已拦截 umo={event.unified_msg_origin}"
        )
        event.clear_result()
        event.stop_event()

    @staticmethod
    def _collect_response_text(response: LLMResponse | None) -> str:
        if response is None:
            return ""
        parts: list[str] = []
        text = getattr(response, "completion_text", "") or ""
        if text:
            parts.append(str(text))
        chain = getattr(response, "result_chain", None)
        for comp in getattr(chain, "chain", []) or []:
            if isinstance(comp, Plain):
                parts.append(str(getattr(comp, "text", "") or ""))
        return "\n".join(parts)

    # ----------------------------------------------------------- 引用图 /q

    async def _quote_results(self, event: AstrMessageEvent, count: int):
        reply_comp = None
        for comp in event.get_messages():
            if isinstance(comp, Reply):
                reply_comp = comp
                break

        if reply_comp is None:
            yield event.plain_result("引用一条消息再发 /q 汪")
            return

        want = max(1, min(_as_int(count, 1), MAX_QUOTE_MESSAGES))
        messages: list[QuoteMessage] = []
        try:
            messages = await self._build_quote_messages(event, reply_comp, want)
        except Exception as e:
            logger.error(f"[reply_guard] 读取被引用消息失败: {e}", exc_info=True)

        if not messages:
            yield event.plain_result("读不到那条消息汪")
            return

        filename = f"quote_{int(time.time() * 1000)}"
        try:
            out_path = await render_quote_card(
                messages,
                out_dir=self.cache_dir,
                filename=filename,
                font_path=self.quote_font_path,
                proxy=self.quote_proxy,
            )
        except Exception as e:
            logger.error(f"[reply_guard] 渲染引用图失败: {e}", exc_info=True)
            yield event.plain_result("图没画出来汪")
            return

        logger.info(f"[reply_guard] 引用图已生成: {out_path}（{len(messages)} 条消息）")
        yield event.chain_result([AstrImage.fromFileSystem(out_path)])

    # ------------------------------------------------------- 消息收集与解析

    async def _build_quote_messages(
        self, event: AstrMessageEvent, reply_comp, count: int
    ) -> list[QuoteMessage]:
        first = await self._build_quote_message(event, reply_comp)
        if first is None:
            return []
        if count <= 1:
            return [first]

        following = await self._fetch_following_messages(event, reply_comp, count)
        return ([first] + following)[:count]

    async def _build_quote_message(
        self, event: AstrMessageEvent, reply_comp
    ) -> QuoteMessage | None:
        nickname = str(getattr(reply_comp, "sender_nickname", "") or "")
        user_id = str(getattr(reply_comp, "sender_id", "") or "")
        segments: list[MessageSegment] = []
        reply: ReplyMessage | None = None

        chain = list(getattr(reply_comp, "chain", None) or [])
        if chain:
            segments, reply = self._parse_astr_chain(chain)

        if not segments and not reply:
            data = await self._fetch_onebot_message(
                event, getattr(reply_comp, "id", None)
            )
            if data:
                sender = (
                    data.get("sender") if isinstance(data.get("sender"), dict) else {}
                )
                nickname = str(
                    sender.get("card") or sender.get("nickname") or nickname
                )
                user_id = str(sender.get("user_id") or user_id)
                segments, reply = self._parse_onebot_message(data)

        if not segments and not nickname:
            return None
        return QuoteMessage(
            user_id=user_id,
            nickname=nickname,
            segments=segments,
            reply=reply,
        )

    async def _fetch_following_messages(
        self, event: AstrMessageEvent, reply_comp, count: int
    ) -> list[QuoteMessage]:
        """取被引用消息之后的消息，拿不到就返回空。"""
        group_id = str(event.get_group_id() or "")
        if not group_id:
            return []
        client = self._get_client(event)
        if client is None:
            return []
        if not str(getattr(reply_comp, "id", "") or "").strip():
            return []

        try:
            result = await client.call_action(
                "get_group_msg_history",
                group_id=int(group_id),
                count=int(count) + 5,
            )
        except Exception as e:
            logger.debug(f"[reply_guard] get_group_msg_history 失败: {e}")
            return []

        raw = []
        if isinstance(result, dict):
            data = result.get("data") if isinstance(result.get("data"), dict) else result
            if isinstance(data, dict):
                for key in ("messages", "message", "list"):
                    value = data.get(key)
                    if isinstance(value, list):
                        raw = value
                        break
        if not isinstance(raw, list):
            return []

        records = [item for item in raw if isinstance(item, dict)]
        records.sort(
            key=lambda item: (
                _as_int(item.get("time"), 0),
                _as_int(item.get("message_seq"), 0),
            )
        )

        target_id = str(getattr(reply_comp, "id", "") or "")
        start = -1
        for index, item in enumerate(records):
            if str(item.get("message_id") or "") == target_id:
                start = index
                break
        if start < 0:
            return []

        following: list[QuoteMessage] = []
        for item in records[start + 1 : start + count]:
            if not isinstance(item, dict):
                continue
            message = self._onebot_to_quote(item)
            if message is not None:
                following.append(message)
        return following

    @staticmethod
    def _onebot_to_quote(data: dict) -> QuoteMessage | None:
        sender = data.get("sender") if isinstance(data.get("sender"), dict) else {}
        nickname = str(sender.get("card") or sender.get("nickname") or "")
        user_id = str(sender.get("user_id") or "")
        segments, _reply = ReplyGuardPlugin._parse_onebot_message(data)
        if not segments and not nickname:
            return None
        return QuoteMessage(
            user_id=user_id, nickname=nickname, segments=segments, reply=None
        )

    @staticmethod
    def _parse_astr_chain(chain) -> tuple[list[MessageSegment], ReplyMessage | None]:
        segments: list[MessageSegment] = []
        reply: ReplyMessage | None = None
        for comp in chain:
            if isinstance(comp, Plain):
                text = str(getattr(comp, "text", "") or "")
                if text:
                    segments.append(MessageSegment(type="text", text=text))
            elif isinstance(comp, AstrImage):
                url = str(getattr(comp, "url", "") or "")
                file = str(getattr(comp, "file", "") or "")
                target = url or (file if file.startswith("http") else "")
                if target:
                    segments.append(
                        MessageSegment(type="image", kind="image", url=target)
                    )
            elif isinstance(comp, Reply):
                nested_chain = list(getattr(comp, "chain", None) or [])
                if not nested_chain:
                    continue
                nested_segments, nested_reply = ReplyGuardPlugin._parse_astr_chain(
                    nested_chain
                )
                reply = ReplyMessage(
                    nickname=str(getattr(comp, "sender_nickname", "") or ""),
                    segments=nested_segments,
                    reply=nested_reply,
                )
        return segments, reply

    @staticmethod
    def _parse_onebot_message(
        data: dict,
    ) -> tuple[list[MessageSegment], ReplyMessage | None]:
        segments: list[MessageSegment] = []
        raw = data.get("message")
        if isinstance(raw, str):
            text = raw.strip()
            if text:
                segments.append(MessageSegment(type="text", text=text))
            return segments, None

        for seg in raw or []:
            if not isinstance(seg, dict):
                continue
            seg_type = str(seg.get("type") or "")
            seg_data = seg.get("data") or {}
            if seg_type == "text":
                text = str(seg_data.get("text") or "")
                if text:
                    segments.append(MessageSegment(type="text", text=text))
            elif seg_type == "image":
                url = str(seg_data.get("url") or seg_data.get("file") or "")
                if url.startswith("http") or url.startswith("data:image/"):
                    sub_type = str(seg_data.get("sub_type") or "")
                    kind = "sticker" if sub_type == "1" else "image"
                    segments.append(MessageSegment(type="image", kind=kind, url=url))
            elif seg_type == "face":
                face_id = str(seg_data.get("id") or "")
                if face_id.isdigit():
                    segments.append(MessageSegment(type="face", kind="emoji", id=face_id))
                else:
                    segments.append(MessageSegment(type="text", text="[表情]"))
            elif seg_type == "at":
                qq = str(seg_data.get("qq") or "")
                segments.append(
                    MessageSegment(
                        type="text", text="@全体成员" if qq == "all" else f"@{qq}"
                    )
                )
        return segments, None

    # ------------------------------------------------------------ OneBot

    @staticmethod
    def _get_client(event: AstrMessageEvent):
        bot = getattr(event, "bot", None)
        if bot is None:
            return None
        client = getattr(bot, "api", None) or bot
        if hasattr(client, "call_action"):
            return client
        return None

    async def _fetch_onebot_message(
        self, event: AstrMessageEvent, message_id
    ) -> dict | None:
        if message_id in (None, ""):
            return None
        client = self._get_client(event)
        if client is None:
            return None

        try:
            mid: Any = int(str(message_id))
        except (TypeError, ValueError):
            mid = message_id

        try:
            result = await client.call_action("get_msg", message_id=mid)
        except Exception as e:
            logger.debug(f"[reply_guard] get_msg 调用失败: {e}")
            return None

        if not isinstance(result, dict):
            return None
        data = result.get("data") if isinstance(result.get("data"), dict) else result
        return data if isinstance(data, dict) else None
