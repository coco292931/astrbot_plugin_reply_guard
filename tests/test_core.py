"""核心逻辑的最小自测：关键词表解析 + 引用图渲染。"""

import asyncio
import os
import sys

PLUGIN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PLUGIN_DIR not in sys.path:
    sys.path.insert(0, PLUGIN_DIR)

from core.keyword_store import KeywordStore  # noqa: E402
from core.quote_render import (  # noqa: E402
    MessageSegment,
    QuoteMessage,
    ReplyMessage,
    render_quote_card,
)


def test_keyword_store(xlsx_path: str) -> None:
    store = KeywordStore(xlsx_path, reload_interval=0)
    assert store.load(force=True), f"加载失败: {store.error}"
    print(f"[keyword] 有效 {store.count} 条 / 共 {store.rows} 行，跳过 {store.skipped} 行")

    assert "秣菏" not in store.snapshot(), "空内容行没被跳过"
    assert "余灰" not in store.snapshot(), "空内容行没被跳过"

    assert store.match("海棠") == "真棒", store.match("海棠")
    assert store.match("  海棠  ") == "真棒", "首尾空白应被忽略"
    assert store.match("海棠花") is None, "精准匹配不该命中前缀"
    assert store.match("") is None

    import openpyxl
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "kw.xlsx")
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.append(["关键词", "回复"])
        ws.append(["aaa", "第一版"])
        ws.append(["bbb", None])
        ws.append(["aaa", "第二版"])
        wb.save(path)

        s3 = KeywordStore(path, reload_interval=0, skip_header=True)
        assert s3.load(force=True)
        assert s3.count == 1, f"重复项未合并: {s3.snapshot()}"
        assert s3.match("aaa") == "第二版", "重复项应保留靠下的一行"
        assert s3.skipped == 1, f"空行统计不对: {s3.skipped}"

    print("[keyword] 全部通过")


async def test_quote_render(font_path: str, out_path: str) -> None:
    import base64
    import io

    from PIL import Image as PILImage

    frames = [
        PILImage.new("RGB", (80, 80), (255, 96, 96)),
        PILImage.new("RGB", (80, 80), (96, 160, 255)),
    ]
    buf = io.BytesIO()
    frames[0].save(
        buf, format="GIF", save_all=True, append_images=frames[1:], duration=200, loop=0
    )
    gif_uri = "data:image/gif;base64," + base64.b64encode(buf.getvalue()).decode()

    reply = ReplyMessage(
        nickname="被引用的某人",
        segments=[MessageSegment(type="text", text="这是被引用的那条消息")],
    )
    messages = [
        QuoteMessage(
            user_id="2111565284",
            nickname="coco",
            segments=[
                MessageSegment(
                    type="text", text="测试一下原版样式的引用图，看看换行和气泡对不对。"
                )
            ],
            reply=reply,
        ),
        QuoteMessage(
            user_id="10001",
            nickname="koko",
            segments=[
                MessageSegment(type="text", text="第二条消息，带个动图："),
                MessageSegment(type="image", kind="sticker", url=gif_uri),
            ],
        ),
    ]
    path = await render_quote_card(
        messages,
        out_dir=os.path.dirname(out_path),
        filename="quote_test",
        font_path=font_path,
    )
    assert os.path.isfile(path), "引用图没生成"
    from PIL import Image

    with Image.open(path) as img:
        print(
            f"[quote] 生成成功 {path} {img.size} "
            f"{os.path.getsize(path)} bytes frames={getattr(img, 'n_frames', 1)}"
        )


def main() -> None:
    xlsx = sys.argv[1] if len(sys.argv) > 1 else ""
    font = sys.argv[2] if len(sys.argv) > 2 else ""
    out = os.path.join(PLUGIN_DIR, "cache_test", "quote_test.png")

    if xlsx:
        test_keyword_store(xlsx)
    asyncio.run(test_quote_render(font, out))
    print("[all] 通过")


if __name__ == "__main__":
    main()
