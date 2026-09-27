"""触发路径自测：各种消息组合下 /q 能不能认出来、认成几条。

跑法：python3 tests/test_trigger.py
"""

import asyncio
import os
import sys
import types

PLUGIN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PLUGIN_DIR not in sys.path:
    sys.path.insert(0, PLUGIN_DIR)

from astrbot.api.message_components import At, Image, Plain, Reply  # noqa: E402

pkg = types.ModuleType("rgpkg")
pkg.__path__ = [PLUGIN_DIR]
sys.modules["rgpkg"] = pkg
import importlib  # noqa: E402

main = importlib.import_module("rgpkg.main")
ReplyGuardPlugin = main.ReplyGuardPlugin

CALLS = []


async def _stub_quote_results(self, event, count):
    CALLS.append(count)
    return
    yield  # pragma: no cover


class FakeEvent:
    def __init__(self, comps, message_str, sender="2111565284", self_id="3358148014"):
        self.comps = comps
        self.message_str = message_str
        self._sender = sender
        self._self = self_id
        self.called_llm = None

    def get_messages(self):
        return self.comps

    def get_sender_id(self):
        return self._sender

    def get_self_id(self):
        return self._self

    def get_group_id(self):
        return "738341962"

    def get_message_type(self):
        return "GROUP_MESSAGE"

    unified_msg_origin = "test:GroupMessage:738341962"

    def should_call_llm(self, flag):
        self.called_llm = flag

    def plain_result(self, text):
        return ("plain", text)


def make_plugin():
    plugin = types.SimpleNamespace()
    plugin.enable_quote = True
    plugin.quote_scope = []
    plugin.enable_keyword_reply = False
    plugin.enable_probability = False
    plugin.probability_scope = []
    plugin.probability = 0.0
    plugin.probability_mode = "group_only"
    plugin.store = types.SimpleNamespace(match=lambda text: None, reload_if_needed=lambda: None)
    plugin._in_scope = lambda event, scope: ReplyGuardPlugin._in_scope(event, scope)
    plugin._typed_text = lambda event: ReplyGuardPlugin._typed_text(event)
    plugin._command_candidate = types.MethodType(
        ReplyGuardPlugin._command_candidate, plugin
    )
    plugin._quote_results = types.MethodType(_stub_quote_results, plugin)
    plugin._dump_quote_debug = lambda messages: None
    plugin._resolve_at_names = lambda event, messages: asyncio.sleep(0)
    return plugin


CASES = [
    ("纯文本 /q 2", [Plain("/q 2")], "/q 2", 2),
    ("@别人 + /q 2", [At(qq="2111565284"), Plain(" /q 2")], "/q 2", 2),
    ("引用图片 + /q 2", [Reply(id="1"), Image(file="/tmp/x.png"), Plain(" /q 2")], "[图片] /q 2", 2),
    ("引用消息 + /q", [Reply(id="1"), Plain("/q")], "/q", 1),
    ("q2 没空格", [Plain("q2")], "q2", 2),
    ("@自己 + /q 3", [At(qq="3358148014"), Plain(" /q 3")], " /q 3", 3),
]


async def main_async():
    ok = True
    for label, comps, message_str, expect in CASES:
        CALLS.clear()
        plugin = make_plugin()
        event = FakeEvent(comps, message_str)
        async for _ in ReplyGuardPlugin.on_message(plugin, event):
            pass
        got = CALLS[0] if CALLS else None
        flag = "✓" if got == expect else "✗"
        if got != expect:
            ok = False
        print(f"{flag} {label:>16}  期望 {expect} 实际 {got}")
    print("结果:", "全部通过" if ok else "有失败")
    return ok


if __name__ == "__main__":
    sys.exit(0 if asyncio.run(main_async()) else 1)
