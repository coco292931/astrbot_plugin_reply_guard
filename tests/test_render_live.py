"""端到端渲染自测：用真实群数据拼一张引用图，检查图片有没有真的下下来。

跑法：NAPCAT_HTTP=http://napcat:5700 python3 tests/test_render_live.py [群号] [被引用消息id] [条数]
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_live  # noqa: E402

from core.quote_render import render_quote_card  # noqa: E402


async def main():
    group_id = sys.argv[1] if len(sys.argv) > 1 else "738341962"
    message_id = sys.argv[2] if len(sys.argv) > 2 else ""
    count = int(sys.argv[3]) if len(sys.argv) > 3 else 3

    if not message_id:
        client = test_live.HttpClient()
        data = await client.call_action(
            "get_group_msg_history", group_id=int(group_id), count=6
        )
        msgs = (data or {}).get("messages") or []
        for m in reversed(msgs):
            if "image" in str(m.get("raw_message")):
                message_id = str(m.get("message_id"))
                break
        message_id = message_id or str(msgs[-1].get("message_id"))
        print("挑中的被引用消息:", message_id)

    messages = await test_live.run_case(group_id, message_id, count)
    out = os.path.join(test_live.PLUGIN_DIR, "cache_test", "live_render")
    path = await render_quote_card(
        messages,
        out_dir=out,
        filename=f"live_{message_id}",
        font_path="/AstrBot/data/koko/fonts/SimHei.ttf",
    )
    size = os.path.getsize(path)
    print(f"[render] {path} {size} bytes")
    # 图片段是否真的贴上了：卡片里含图片时体积会明显更大
    has_image = any(s.type == "image" for m in messages for s in m.segments)
    print("含图片段:", has_image, "| 体积:", size)


if __name__ == "__main__":
    asyncio.run(main())
