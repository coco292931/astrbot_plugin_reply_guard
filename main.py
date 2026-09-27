"""群聊回复守卫：关键词精准回复、回复概率门、敏感词拦截、引用图。"""

from __future__ import annotations

import json
import os
import random
import re
import time
from typing import Any, Iterable

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Image as AstrImage
from astrbot.api.message_components import At, Plain, Reply
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
from .core.quote_render import _parse_cq_string as parse_cq_string

PLUGIN_NAME = "astrbot_plugin_reply_guard"
MAX_QUOTE_MESSAGES = 10
MAX_REPLY_DEPTH = 3


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


_QUOTE_COMMAND_RE = re.compile(r"^[/／]?q\s*(\d{1,2})?$", re.IGNORECASE)
_QUOTE_COMMAND_TAIL_RE = re.compile(r"(?:^|\s)[/／]?q\s*(\d{1,2})?\s*$", re.IGNORECASE)


def _parse_quote_command(text: str) -> int | None:
    """认 /q、/q 3、q3 这些写法，前面挂着 @ 或引用噪声也能认出来。"""
    candidate = (text or "").strip()
    if not candidate:
        return None
    match = _QUOTE_COMMAND_RE.match(candidate) or _QUOTE_COMMAND_TAIL_RE.search(candidate)
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
    "0.2.6",
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
        self._at_name_cache: dict[str, str] = {}
        self._member_cache: dict[str, dict] = {}
        self._last_window: list = []

    async def initialize(self) -> None:
        if self.enable_keyword_reply:
            self.store.load(force=True)
            if self.store.error:
                logger.warning(f"[reply_guard] 关键词表未就绪: {self.store.error}")

    # ------------------------------------------------------------------ 通用

    @staticmethod
    def _typed_text(event: AstrMessageEvent) -> str:
        """只取用户手打的纯文本，撇开 @、引用、图片这些组件。"""
        parts: list[str] = []
        for comp in event.get_messages():
            if isinstance(comp, Plain):
                parts.append(str(getattr(comp, "text", "") or ""))
        return "".join(parts).strip()

    def _command_candidate(self, event: AstrMessageEvent) -> str:
        """命令判定用的文本：手打的优先，拿不到就把 @ 和方括号噪声剔掉。"""
        typed = self._typed_text(event)
        if typed:
            return typed
        text = (event.message_str or "").strip()
        text = re.sub(r"@[^\s@]*\(\s*\d+\s*\)", " ", text)
        text = re.sub(r"\[[^\]]*\]", " ", text)
        return text.strip()

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

            # 0) 引用图：/q [数量]，只看手打的文本，免得被 @ 和引用带偏
            if self.enable_quote and self._in_scope(event, self.quote_scope):
                candidate = self._command_candidate(event)
                quote_count = _parse_quote_command(candidate)
                if "q" in candidate.lower() or "q" in text.lower():
                    logger.info(
                        "[reply_guard] /q 判定: "
                        f"comps={[type(c).__name__ for c in event.get_messages()]} "
                        f"typed={candidate!r} str={text!r} -> {quote_count}"
                    )
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

        await self._resolve_at_names(event, messages)
        self._dump_quote_debug(messages)

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

    def _dump_quote_debug(self, messages: list[QuoteMessage]) -> None:
        """把这次拼进图里的内容落一份，出问题了直接看。"""
        try:
            payload = [
                {
                    "nickname": message.nickname,
                    "user_id": message.user_id,
                    "segments": [
                        f"{segment.type}:{segment.kind}:{segment.text or segment.url or segment.id}"
                        for segment in message.segments
                    ],
                    "reply": message.reply.nickname if message.reply else None,
                    "message_id": message.message_id,
                    "time": message.time,
                }
                for message in messages
            ]
            window = []
            for item in getattr(self, "_last_window", []) or []:
                sender = item.get("sender") if isinstance(item.get("sender"), dict) else {}
                window.append(
                    {
                        "message_id": item.get("message_id"),
                        "message_seq": item.get("message_seq"),
                        "time": item.get("time"),
                        "user_id": sender.get("user_id"),
                        "raw": str(item.get("raw_message") or "")[:60],
                    }
                )
            path = os.path.join(self.cache_dir, "last_quote.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(
                    {"rendered": payload, "window": window},
                    handle,
                    ensure_ascii=False,
                    indent=1,
                )
        except Exception as e:
            logger.debug(f"[reply_guard] 写调试信息失败: {e}")

    async def _group_member_names(self, client, group_id: str) -> dict[str, str]:
        """拉一次群成员表，10 分钟内复用。"""
        now = time.time()
        cached = self._member_cache.get(group_id)
        if cached and now - float(cached.get("at") or 0) < 600:
            return dict(cached.get("names") or {})

        try:
            result = await client.call_action(
                "get_group_member_list", group_id=int(group_id)
            )
        except Exception as e:
            logger.debug(f"[reply_guard] 取群成员列表失败: {e}")
            return {}

        raw: list = []
        if isinstance(result, dict):
            data = result.get("data")
            if isinstance(data, list):
                raw = data
            elif isinstance(data, dict) and isinstance(data.get("list"), list):
                raw = data["list"]
        elif isinstance(result, list):
            raw = result

        names: dict[str, str] = {}
        for item in raw:
            if not isinstance(item, dict):
                continue
            qq = str(item.get("user_id") or "")
            if qq:
                names[qq] = str(item.get("card") or item.get("nickname") or "")
        self._member_cache[group_id] = {"at": now, "names": names}
        logger.info(f"[reply_guard] 群成员表已缓存 {len(names)} 人")
        return names

    async def _resolve_at_names(
        self, event: AstrMessageEvent, messages: list[QuoteMessage]
    ) -> None:
        """把 @ 的号换成群名片/昵称，顺手把 at 段变成文本段。"""
        group_id = str(event.get_group_id() or "")
        wanted: dict[str, str] = {}

        def collect(segments: list[MessageSegment]) -> None:
            for segment in segments:
                if segment.type == "at" and segment.id:
                    wanted.setdefault(segment.id, segment.text or "")

        def walk_collect(reply: ReplyMessage | None) -> None:
            if reply is None:
                return
            collect(reply.segments)
            walk_collect(reply.reply)

        for message in messages:
            collect(message.segments)
            walk_collect(message.reply)

        missing = [
            qq
            for qq, name in wanted.items()
            if not name and qq != "all" and qq.isdigit()
        ]
        if missing and group_id:
            client = self._get_client(event)
            if client is not None:
                for qq in missing:
                    cached = self._at_name_cache.get(f"{group_id}:{qq}")
                    if cached:
                        wanted[qq] = cached

                # 先用一次群成员列表把名字对上，比逐个查省事
                if [qq for qq in missing if not wanted.get(qq)]:
                    for qq, name in (
                        await self._group_member_names(client, group_id)
                    ).items():
                        if qq in missing and name and not wanted.get(qq):
                            wanted[qq] = name
                            self._at_name_cache[f"{group_id}:{qq}"] = name

                for qq in [q for q in missing if not wanted.get(q)]:
                    try:
                        info = await client.call_action(
                            "get_group_member_info",
                            group_id=int(group_id),
                            user_id=int(qq),
                        )
                    except Exception as e:
                        logger.debug(f"[reply_guard] 取群名片失败 {qq}: {e}")
                        continue
                    data = info.get("data") if isinstance(info, dict) else None
                    if isinstance(data, dict):
                        name = str(data.get("card") or data.get("nickname") or "")
                        if name:
                            wanted[qq] = name
                            self._at_name_cache[f"{group_id}:{qq}"] = name

            logger.info(
                f"[reply_guard] @ 名称解析 {len([q for q in wanted if wanted[q]])}/{len(wanted)}"
            )

        def apply(segments: list[MessageSegment]) -> None:
            for segment in segments:
                if segment.type != "at":
                    continue
                if segment.id == "all":
                    segment.type = "text"
                    segment.text = "@全体成员"
                    continue
                name = wanted.get(segment.id) or segment.text or segment.id
                segment.type = "text"
                segment.text = f"@{name}"

        def walk_apply(reply: ReplyMessage | None) -> None:
            if reply is None:
                return
            apply(reply.segments)
            walk_apply(reply.reply)

        for message in messages:
            apply(message.segments)
            walk_apply(message.reply)

    async def _build_quote_messages(
        self, event: AstrMessageEvent, reply_comp, count: int
    ) -> list[QuoteMessage]:
        first = await self._build_quote_message(event, reply_comp)
        if first is None:
            return []
        if count <= 1:
            return [first]

        before = await self._fetch_previous_messages(event, reply_comp, count, first)
        return (before + [first])[-count:]

    async def _fetch_onebot_reply(
        self, event: AstrMessageEvent, message_id: str | None, depth: int
    ) -> ReplyMessage | None:
        """递归取被引用的消息，最多三层，取不到就写占位。"""
        if not message_id or depth > MAX_REPLY_DEPTH:
            return None
        data = await self._fetch_onebot_message(event, message_id)
        if not data:
            return None
        sender = data.get("sender") if isinstance(data.get("sender"), dict) else {}
        nickname = str(sender.get("card") or sender.get("nickname") or "")
        segments, reply_id = self._parse_onebot_message(data)
        if not segments:
            segments = [MessageSegment(type="text", text="[引用消息不可用]")]
        return ReplyMessage(
            nickname=nickname or "匿名",
            segments=segments,
            reply=await self._fetch_onebot_reply(event, reply_id, depth + 1),
        )

    async def _enrich_reply(
        self, event: AstrMessageEvent, reply: ReplyMessage | None, depth: int
    ) -> ReplyMessage | None:
        """引用链里只有 id 没内容的节点，用 get_msg 补上。"""
        if reply is None or depth > MAX_REPLY_DEPTH:
            return reply
        empty = not reply.segments or (
            len(reply.segments) == 1
            and reply.segments[0].type == "text"
            and reply.segments[0].text in ("[引用消息不可用]", "")
        )
        if empty and reply.message_id:
            fetched = await self._fetch_onebot_reply(event, reply.message_id, depth)
            if fetched is not None:
                if not fetched.nickname or fetched.nickname == "匿名":
                    fetched.nickname = reply.nickname or fetched.nickname
                reply = fetched
        reply.reply = await self._enrich_reply(event, reply.reply, depth + 1)
        return reply

    async def _build_quote_message(
        self, event: AstrMessageEvent, reply_comp
    ) -> QuoteMessage | None:
        nickname = str(getattr(reply_comp, "sender_nickname", "") or "")
        user_id = str(getattr(reply_comp, "sender_id", "") or "")
        segments: list[MessageSegment] = []
        reply: ReplyMessage | None = None
        message_id = str(getattr(reply_comp, "id", "") or "")
        msg_time = _as_int(getattr(reply_comp, "time", 0), 0)

        chain = list(getattr(reply_comp, "chain", None) or [])
        if chain:
            segments, reply = self._parse_astr_chain(chain)

        if not segments and not reply:
            data = await self._fetch_onebot_message(event, message_id or None)
            if data:
                sender = (
                    data.get("sender") if isinstance(data.get("sender"), dict) else {}
                )
                nickname = str(
                    sender.get("card") or sender.get("nickname") or nickname
                )
                user_id = str(sender.get("user_id") or user_id)
                message_id = str(data.get("message_id") or message_id)
                # Reply.time 往往是当前那条命令的时间，优先用消息自己的时间
                msg_time = _as_int(data.get("time"), msg_time)
                segments, reply_id = self._parse_onebot_message(data)
                reply = await self._fetch_onebot_reply(event, reply_id, 1)
        else:
            reply = await self._enrich_reply(event, reply, 1)

        if not segments and not nickname:
            return None
        return QuoteMessage(
            user_id=user_id,
            nickname=nickname,
            segments=segments,
            reply=reply,
            message_id=message_id,
            time=msg_time,
        )

    async def _fetch_previous_messages(
        self, event: AstrMessageEvent, reply_comp, count: int, quoted: QuoteMessage
    ) -> list[QuoteMessage]:
        """取被引用消息之前的若干条，拼在它前面（被引用那条排最后）。"""
        want = max(0, count - 1)
        if want <= 0:
            return []
        group_id = str(event.get_group_id() or "")
        if not group_id:
            return []
        client = self._get_client(event)
        if client is None:
            return []
        target_id = quoted.message_id or str(getattr(reply_comp, "id", "") or "")
        if not target_id:
            return []

        # 多拉一点，靠 message_id 在窗口里定位被引用那条，再取它前面的
        try:
            result = await client.call_action(
                "get_group_msg_history",
                group_id=int(group_id),
                count=min(max(int(count) * 10, 120), 200),
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
        if not records:
            return []

        def _key(item: dict) -> tuple[int, int]:
            seq = _as_int(item.get("message_seq"), 0) or _as_int(
                item.get("message_id"), 0
            )
            return (_as_int(item.get("time"), 0), seq)

        ordered = sorted(records, key=_key)
        keys = [_key(item) for item in ordered]
        if keys and keys[0] == keys[-1]:
            # 时间和序号都拿不到，按接口惯例（新在前）反过来
            ordered = list(reversed(records))

        target_id = str(getattr(reply_comp, "id", "") or "")
        quoted_ts = _as_int(getattr(reply_comp, "time", 0), 0)
        self._last_window = ordered[-30:]
        logger.info(
            "[reply_guard] 历史窗口: "
            + ", ".join(
                f"{item.get('message_id')}@{_as_int(item.get('time'), 0)}"
                for item in ordered[:12]
            )
            + f" | 目标 {target_id}@{quoted_ts} | 需要前 {want} 条"
        )

        candidates: list[dict] = []
        start = -1
        for index, item in enumerate(ordered):
            if str(item.get("message_id") or "") == target_id:
                start = index
                break
        if start >= 0:
            candidates = ordered[max(0, start - want) : start]
        elif quoted.time:
            candidates = [
                item
                for item in ordered
                if _as_int(item.get("time"), 0)
                and _as_int(item.get("time"), 0) < quoted.time
            ][-want:]
        else:
            return []

        previous: list[QuoteMessage] = []
        for item in candidates:
            message = await self._onebot_to_quote(event, item)
            if message is not None:
                previous.append(message)
        return previous[-want:]

    async def _onebot_to_quote(
        self, event: AstrMessageEvent, data: dict
    ) -> QuoteMessage | None:
        sender = data.get("sender") if isinstance(data.get("sender"), dict) else {}
        nickname = str(sender.get("card") or sender.get("nickname") or "")
        user_id = str(sender.get("user_id") or "")
        segments, reply_id = self._parse_onebot_message(data)
        _ = reply_id
        if not segments and not nickname:
            return None
        return QuoteMessage(
            user_id=user_id,
            nickname=nickname,
            segments=segments,
            reply=None,
            message_id=str(data.get("message_id") or ""),
            time=_as_int(data.get("time"), 0),
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
            elif isinstance(comp, At):
                qq = str(getattr(comp, "qq", "") or "")
                name = str(getattr(comp, "name", "") or "")
                if qq == "all":
                    segments.append(MessageSegment(type="text", text="@全体成员"))
                elif qq:
                    segments.append(MessageSegment(type="at", id=qq, text=name))
            elif isinstance(comp, Reply):
                nested_chain = list(getattr(comp, "chain", None) or [])
                nested_id = str(getattr(comp, "id", "") or "")
                if not nested_chain:
                    reply = ReplyMessage(
                        nickname=str(getattr(comp, "sender_nickname", "") or ""),
                        segments=[
                            MessageSegment(type="text", text="[引用消息不可用]")
                        ],
                        message_id=nested_id,
                    )
                    continue
                nested_segments, nested_reply = ReplyGuardPlugin._parse_astr_chain(
                    nested_chain
                )
                reply = ReplyMessage(
                    nickname=str(getattr(comp, "sender_nickname", "") or ""),
                    segments=nested_segments,
                    reply=nested_reply,
                    message_id=nested_id,
                )
        return segments, reply

    @staticmethod
    def _parse_onebot_message(
        data: dict,
    ) -> tuple[list[MessageSegment], str | None]:
        segments: list[MessageSegment] = []
        reply_id: str | None = None
        raw = data.get("message")
        if raw is None:
            raw = data.get("raw_message")
        if isinstance(raw, str):
            text = raw.strip()
            if not text:
                return segments, None
            if "[CQ:" in text:
                return parse_cq_string(text)
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
                if qq:
                    segments.append(MessageSegment(type="at", id=qq))
            elif seg_type == "reply":
                reply_id = str(seg_data.get("id") or "") or None
        return segments, reply_id

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
