"""用真实数据跑一遍取消息的逻辑（不依赖 AstrBot 进程）。

数据源：NapCat 的 HTTP 接口。跑法：
python3 tests/test_live.py [群号] [被引用消息id] [条数]
"""

import asyncio
import json
import os
import sys
import types
import urllib.parse
import urllib.request

PLUGIN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PLUGIN_DIR not in sys.path:
    sys.path.insert(0, PLUGIN_DIR)

pkg = types.ModuleType("rgpkg")
pkg.__path__ = [PLUGIN_DIR]
sys.modules["rgpkg"] = pkg
import importlib  # noqa: E402

main = importlib.import_module("rgpkg.main")
ReplyGuardPlugin = main.ReplyGuardPlugin

NAPCAT = os.environ.get("NAPCAT_HTTP", "http://127.0.0.1:5700")


class HttpClient:
    """把 NapCat 的 HTTP 接口伪装成 AstrBot 的 bot.api。"""

    async def call_action(self, action: str, **params):
        loop = asyncio.get_running_loop()

        def _do():
            url = f"{NAPCAT}/{action}?" + urllib.parse.urlencode(params)
            with urllib.request.urlopen(url, timeout=20) as resp:
                text = resp.read().decode()
            try:
                payload = json.loads(text)
            except json.JSONDecodeError:
                return None
            return payload.get("data", payload)

        return await loop.run_in_executor(None, _do)


class FakeBot:
    def __init__(self):
        self.api = HttpClient()


class FakeEvent:
    def __init__(self, group_id: str, user_id: str = "3358148014"):
        self.bot = FakeBot()
        self._group = group_id
        self._user = user_id

    def get_group_id(self):
        return self._group

    def get_sender_id(self):
        return self._user

    def get_self_id(self):
        return self._user

    def get_messages(self):
        return []

    def plain_result(self, text):
        return ("plain", text)

    def chain_result(self, chain):
        return ("chain", chain)

    def should_call_llm(self, flag):
        pass


def make_plugin():
    plugin = ReplyGuardPlugin.__new__(ReplyGuardPlugin)
    plugin.enable_quote = True
    plugin.quote_scope = []
    plugin.quote_font_path = "/AstrBot/data/koko/fonts/SimHei.ttf"
    plugin.quote_proxy = ""
    plugin.cache_dir = os.path.join(PLUGIN_DIR, "cache_test")
    plugin.avatar_dir = ""
    plugin._at_name_cache = {}
    plugin._member_cache = {}
    plugin._last_window = []
    return plugin


def fake_reply_comp(message_id: str, nickname: str = "", chain=None):
    return types.SimpleNamespace(
        id=message_id,
        chain=chain or [],
        sender_id="",
        sender_nickname=nickname,
        time=0,
        message_str="",
    )


async def run_case(group_id: str, message_id: str, count: int):
    plugin = make_plugin()
    event = FakeEvent(group_id)
    comp = fake_reply_comp(message_id)
    messages = await plugin._build_quote_messages(event, comp, count)
    await plugin._resolve_at_names(event, messages)
    plugin._dump_quote_debug(messages)
    print(f"=== 引用 {message_id}，/q {count} -> {len(messages)} 条")
    for m in messages:
        text = "".join(f"[{s.type}:{s.kind}]{s.text or s.url or s.id}" for s in m.segments)
        nested = f" ⟵引用 {m.reply.nickname}" if m.reply else ""
        print(f"  {m.time} {m.nickname}({m.user_id}) id={m.message_id}: {text[:70]}{nested}")
    return messages


async def main():
    group_id = sys.argv[1] if len(sys.argv) > 1 else "738341962"
    message_id = sys.argv[2] if len(sys.argv) > 2 else ""
    count = int(sys.argv[3]) if len(sys.argv) > 3 else 5

    if not message_id:
        client = HttpClient()
        data = await client.call_action(
            "get_group_msg_history", group_id=int(group_id), count=5
        )
        msgs = (data or {}).get("messages") or []
        if not msgs:
            print("拿不到历史，先看群号/接口")
            return
        message_id = str(msgs[-1].get("message_id"))
        print("自动选中最后一条作为被引用消息:", message_id)

    await run_case(group_id, message_id, count)


if __name__ == "__main__":
    asyncio.run(main())
