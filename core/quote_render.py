"""引用图渲染。

布局、配色、尺寸对照 zjutjh/qq-quote-generator（Go + SVG 版）重写的 Python 实现：
卡片 600 宽、圆角 12、浅色卡片底、头像 40 圆形、昵称 12、气泡白色圆角带内边距、
正文 15/行高 24、嵌套引用 2px 竖条 + 8px 缩进。渲染倍率 2 倍输出。
"""

from __future__ import annotations

import asyncio
import math
import os
import re
from dataclasses import dataclass, field
from typing import Any, Sequence

from astrbot.api import logger

try:
    from PIL import Image, ImageChops, ImageDraw, ImageFont
except ImportError:  # pragma: no cover
    Image = None
    ImageChops = None
    ImageDraw = None
    ImageFont = None


# ------------------------------------------------------------------ 常量

OUTPUT_SCALE = 2.0

CARD_MAX_WIDTH = 600.0
CARD_PAD_X = 12.0
CARD_PAD_Y = 16.0
AVATAR_SIZE = 40.0
ROW_GAP = 10.0
ROW_MARGIN = 14.0
NICKNAME_SIZE = 12.0
NICKNAME_HEIGHT = 16.0
NICKNAME_MARGIN = 4.0
BUBBLE_PAD_X = 12.0
BUBBLE_PAD_Y = 8.0
TEXT_SIZE = 15.0
TEXT_LINE_HEIGHT = 24.0
SEGMENT_MARGIN = 4.0
INLINE_EMOJI_SIZE = 24.0
INLINE_EMOJI_GAP = 2.0
IMAGE_RADIUS = 6.0
REPLY_BAR_WIDTH = 2.0
REPLY_INDENT = 8.0
REPLY_BODY_MARGIN = 8.0

CARD_BG = "#f7f8fb"
AVATAR_BG = "#d9dee8"
NICKNAME_COLOR = "#667085"
BUBBLE_BG = "#ffffff"
MESSAGE_COLOR = "#242937"
REPLY_TEXT_COLOR = "#475467"
REPLY_BAR_COLOR = "#d0d5dd"

MAX_REPLY_DEPTH = 3
MAX_REPLY_NODES = 30
REPLY_UNAVAILABLE_TEXT = "[引用消息不可用]"
REPLY_OMITTED_TEXT = "[更早的回复已省略]"

MAX_GIF_FRAMES = 100
MAX_GIF_DURATION = 500  # 厘秒
MIN_FRAME_DELAY = 2

AVATAR_URL = "https://q1.qlogo.cn/g?b=qq&nk={qq}&s=100"
QFACE_BASE_URL = "https://koishi.js.org/QFace/assets/qq_emoji"
MAX_IMAGE_BYTES = 16 << 20

_FONT_HINTS = (
    "simhei",
    "notosanscjk",
    "notosanssc",
    "notosans",
    "sourcehansans",
    "sourcehans",
    "wqy",
    "msyh",
    "droidsansfallback",
    "cjk",
)
_FONT_ROOTS = (
    "/AstrBot/data/fonts",
    "/AstrBot/data/koko/fonts",
    "/usr/share/fonts",
    "/usr/local/share/fonts",
    "/System/Library/Fonts",
    "C:/Windows/Fonts",
)

EMOJI_FONT_SIZE = 109  # Noto Color Emoji 是位图字体，只有这个字号
EMOJI_SCALE = 1.2

_EMOJI_FONT_ROOTS = (
    "/AstrBot/data/plugin_data/astrbot_plugin_reply_guard/fonts",
    "/AstrBot/data/fonts",
    "/AstrBot/data/koko/fonts",
    "/usr/share/fonts",
    "/usr/local/share/fonts",
    "/System/Library/Fonts",
    "C:/Windows/Fonts",
)
_EMOJI_FONT_HINTS = ("notocoloremoji", "applecoloremoji", "seguiemj", "emoji")

_EMOJI_CHAR_RE = re.compile(
    "["
    "\U0001F300-\U0001FAFF"
    "\U0001F1E6-\U0001F1FF"
    "\U0001F3FB-\U0001F3FF"
    "\u2600-\u27BF"
    "\u2B00-\u2BFF"
    "\u2190-\u21FF"
    "]"
)

_font_cache: dict[tuple[str, int], Any] = {}
_resolved_font: str | None = None
_resolved_emoji_font: str | None = None
_emoji_font_obj: Any = None


def resolve_font_path(preferred: str = "") -> str:
    global _resolved_font
    if preferred:
        path = os.path.expanduser(preferred)
        if os.path.isfile(path):
            return path
    if _resolved_font is not None:
        return _resolved_font

    found: list[str] = []
    for root in _FONT_ROOTS:
        if not os.path.isdir(root):
            continue
        for dirpath, _dirnames, filenames in os.walk(root):
            for name in filenames:
                lower = name.lower()
                if lower.endswith((".ttf", ".otf", ".ttc")):
                    found.append(os.path.join(dirpath, name))
    for path in found:
        if any(hint in os.path.basename(path).lower() for hint in _FONT_HINTS):
            _resolved_font = path
            return path
    for path in found:
        lower = os.path.basename(path).lower()
        if "dejavu" in lower or "emoji" in lower:
            continue
        _resolved_font = path
        return path
    _resolved_font = ""
    return ""


def _load_font(font_path: str, size: float):
    px = max(1, int(round(size * OUTPUT_SCALE)))
    key = (font_path, px)
    cached = _font_cache.get(key)
    if cached is not None:
        return cached
    if not font_path:
        font = ImageFont.load_default()
    else:
        try:
            font = ImageFont.truetype(font_path, px)
        except Exception:
            try:
                font = ImageFont.truetype(font_path, px, index=0)
            except Exception:
                font = ImageFont.load_default()
    _font_cache[key] = font
    return font


def resolve_emoji_font_path(preferred: str = "") -> str:
    """找彩色 emoji 字体（Noto Color Emoji / Apple Color Emoji 那一类）。"""
    global _resolved_emoji_font
    if preferred:
        path = os.path.expanduser(preferred)
        if os.path.isfile(path):
            return path
    if _resolved_emoji_font is not None:
        return _resolved_emoji_font

    found: list[str] = []
    for root in _EMOJI_FONT_ROOTS:
        if not os.path.isdir(root):
            continue
        for dirpath, _dirnames, filenames in os.walk(root):
            for name in filenames:
                if name.lower().endswith((".ttf", ".otf", ".ttc")):
                    found.append(os.path.join(dirpath, name))

    for path in found:
        if any(hint in os.path.basename(path).lower() for hint in _EMOJI_FONT_HINTS):
            _resolved_emoji_font = path
            return path

    _resolved_emoji_font = ""
    return ""


def _load_emoji_font():
    global _emoji_font_obj
    if _emoji_font_obj is not None:
        return _emoji_font_obj
    path = _resolved_emoji_font or ""
    if not path:
        return None
    try:
        _emoji_font_obj = ImageFont.truetype(path, EMOJI_FONT_SIZE)
    except Exception:
        try:
            _emoji_font_obj = ImageFont.truetype(path, EMOJI_FONT_SIZE, index=0)
        except Exception as e:
            logger.debug(f"[reply_guard] emoji 字体打不开: {e}")
            _emoji_font_obj = None
    return _emoji_font_obj


def _render_emoji(char: str, target_height: int):
    """用彩色字体画一个 emoji，再缩放到目标高度。"""
    font = _load_emoji_font()
    if font is None or not char:
        return None
    try:
        canvas = Image.new(
            "RGBA", (EMOJI_FONT_SIZE * 2, EMOJI_FONT_SIZE * 2), (0, 0, 0, 0)
        )
        ImageDraw.Draw(canvas).text((0, 0), char, font=font, embedded_color=True)
        box = canvas.getbbox()
        if not box:
            return None
        canvas = canvas.crop(box)
        ratio = target_height / max(1, canvas.height)
        return canvas.resize(
            (max(1, int(round(canvas.width * ratio))), max(1, target_height)),
            Image.LANCZOS,
        )
    except Exception as e:
        logger.debug(f"[reply_guard] emoji 渲染失败 {char!r}: {e}")
        return None


# ------------------------------------------------------------------ 数据结构


@dataclass
class MessageSegment:
    type: str = "text"  # text | image | face
    kind: str = ""  # emoji | sticker
    text: str = ""
    url: str = ""
    id: str = ""


@dataclass
class ReplyMessage:
    nickname: str = ""
    segments: list[MessageSegment] = field(default_factory=list)
    reply: "ReplyMessage | None" = None
    message_id: str = ""


@dataclass
class QuoteMessage:
    user_id: str = ""
    nickname: str = ""
    avatar: str = ""
    segments: list[MessageSegment] = field(default_factory=list)
    reply: ReplyMessage | None = None
    message_id: str = ""
    time: int = 0


@dataclass
class LoadedImage:
    image: Any = None
    width: int = 0
    height: int = 0
    animation: list[tuple[Any, int]] | None = None


# ------------------------------------------------------------------ 测量 / 换行


class FontManager:
    def __init__(self, font_path: str, emoji_font_path: str = "") -> None:
        self.font_path = font_path
        self.emoji_font_path = emoji_font_path or resolve_emoji_font_path()

    def font(self, size: float):
        return _load_font(self.font_path, size)

    @property
    def has_emoji_font(self) -> bool:
        return bool(self.emoji_font_path) and _load_emoji_font() is not None

    @staticmethod
    def runs(text: str) -> list[tuple[bool, str]]:
        """按 emoji / 非 emoji 切段，方便分段换字体。"""
        runs: list[tuple[bool, str]] = []
        for char in text:
            is_emoji = bool(_EMOJI_CHAR_RE.match(char))
            if runs and runs[-1][0] == is_emoji:
                runs[-1] = (is_emoji, runs[-1][1] + char)
            else:
                runs.append((is_emoji, char))
        return runs

    def plain_measure(self, text: str, size: float) -> float:
        if not text:
            return 0.0
        font = self.font(size)
        try:
            return float(font.getlength(text)) / OUTPUT_SCALE
        except Exception:
            return len(text) * size * 0.6

    def measure(self, text: str, size: float) -> float:
        if not text:
            return 0.0
        if not self.has_emoji_font:
            return self.plain_measure(text, size)
        total = 0.0
        for is_emoji, chunk in self.runs(text):
            if is_emoji:
                total += size * EMOJI_SCALE * len(chunk)
            else:
                total += self.plain_measure(chunk, size)
        return total

    def line_metrics(self, text: str, size: float) -> tuple[float, float, float]:
        """返回 (宽度, ascent, descent)，单位是逻辑像素，descent 为正。"""
        font = self.font(size)
        try:
            ascent, descent = font.getmetrics()
        except Exception:
            ascent, descent = int(size), int(size * 0.25)
        ascent_l = ascent / OUTPUT_SCALE
        descent_l = descent / OUTPUT_SCALE
        return self.measure(text, size), ascent_l, descent_l


def _line_tokens(text: str) -> list[str]:
    """ASCII 字母数字连成一段，其余逐字，跟原版 lineTokens 一致。"""
    tokens: list[str] = []
    latin: list[str] = []
    for char in text:
        if char.isascii() and (char.isalpha() or char.isdigit()):
            latin.append(char)
            continue
        if latin:
            tokens.append("".join(latin))
            latin = []
        tokens.append(char)
    if latin:
        tokens.append("".join(latin))
    return tokens


@dataclass
class TextLine:
    text: str
    width: float
    baseline: float


@dataclass
class TextLayout:
    x: float = 0.0
    y: float = 0.0
    width: float = 0.0
    height: float = 0.0
    lines: list[TextLine] = field(default_factory=list)


@dataclass
class SegmentLayout:
    type: str = "text"
    kind: str = ""
    x: float = 0.0
    y: float = 0.0
    width: float = 0.0
    height: float = 0.0
    lines: list[TextLine] = field(default_factory=list)
    image: Any = None
    animation: list[tuple[Any, int]] | None = None


@dataclass
class ReplyLayout:
    bar_x: float = 0.0
    bar_y: float = 0.0
    bar_w: float = 0.0
    bar_h: float = 0.0
    nickname: TextLayout = field(default_factory=TextLayout)
    segments: list[SegmentLayout] = field(default_factory=list)
    reply: "ReplyLayout | None" = None


@dataclass
class RowLayout:
    height: float = 0.0
    avatar_x: float = 0.0
    avatar_y: float = 0.0
    avatar: Any = None
    nickname: TextLayout = field(default_factory=TextLayout)
    bubble_x: float = 0.0
    bubble_y: float = 0.0
    bubble_w: float = 0.0
    bubble_h: float = 0.0
    reply: ReplyLayout | None = None
    segments: list[SegmentLayout] = field(default_factory=list)


@dataclass
class CardLayout:
    width: float = 0.0
    height: float = 0.0
    rows: list[RowLayout] = field(default_factory=list)


# ------------------------------------------------------------------ 布局


class LayoutEngine:
    def __init__(self, fonts: FontManager) -> None:
        self.fonts = fonts

    def _text_line(self, text: str, size: float, line_height: float) -> TextLine:
        width, ascent, descent = self.fonts.line_metrics(text, size)
        baseline = (line_height - (ascent + descent)) / 2.0 + ascent
        return TextLine(text=text, width=width, baseline=baseline)

    def wrap_text(
        self, text: str, max_width: float, size: float, line_height: float
    ) -> list[TextLine]:
        lines: list[TextLine] = []
        for paragraph in str(text).split("\n"):
            if paragraph == "":
                lines.append(self._text_line("", size, line_height))
                continue
            current = ""
            for token in _line_tokens(paragraph):
                if current and self.fonts.measure(current + token, size) > max_width:
                    lines.append(self._text_line(current, size, line_height))
                    current = ""
                if self.fonts.measure(token, size) <= max_width:
                    current += token
                    continue
                for char in token:
                    if current and self.fonts.measure(current + char, size) > max_width:
                        lines.append(self._text_line(current, size, line_height))
                        current = ""
                    current += char
            lines.append(self._text_line(current, size, line_height))
        return lines

    def layout(self, messages: list[dict]) -> CardLayout:
        card = CardLayout(width=CARD_PAD_X * 2, height=CARD_PAD_Y * 2)
        available = CARD_MAX_WIDTH - CARD_PAD_X * 2 - AVATAR_SIZE - ROW_GAP
        max_row_width = 0.0
        y = CARD_PAD_Y
        rows: list[RowLayout] = []
        for message in messages:
            row = self._layout_row(message, available, y)
            if self._needs_max_width(message, available - BUBBLE_PAD_X * 2):
                row.bubble_w = available
            rows.append(row)
            row_width = AVATAR_SIZE + ROW_GAP + max(
                row.nickname.width, row.bubble_w
            )
            max_row_width = max(max_row_width, row_width)
            y += row.height + ROW_MARGIN
        if rows:
            card.height = y - ROW_MARGIN + CARD_PAD_Y
            card.width = min(CARD_MAX_WIDTH, CARD_PAD_X * 2 + max_row_width)
        card.rows = rows
        return card

    def _needs_max_width(self, message: dict, inner_width: float) -> bool:
        if self._segments_need_max_width(message.get("segments") or [], inner_width):
            return True
        return self._reply_needs_max_width(message.get("reply"), inner_width)

    def _segments_need_max_width(
        self, segments: list[MessageSegment], max_width: float
    ) -> bool:
        for segment in segments:
            if segment.type != "text":
                continue
            for paragraph in str(segment.text).split("\n"):
                if self.fonts.measure(paragraph, TEXT_SIZE) > max_width:
                    return True
        return False

    def _reply_needs_max_width(self, reply: Any, max_width: float) -> bool:
        if reply is None:
            return False
        content_width = max_width - REPLY_BAR_WIDTH - REPLY_INDENT
        if content_width <= 0:
            return True
        if self.fonts.measure(reply.nickname, NICKNAME_SIZE) > content_width:
            return True
        if self._segments_need_max_width(reply.segments, content_width):
            return True
        return self._reply_needs_max_width(reply.reply, content_width)

    def _layout_row(self, message: dict, max_content_width: float, y: float) -> RowLayout:
        content_x = CARD_PAD_X + AVATAR_SIZE + ROW_GAP
        nickname_line = self._text_line(message["nickname"], NICKNAME_SIZE, NICKNAME_HEIGHT)
        max_inner = max_content_width - BUBBLE_PAD_X * 2
        inner_width, inner_height = 0.0, 0.0
        inner_x = content_x + BUBBLE_PAD_X
        inner_y = y + NICKNAME_HEIGHT + NICKNAME_MARGIN + BUBBLE_PAD_Y

        reply = None
        if message.get("reply") is not None:
            reply, width, height = self._layout_reply(
                message["reply"], max_inner, inner_x, inner_y
            )
            inner_width = max(inner_width, width)
            inner_height += height
        if message.get("reply") is not None and message.get("segments"):
            inner_height += REPLY_BODY_MARGIN

        segments, width, height = self._layout_segments(
            message.get("segments") or [], max_inner, inner_x, inner_y + inner_height
        )
        inner_width = max(inner_width, width)
        inner_height += height

        bubble_width = inner_width + BUBBLE_PAD_X * 2
        bubble_height = inner_height + BUBBLE_PAD_Y * 2
        bubble_y = y + NICKNAME_HEIGHT + NICKNAME_MARGIN
        row_height = max(AVATAR_SIZE, NICKNAME_HEIGHT + NICKNAME_MARGIN + bubble_height)

        return RowLayout(
            height=row_height,
            avatar_x=CARD_PAD_X,
            avatar_y=y,
            avatar=message.get("avatar"),
            nickname=TextLayout(
                x=content_x,
                y=y,
                width=nickname_line.width,
                height=NICKNAME_HEIGHT,
                lines=[nickname_line],
            ),
            bubble_x=content_x,
            bubble_y=bubble_y,
            bubble_w=bubble_width,
            bubble_h=bubble_height,
            reply=reply,
            segments=segments,
        )

    def _layout_reply(
        self, reply: ReplyMessage, max_width: float, x: float, y: float
    ) -> tuple[ReplyLayout, float, float]:
        layout = ReplyLayout()
        content_x = x + REPLY_BAR_WIDTH + REPLY_INDENT
        content_width = max_width - REPLY_BAR_WIDTH - REPLY_INDENT
        cursor_y = y
        used_width = 0.0

        if reply.reply is not None:
            layout.reply, width, height = self._layout_reply(
                reply.reply, content_width, content_x, cursor_y
            )
            used_width = max(used_width, width)
            cursor_y += height + SEGMENT_MARGIN

        if reply.nickname:
            lines = self.wrap_text(
                reply.nickname, content_width, NICKNAME_SIZE, NICKNAME_HEIGHT
            )
            nickname_layout = TextLayout(
                x=content_x,
                y=cursor_y,
                height=len(lines) * NICKNAME_HEIGHT,
                lines=lines,
            )
            for line in lines:
                nickname_layout.width = max(nickname_layout.width, line.width)
            layout.nickname = nickname_layout
            used_width = max(used_width, nickname_layout.width)
            cursor_y += nickname_layout.height + NICKNAME_MARGIN

        segments, width, height = self._layout_segments(
            reply.segments, content_width, content_x, cursor_y
        )
        layout.segments = segments
        used_width = max(used_width, width)
        cursor_y += height
        total_height = cursor_y - y
        layout.bar_x = x
        layout.bar_y = y
        layout.bar_w = REPLY_BAR_WIDTH
        layout.bar_h = total_height
        return layout, REPLY_BAR_WIDTH + REPLY_INDENT + used_width, total_height

    def _layout_segments(
        self, segments: list[MessageSegment], max_width: float, x: float, y: float
    ) -> tuple[list[SegmentLayout], float, float]:
        layouts: list[SegmentLayout] = []
        used_width, used_height = 0.0, 0.0
        index = 0
        while index < len(segments):
            end = _inline_group_end(segments, index)
            if end > index:
                if layouts:
                    used_height += SEGMENT_MARGIN
                group, width, height = self._layout_inline_group(
                    segments[index:end], max_width, x, y + used_height
                )
                layouts.extend(group)
                used_width = max(used_width, width)
                used_height += height
                index = end
                continue

            segment = segments[index]
            if layouts:
                used_height += SEGMENT_MARGIN
            layout = SegmentLayout(type=segment.type, kind=segment.kind)
            if segment.type == "text":
                layout.lines = self.wrap_text(
                    segment.text, max_width, TEXT_SIZE, TEXT_LINE_HEIGHT
                )
                for line in layout.lines:
                    layout.width = max(layout.width, line.width)
                layout.height = max(1, len(layout.lines)) * TEXT_LINE_HEIGHT
            elif segment.type == "image":
                max_w, max_h = 200.0, 200.0
                if segment.kind == "emoji":
                    max_w = max_h = INLINE_EMOJI_SIZE
                elif segment.kind == "sticker":
                    max_w = max_h = 128.0
                image = segment_image(segment)
                if image is not None and image.width > 0 and image.height > 0:
                    scale = min(
                        1.0,
                        max_w / image.width,
                        max_h / image.height,
                    )
                    layout.width = image.width * scale
                    layout.height = image.height * scale
                    if segment.kind == "emoji":
                        layout.width = layout.height = INLINE_EMOJI_SIZE
                    layout.image = image.image
                    layout.animation = image.animation
                else:
                    if segment.kind == "emoji" and segment.text:
                        placeholder = segment.text
                    elif segment.type == "face":
                        placeholder = "[表情]"
                    else:
                        placeholder = "[图片]"
                    layout.type = "text"
                    layout.lines = [
                        self._text_line(placeholder, TEXT_SIZE, TEXT_LINE_HEIGHT)
                    ]
                    layout.width = layout.lines[0].width
                    layout.height = TEXT_LINE_HEIGHT
            layout.x = x
            layout.y = y + used_height
            used_width = max(used_width, layout.width)
            used_height += layout.height
            layouts.append(layout)
            index += 1
        return layouts, used_width, used_height

    def _layout_inline_group(
        self, group: list[MessageSegment], max_width: float, x: float, y: float
    ) -> tuple[list[SegmentLayout], float, float]:
        layouts: list[SegmentLayout] = []
        cursor_x, line_y, used_width = 0.0, 0.0, 0.0
        previous_emoji = False

        def new_line() -> None:
            nonlocal cursor_x, line_y, used_width, previous_emoji
            used_width = max(used_width, cursor_x)
            cursor_x = 0.0
            line_y += TEXT_LINE_HEIGHT
            previous_emoji = False

        for segment in group:
            if segment.type == "image":
                gap = INLINE_EMOJI_GAP if cursor_x > 0 else 0.0
                if cursor_x + gap + INLINE_EMOJI_SIZE > max_width:
                    new_line()
                    gap = 0.0
                cursor_x += gap
                image = segment_image(segment)
                layouts.append(
                    SegmentLayout(
                        type="image",
                        kind=segment.kind,
                        x=x + cursor_x,
                        y=y + line_y,
                        width=INLINE_EMOJI_SIZE,
                        height=INLINE_EMOJI_SIZE,
                        image=image.image if image is not None else None,
                        animation=image.animation if image is not None else None,
                    )
                )
                cursor_x += INLINE_EMOJI_SIZE
                previous_emoji = True
                continue

            paragraphs = str(segment.text).split("\n")
            for paragraph_index, paragraph in enumerate(paragraphs):
                if paragraph_index > 0:
                    new_line()
                fragment = ""

                def flush() -> None:
                    nonlocal fragment, cursor_x, previous_emoji
                    if not fragment:
                        return
                    if previous_emoji and cursor_x > 0:
                        cursor_x += INLINE_EMOJI_GAP
                    line = self._text_line(fragment, TEXT_SIZE, TEXT_LINE_HEIGHT)
                    layouts.append(
                        SegmentLayout(
                            type="text",
                            x=x + cursor_x,
                            y=y + line_y,
                            width=line.width,
                            height=TEXT_LINE_HEIGHT,
                            lines=[line],
                        )
                    )
                    cursor_x += line.width
                    fragment = ""
                    previous_emoji = False

                for token in _line_tokens(paragraph):
                    gap = INLINE_EMOJI_GAP if (previous_emoji and cursor_x > 0) else 0.0
                    if (
                        self.fonts.measure(fragment + token, TEXT_SIZE)
                        <= max_width - cursor_x - gap
                    ):
                        fragment += token
                        continue
                    flush()
                    if cursor_x > 0:
                        new_line()
                    if self.fonts.measure(token, TEXT_SIZE) <= max_width:
                        fragment = token
                        continue
                    for char in token:
                        if (
                            fragment
                            and self.fonts.measure(fragment + char, TEXT_SIZE) > max_width
                        ):
                            flush()
                            new_line()
                        fragment += char
                flush()

        used_width = max(used_width, cursor_x)
        return layouts, used_width, line_y + TEXT_LINE_HEIGHT


def _inline_group_end(segments: list[MessageSegment], start: int) -> int:
    end = start
    has_text, has_emoji = False, False
    while end < len(segments):
        segment = segments[end]
        if segment.type == "text":
            has_text = True
        elif segment.type == "image" and segment.kind == "emoji":
            has_emoji = True
        else:
            break
        end += 1
    return end if (has_text and has_emoji) else start


# ------------------------------------------------------------------ 资源


def _decode_animation(img: Any) -> list[tuple[Any, int]] | None:
    """多帧 GIF / APNG 解成 [(帧, 时长厘秒)]，非动图返回 None。"""
    try:
        if not getattr(img, "is_animated", False):
            return None
        frames: list[tuple[Any, int]] = []
        for index in range(int(getattr(img, "n_frames", 1))):
            img.seek(index)
            frame = img.convert("RGBA").copy()
            delay = int(img.info.get("duration") or 0) or 100
            frames.append((frame, max(2, int(round(delay / 10)))))
        if len(frames) < 2:
            return None
        return frames
    except Exception as e:
        logger.debug(f"[reply_guard] 解码动图失败: {e}")
        return None


class ResourceLoader:
    def __init__(self, proxy: str = "", cache_dir: str = "") -> None:
        self.proxy = proxy
        self.cache_dir = cache_dir
        self._cache: dict[str, LoadedImage] = {}

    async def _fetch(self, url: str) -> bytes | None:
        if not url:
            return None
        if url.lower().startswith("data:image/"):
            try:
                import base64

                header, payload = url.split(",", 1)
                if ";base64" not in header.lower():
                    return None
                return base64.b64decode(payload)
            except Exception:
                return None
        try:
            import aiohttp
        except ImportError:
            return None
        try:
            timeout = aiohttp.ClientTimeout(total=10)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(url, proxy=self.proxy or None) as resp:
                    if resp.status != 200:
                        logger.debug(f"[reply_guard] 图片 HTTP {resp.status}: {url}")
                        return None
                    data = await resp.read()
                    if len(data) > MAX_IMAGE_BYTES:
                        return None
                    return data
        except Exception as e:
            logger.debug(f"[reply_guard] 下载图片失败 {url}: {e}")
            return None

    async def load(self, source: str) -> LoadedImage:
        source = (source or "").strip()
        if not source:
            return LoadedImage()
        cached = self._cache.get(source)
        if cached is not None:
            return cached
        data = await self._fetch(source)
        if not data:
            result = LoadedImage()
        else:
            try:
                from io import BytesIO

                img = Image.open(BytesIO(data))
                img.load()
                animation = _decode_animation(img)
                result = LoadedImage(
                    image=img.convert("RGBA"),
                    width=img.width,
                    height=img.height,
                    animation=animation,
                )
            except Exception as e:
                logger.debug(f"[reply_guard] 解码图片失败: {e}")
                result = LoadedImage()
        self._cache[source] = result
        return result


def segment_image(segment: MessageSegment) -> LoadedImage | None:
    image = getattr(segment, "_image", None)
    return image if isinstance(image, LoadedImage) else None


_TWEMOJI_URL = "https://cdn.jsdelivr.net/gh/jdecked/twemoji@15.1.0/assets/72x72/{name}.png"

_EMOJI_SEQ_RE = re.compile(
    "(?:"
    "[\U0001F1E6-\U0001F1FF]{2}"
    "|[\U0001F300-\U0001FAFF\u2600-\u27BF\u2B00-\u2BFF\u2190-\u21FF]"
    "\uFE0F?"
    "[\U0001F3FB-\U0001F3FF]?"
    "(?:\u200D[\U0001F300-\U0001FAFF\u2600-\u27BF\u2B00-\u2BFF]"
    "\uFE0F?[\U0001F3FB-\U0001F3FF]?)*"
    ")"
)


def _emoji_name(chars: str) -> str:
    codepoints = [ord(char) for char in chars if ord(char) != 0xFE0F]
    return "-".join(f"{cp:x}" for cp in codepoints)


def _split_text_emoji(text: str) -> list[MessageSegment]:
    """把文本里的 emoji 拆成图片段，交给内联表情排版。"""
    segments: list[MessageSegment] = []
    cursor = 0
    for match in _EMOJI_SEQ_RE.finditer(text):
        start, end = match.span()
        if start > cursor:
            segments.append(MessageSegment(type="text", text=text[cursor:start]))
        segments.append(
            MessageSegment(
                type="image",
                kind="emoji",
                url=_TWEMOJI_URL.format(name=_emoji_name(match.group())),
                text=match.group(),
            )
        )
        cursor = end
    if cursor < len(text):
        segments.append(MessageSegment(type="text", text=text[cursor:]))
    return segments or [MessageSegment(type="text", text=text)]


def _expand_segments(segments: list[MessageSegment]) -> list[MessageSegment]:
    expanded: list[MessageSegment] = []
    for segment in segments:
        if segment.type == "text" and segment.text:
            expanded.extend(_split_text_emoji(segment.text))
        else:
            expanded.append(segment)
    return expanded


_CQ_CODE_RE = re.compile(r"\[CQ:([a-zA-Z_]+),?([^\]]*)\]")


def _merge_text_segments(segments: list[MessageSegment]) -> list[MessageSegment]:
    """相邻的文本段合并成一段，免得被排版拆成多行。"""
    merged: list[MessageSegment] = []
    for segment in segments:
        if segment.type == "text":
            if not (segment.text or ""):
                continue
            if merged and merged[-1].type == "text":
                merged[-1].text = (merged[-1].text or "") + (segment.text or "")
                continue
        merged.append(segment)
    return merged


def _parse_cq_string(text: str) -> tuple[list[MessageSegment], str | None]:
    """解析 [CQ:xxx,key=value] 形式的消息串（get_msg 返回的就是这种）。"""
    segments: list[MessageSegment] = []
    reply_id: str | None = None
    cursor = 0
    for match in _CQ_CODE_RE.finditer(text or ""):
        if match.start() > cursor:
            plain = text[cursor : match.start()]
            if plain:
                segments.append(MessageSegment(type="text", text=plain))
        code = match.group(1)
        params: dict[str, str] = {}
        for pair in match.group(2).split(","):
            if "=" in pair:
                key, value = pair.split("=", 1)
                params[key.strip()] = value.strip()

        if code == "at":
            qq = str(params.get("qq") or "")
            if qq == "all":
                segments.append(MessageSegment(type="text", text="@全体成员"))
            elif qq:
                segments.append(MessageSegment(type="at", id=qq))
        elif code == "image":
            url = str(params.get("url") or "")
            if url.startswith("http"):
                segments.append(MessageSegment(type="image", kind="image", url=url))
            else:
                segments.append(MessageSegment(type="text", text="[图片]"))
        elif code == "face":
            face_id = str(params.get("id") or "")
            if face_id.isdigit():
                segments.append(MessageSegment(type="face", kind="emoji", id=face_id))
            else:
                segments.append(MessageSegment(type="text", text="[表情]"))
        elif code == "reply":
            reply_id = str(params.get("id") or "") or None
        elif code == "json" or code == "xml":
            segments.append(MessageSegment(type="text", text="[卡片消息]"))
        cursor = match.end()

    if cursor < len(text or ""):
        tail = text[cursor:]
        if tail:
            segments.append(MessageSegment(type="text", text=tail))
    return _merge_text_segments(segments), reply_id


def _segment_urls(segment: MessageSegment) -> list[str]:
    """图片段和 QQ 表情要取的地址，表情优先 apng。"""
    if segment.type == "image" and segment.url:
        return [segment.url]
    face_id = str(segment.id or "")
    if segment.type == "face" and face_id.isdigit():
        return [
            f"{QFACE_BASE_URL}/{face_id}/apng/{face_id}.png",
            f"{QFACE_BASE_URL}/{face_id}/png/{face_id}.png",
        ]
    return []


async def _load_first(loader: ResourceLoader, urls: list[str]) -> LoadedImage:
    for url in urls:
        image = await loader.load(url)
        if image.image is not None:
            return image
    return LoadedImage()


async def prepare_messages(
    loader: ResourceLoader, messages: Sequence[QuoteMessage]
) -> list[dict]:
    """把消息转成布局用的结构，顺便并发下好头像和图片。"""
    prepared: list[dict] = []
    tasks: list[tuple[Any, str, list[str]]] = []

    def register(owner: Any, attr: str, urls: list[str]) -> None:
        urls = [u for u in urls if u]
        if urls:
            tasks.append((owner, attr, urls))

    def walk_reply(reply: ReplyMessage | None, depth: int = 0) -> ReplyMessage | None:
        if reply is None or depth > MAX_REPLY_DEPTH:
            return None
        reply.nickname = reply.nickname or "匿名"
        if not reply.segments:
            reply.segments = [
                MessageSegment(type="text", text=REPLY_UNAVAILABLE_TEXT)
            ]
        reply.segments = _merge_text_segments(list(reply.segments))
        for segment in reply.segments:
            register(segment, "_image", _segment_urls(segment))
        reply.reply = walk_reply(reply.reply, depth + 1)
        return reply

    for message in messages:
        avatar_url = message.avatar or (
            AVATAR_URL.format(qq=message.user_id)
            if str(message.user_id).isdigit()
            else ""
        )
        item: dict[str, Any] = {
            "nickname": message.nickname or "匿名",
            "avatar": None,
            "segments": _merge_text_segments(list(message.segments)),
            "reply": None,
        }
        if avatar_url:
            register(item, "avatar", [avatar_url])
        for segment in message.segments:
            register(segment, "_image", _segment_urls(segment))
        item["reply"] = walk_reply(message.reply) if message.reply else None
        prepared.append(item)

    if tasks:
        results = await asyncio.gather(
            *[_load_first(loader, urls) for _owner, _attr, urls in tasks],
            return_exceptions=True,
        )
        for (owner, attr, _urls), result in zip(tasks, results):
            if not isinstance(result, LoadedImage):
                continue
            if isinstance(owner, dict):
                owner[attr] = result.image
            else:
                setattr(owner, attr, result)

    return prepared


# ------------------------------------------------------------------ 绘制


def _hex(color: str) -> tuple[int, int, int]:
    color = color.lstrip("#")
    return tuple(int(color[i : i + 2], 16) for i in (0, 2, 4))  # type: ignore[return-value]


def _s(value: float) -> float:
    return value * OUTPUT_SCALE


def _circle_image(img: Any, size: int) -> Any:
    """头像按中心裁成正方形再画圆（原版是 slice，不拉伸）。"""
    img = img.convert("RGBA")
    width, height = img.size
    side = min(width, height)
    if side > 0:
        left = (width - side) // 2
        top = (height - side) // 2
        img = img.crop((left, top, left + side, top + side))
    img = img.resize((size, size), Image.LANCZOS)
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, size, size), fill=255)
    out = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    out.paste(img, (0, 0), mask)
    return out


def _rounded_image(img: Any, size: tuple[int, int], radius: int) -> Any:
    img = img.convert("RGBA").resize(size, Image.LANCZOS)
    mask = Image.new("L", size, 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        (0, 0, size[0] - 1, size[1] - 1), radius=radius, fill=255
    )
    out = Image.new("RGBA", size, (0, 0, 0, 0))
    out.paste(img, (0, 0), mask)
    return out


def _draw_text(
    canvas: Any,
    draw: Any,
    x: float,
    baseline: float,
    text: str,
    fonts: FontManager,
    size: float,
    color: str,
) -> None:
    if not text:
        return
    font = fonts.font(size)
    try:
        ascent, descent = font.getmetrics()
    except Exception:
        ascent, descent = 0, 0

    if not fonts.has_emoji_font:
        draw.text((_s(x), _s(baseline) - ascent), text, font=font, fill=_hex(color))
        return

    cursor = x
    for is_emoji, chunk in fonts.runs(text):
        if is_emoji:
            target = max(1, int(round(_s(size) * EMOJI_SCALE)))
            drawn = False
            for char in chunk:
                img = _render_emoji(char, target)
                if img is None:
                    continue
                top = int(round(_s(baseline) + descent - target))
                canvas.paste(img, (int(round(_s(cursor))), top), img)
                cursor += size * EMOJI_SCALE
                drawn = True
            if drawn:
                continue
        draw.text((_s(cursor), _s(baseline) - ascent), chunk, font=font, fill=_hex(color))
        cursor += fonts.plain_measure(chunk, size)


def _draw_segments(
    canvas: Any,
    draw: Any,
    layouts: list[SegmentLayout],
    text_color: str,
    fonts: FontManager,
    skip_animation: bool = False,
) -> None:
    for segment in layouts:
        if segment.type == "text":
            for index, line in enumerate(segment.lines):
                baseline = segment.y + line.baseline + index * TEXT_LINE_HEIGHT
                _draw_text(
                    canvas, draw, segment.x, baseline, line.text, fonts, TEXT_SIZE, text_color
                )
        elif segment.image is not None:
            if skip_animation and segment.animation:
                continue
            size = (max(1, int(_s(segment.width))), max(1, int(_s(segment.height))))
            if segment.kind == "emoji":
                img = _circle_image(segment.image, size[0]) if False else segment.image.convert(
                    "RGBA"
                ).resize(size, Image.LANCZOS)
            else:
                img = _rounded_image(
                    segment.image, size, max(1, int(_s(IMAGE_RADIUS)))
                )
            canvas.paste(img, (int(_s(segment.x)), int(_s(segment.y))), img)


def _draw_reply(
    canvas: Any,
    draw: Any,
    reply: ReplyLayout | None,
    fonts: FontManager,
    skip_animation: bool = False,
) -> None:
    if reply is None:
        return
    draw.rectangle(
        (
            _s(reply.bar_x),
            _s(reply.bar_y),
            _s(reply.bar_x + reply.bar_w),
            _s(reply.bar_y + reply.bar_h),
        ),
        fill=_hex(REPLY_BAR_COLOR),
    )
    _draw_reply(canvas, draw, reply.reply, fonts, skip_animation)
    for index, line in enumerate(reply.nickname.lines):
        baseline = (
            reply.nickname.y + line.baseline + index * NICKNAME_HEIGHT
        )
        _draw_text(
            canvas,
            draw,
            reply.nickname.x,
            baseline,
            line.text,
            fonts,
            NICKNAME_SIZE,
            NICKNAME_COLOR,
        )
    _draw_segments(canvas, draw, reply.segments, REPLY_TEXT_COLOR, fonts, skip_animation)


def render_card_sync(
    card: CardLayout, font_path: str, skip_animation: bool = False
) -> Any:
    fonts = FontManager(font_path)
    width = max(1, int(math.ceil(_s(card.width))))
    height = max(1, int(math.ceil(_s(card.height))))
    canvas = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(canvas)
    draw.rounded_rectangle(
        (0, 0, width - 1, height - 1),
        radius=int(_s(12)),
        fill=_hex(CARD_BG),
    )

    nickname_font = fonts.font(NICKNAME_SIZE)
    for row in card.rows:
        # 头像
        avatar_size = int(_s(AVATAR_SIZE))
        draw.ellipse(
            (
                _s(row.avatar_x),
                _s(row.avatar_y),
                _s(row.avatar_x + AVATAR_SIZE),
                _s(row.avatar_y + AVATAR_SIZE),
            ),
            fill=_hex(AVATAR_BG),
        )
        if row.avatar is not None:
            avatar_img = _circle_image(row.avatar, avatar_size)
            canvas.paste(
                avatar_img, (int(_s(row.avatar_x)), int(_s(row.avatar_y))), avatar_img
            )

        # 昵称
        for index, line in enumerate(row.nickname.lines):
            baseline = row.nickname.y + line.baseline + index * NICKNAME_HEIGHT
            _draw_text(
                canvas,
                draw,
                row.nickname.x,
                baseline,
                line.text,
                fonts,
                NICKNAME_SIZE,
                NICKNAME_COLOR,
            )

        # 气泡
        draw.rounded_rectangle(
            (
                _s(row.bubble_x),
                _s(row.bubble_y),
                _s(row.bubble_x + row.bubble_w),
                _s(row.bubble_y + row.bubble_h),
            ),
            radius=int(_s(12)),
            fill=_hex(BUBBLE_BG),
        )

        _draw_reply(canvas, draw, row.reply, fonts, skip_animation)
        _draw_segments(canvas, draw, row.segments, MESSAGE_COLOR, fonts, skip_animation)

    return canvas


# ------------------------------------------------------------------ 动图


def _gcd(a: int, b: int) -> int:
    while b:
        a, b = b, a % b
    return a


def _lcm_capped(a: int, b: int, cap: int) -> int:
    if a <= 0 or b <= 0:
        return max(a, b, 1)
    value = a // _gcd(a, b) * b
    return cap if value > cap else value


def _frame_at(delays: list[int], elapsed: int) -> int:
    cycle = sum(delays)
    if cycle <= 0:
        return 0
    elapsed %= cycle
    for index, delay in enumerate(delays):
        if elapsed < delay:
            return index
        elapsed -= delay
    return len(delays) - 1


def _collect_animations(card: CardLayout) -> list[dict]:
    placements: list[dict] = []

    def add(segment: SegmentLayout) -> None:
        if segment.animation and segment.width > 0 and segment.height > 0:
            placements.append(
                {
                    "animation": segment.animation,
                    "kind": segment.kind,
                    "x": segment.x,
                    "y": segment.y,
                    "w": segment.width,
                    "h": segment.height,
                }
            )

    def walk_reply(reply: ReplyLayout | None) -> None:
        if reply is None:
            return
        walk_reply(reply.reply)
        for segment in reply.segments:
            add(segment)

    for row in card.rows:
        walk_reply(row.reply)
        for segment in row.segments:
            add(segment)
    return placements


def _build_timeline(
    placements: list[dict],
    max_frames: int = MAX_GIF_FRAMES,
    max_duration: int = MAX_GIF_DURATION,
) -> list[dict]:
    if not placements:
        return [{"state": [], "delay": 0}]
    period, tick = 1, 0
    for placement in placements:
        cycle = 0
        for _frame, delay in placement["animation"]:
            cycle += delay
            tick = delay if tick <= 0 else _gcd(tick, delay)
        period = _lcm_capped(period, cycle, max_duration)
    period = min(period, max_duration)
    if tick <= 0:
        tick = MIN_FRAME_DELAY
    if (period + tick - 1) // tick > max_frames:
        tick = (period + max_frames - 1) // max_frames

    moments: list[dict] = []
    for elapsed in range(0, period, tick):
        state = [
            _frame_at([delay for _frame, delay in placement["animation"]], elapsed)
            for placement in placements
        ]
        delay = min(tick, period - elapsed)
        if moments and moments[-1]["state"] == state:
            moments[-1]["delay"] += delay
        else:
            moments.append({"state": state, "delay": delay})
    return moments


def _prepare_frames(placements: list[dict]) -> None:
    for placement in placements:
        size = (
            max(1, int(round(_s(placement["w"])))),
            max(1, int(round(_s(placement["h"])))),
        )
        frames = []
        for frame, _delay in placement["animation"]:
            img = frame.convert("RGBA").resize(size, Image.LANCZOS)
            if placement["kind"] != "emoji" and ImageChops is not None:
                mask = Image.new("L", size, 0)
                ImageDraw.Draw(mask).rounded_rectangle(
                    (0, 0, size[0] - 1, size[1] - 1),
                    radius=max(1, int(_s(IMAGE_RADIUS))),
                    fill=255,
                )
                img.putalpha(ImageChops.multiply(img.getchannel("A"), mask))
            frames.append(img)
        placement["frames"] = frames
        placement["pos"] = (
            int(round(_s(placement["x"]))),
            int(round(_s(placement["y"]))),
        )


def render_gif_sync(base_canvas: Any, placements: list[dict], out_path: str) -> bool:
    """把动图段逐帧贴回卡片存成 GIF，没有多帧就返回 False。"""
    if Image is None or not placements:
        return False
    moments = _build_timeline(placements)
    if len(moments) <= 1:
        return False
    _prepare_frames(placements)

    frames = []
    delays = []
    for moment in moments:
        frame = base_canvas.copy()
        for placement, index in zip(placements, moment["state"]):
            images = placement["frames"]
            if not images:
                continue
            img = images[index % len(images)]
            frame.paste(img, placement["pos"], img)
        frames.append(frame.convert("RGB"))
        delays.append(max(20, int(moment["delay"]) * 10))

    if len(frames) < 2:
        return False
    frames[0].save(
        out_path,
        save_all=True,
        append_images=frames[1:],
        duration=delays,
        loop=0,
        disposal=2,
        optimize=True,
    )
    return True


async def render_quote_card(
    messages: Sequence[QuoteMessage],
    *,
    out_dir: str,
    filename: str = "quote",
    font_path: str = "",
    proxy: str = "",
    avatar_dir: str = "",
    animated: bool = True,
) -> str:
    """渲染引用图，返回文件路径（有动图就是 .gif，否则 .png）。"""
    if Image is None:
        raise RuntimeError("缺少 Pillow 依赖，无法生成引用图")

    resolved_font = resolve_font_path(font_path)
    if not resolved_font:
        logger.warning("[reply_guard] 没找到中文字体，引用图可能显示异常")

    loader = ResourceLoader(proxy=proxy, cache_dir=avatar_dir)
    prepared = await prepare_messages(loader, messages)
    engine = LayoutEngine(FontManager(resolved_font))
    card = engine.layout(prepared)

    placements = _collect_animations(card) if animated else []
    canvas = render_card_sync(card, resolved_font, skip_animation=bool(placements))

    os.makedirs(out_dir, exist_ok=True)
    if placements:
        gif_path = os.path.join(out_dir, f"{filename}.gif")
        try:
            if render_gif_sync(canvas, placements, gif_path):
                return gif_path
        except Exception as e:
            logger.warning(f"[reply_guard] 生成动图失败，退回静态图: {e}")

    png_path = os.path.join(out_dir, f"{filename}.png")
    canvas.convert("RGB").save(png_path, "PNG")
    return png_path
