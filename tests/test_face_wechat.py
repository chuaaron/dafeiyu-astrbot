# -*- coding: utf-8 -*-
"""face_convert 微信（weixin_oc）分叉的离线测试。

跑法（纯标准库，不需要 AstrBot 环境）：
    /opt/homebrew/bin/python3.11 -B -m unittest discover -s tests -p 'test_face_*.py'
原理与 test_face_rules.py 相同：用空壳模块桩掉 astrbot 依赖，直接加载插件源码。
覆盖：身份标记（主人/陌生）、wechat_mode_enabled=false 完全直通、
表情 token -> emoji 降级、未知 token 原样保留、文本卫生在微信路径生效、
QQ（aiocqhttp）路径回归（仍产出 Face 段、陌生人仍打「主人的朋友」标）。
"""
import asyncio
import importlib.util
import json
import logging
import os
import sys
import types
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MAIN = os.path.join(ROOT, "plugin", "astrbot_plugin_face_convert", "main.py")
WX_OWNER = "o9cq8077AWv9Q5urqfmASXuJyoCI@im.wechat"


class Plain:
    def __init__(self, text=""):
        self.text = text


class Face:
    def __init__(self, id=0, **kw):
        self.id = id


def _stub(name, **attrs):
    m = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(m, k, v)
    sys.modules[name] = m
    return m


class _Star:
    def __init__(self, *a, **k):
        pass


def _register(*a, **k):
    def deco(cls):
        return cls

    return deco


class _Filter:  # filter.xxx(...) 全都变成空装饰器
    EventMessageType = types.SimpleNamespace(ALL="ALL")

    def __getattr__(self, _):
        def deco(*a, **k):
            def wrap(fn):
                return fn

            return wrap

        return deco


logging.basicConfig(level=logging.CRITICAL)
_aiohttp_stub = _stub(
    "aiohttp",
    ClientSession=object,
    ClientTimeout=lambda *a, **k: None,
    ClientError=Exception,
)
_stub("astrbot", api=types.ModuleType("astrbot.api"))
_stub("astrbot.api", logger=logging.getLogger("t"))
_stub("astrbot.api.event", AstrMessageEvent=object, filter=_Filter())
_stub("astrbot.api.star", Context=object, Star=_Star, register=_register)
_stub("astrbot.core")
_stub("astrbot.core.message")
_stub(
    "astrbot.core.message.components",
    At=type("At", (), {}),
    Face=Face,
    Image=type("Image", (), {}),
    Plain=Plain,
    Record=type("Record", (), {}),
    Reply=type("Reply", (), {}),
    Video=type("Video", (), {}),
)
_stub("astrbot.core.star")
_stub("astrbot.core.star.session_llm_manager", SessionServiceManager=object)

spec = importlib.util.spec_from_file_location("face_wechat_plugin", MAIN)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
if sys.modules.get("aiohttp") is _aiohttp_stub:
    del sys.modules["aiohttp"]


class FakeEvent:
    """最小 AstrMessageEvent 桩（微信/QQ 两种形态）。"""

    def __init__(self, platform="weixin_oc", sender=WX_OWNER, name=None, text="你好"):
        self.platform_name = platform
        self._sender = sender
        self._name = name if name is not None else sender  # weixin 实测昵称=wxid
        self.message_str = text
        self._extras = {}
        self.unified_msg_origin = f"{platform}_x:FriendMessage:{sender}"

    def get_platform_name(self):
        return self.platform_name

    def get_platform_id(self):
        return "weixin_personal_xgsa"

    def get_sender_id(self):
        return self._sender

    def get_sender_name(self):
        return self._name

    def get_group_id(self):
        return ""

    def is_private_chat(self):
        return True

    def is_wake(self):
        return False

    def get_extra(self, key, default=None):
        return self._extras.get(key, default)

    def set_extra(self, key, value):
        self._extras[key] = value

    def get_messages(self):
        return [Plain(self.message_str)] if self.message_str else []


class FakeResult:
    def __init__(self, chain):
        self.chain = chain

    def is_llm_result(self):
        return True


class FakeSendEvent(FakeEvent):
    def __init__(self, result, **kw):
        super().__init__(**kw)
        self._result = result

    def get_result(self):
        return self._result


class Resp:
    def __init__(self, text):
        self.completion_text = text


def make_plugin(wechat_on, owner_map=None):
    cfg = {"wechat_mode_enabled": wechat_on}
    if owner_map is not None:
        cfg["wechat_owner_map"] = owner_map
    return mod.FaceConvertPlugin(None, config=cfg)


class TestFaceWechat(unittest.TestCase):
    def setUp(self):
        self.plugin = make_plugin(True, {WX_OWNER: "主人"})

    # ---- 配置合并 ----
    def test_astrbot_config_merge_and_defaults(self):
        self.assertTrue(self.plugin.cfg["wechat_mode_enabled"])
        self.assertEqual(self.plugin.wechat_owner_map, {WX_OWNER: "主人"})
        # 默认关：不传 config（QQ 旧部署）不受影响
        plain = mod.FaceConvertPlugin(None)
        self.assertFalse(plain.cfg["wechat_mode_enabled"])
        self.assertEqual(plain.wechat_owner_map, {})

    def test_wechat_face_map_keys_are_real_qq_faces(self):
        # 降级表里的每个 token 必须同时存在于 QQ 字典（防止幽灵条目）
        for name in self.plugin.wechat_face_map:
            self.assertIn(name, self.plugin.face_map, f"幽灵表情条目: {name}")

    # ---- 入站身份标记 ----
    def test_inbound_owner_tag(self):
        ev = FakeEvent(text="今天心情不错")
        asyncio.run(self.plugin.inbound_face_to_text(ev))
        self.assertTrue(ev.message_str.startswith(f"[主人({WX_OWNER})] "))
        self.assertTrue(ev.get_extra("_fy_tagged"))

    def test_inbound_unknown_sender_gets_friend_last4(self):
        ev = FakeEvent(sender="wxid_abc123xyZ@im.wechat", text="你是谁")
        asyncio.run(self.plugin.inbound_face_to_text(ev))
        self.assertTrue(ev.message_str.startswith("[朋友(3xyZ)] "), ev.message_str)

    def test_inbound_no_tag_when_mode_off(self):
        plugin = make_plugin(False, {WX_OWNER: "主人"})
        ev = FakeEvent(text="在吗")
        asyncio.run(plugin.inbound_face_to_text(ev))
        self.assertEqual(ev.message_str, "在吗")
        self.assertIsNone(ev.get_extra("_fy_tagged"))

    def test_inbound_qq_guest_tag_regression(self):
        # QQ 路径不受微信分支影响：陌生私聊仍打「主人的朋友」标
        plugin = make_plugin(True, {WX_OWNER: "主人"})
        ev = FakeEvent(platform="aiocqhttp", sender="123456", name="小明", text="hello")
        asyncio.run(plugin.inbound_face_to_text(ev))
        self.assertTrue(ev.message_str.startswith("[主人的朋友 小明(123456)] "), ev.message_str)

    # ---- 出站表情降级 ----
    def _sent_text(self, result):
        return "".join(c.text for c in result.chain if isinstance(c, Plain))

    def test_outbound_known_token_to_emoji(self):
        result = FakeResult([Plain("好呀 [旺柴] 明天见")])
        ev = FakeSendEvent(result)
        asyncio.run(self.plugin.convert_faces(ev))
        self.assertEqual(self._sent_text(result), "好呀 🐶 明天见")
        self.assertFalse(any(isinstance(c, Face) for c in result.chain))

    def test_outbound_unknown_token_untouched(self):
        result = FakeResult([Plain("先看 [项目计划] 再说")])
        asyncio.run(self.plugin.convert_faces(FakeSendEvent(result)))
        self.assertEqual(self._sent_text(result), "先看 [项目计划] 再说")

    def test_outbound_plain_strip_entry(self):
        result = FakeResult([Plain("有点无语 [无语] 了")])
        asyncio.run(self.plugin.convert_faces(FakeSendEvent(result)))
        self.assertEqual(self._sent_text(result), "有点无语 无语 了")

    def test_outbound_passthrough_when_mode_off(self):
        plugin = make_plugin(False, {WX_OWNER: "主人"})
        result = FakeResult([Plain("好呀 [旺柴] 明天见")])
        asyncio.run(plugin.convert_faces(FakeSendEvent(result)))
        # 直通：不换 emoji、也不产出微信渲染不了的 Face 段
        self.assertEqual(self._sent_text(result), "好呀 [旺柴] 明天见")
        self.assertFalse(any(isinstance(c, Face) for c in result.chain))

    def test_outbound_qq_face_segment_regression(self):
        # QQ 路径照旧产出真实 Face 段（id 277=旺柴）
        result = FakeResult([Plain("好呀 [旺柴]")])
        asyncio.run(self.plugin.convert_faces(FakeSendEvent(result, platform="aiocqhttp")))
        self.assertTrue(any(isinstance(c, Face) and c.id == 277 for c in result.chain))

    # ---- 文本卫生在微信路径同样生效 ----
    def test_hygiene_scrub_drafts_on_wechat(self):
        resp = Resp("隐约飘进来一句：你好")
        asyncio.run(self.plugin.scrub_drafts(FakeEvent(), resp))
        self.assertEqual(resp.completion_text, "你好")

    def test_hygiene_punct_and_unwrap_on_wechat(self):
        resp = Resp("「真通了」、也能把那条视频点开。")
        asyncio.run(self.plugin.scrub_drafts(FakeEvent(), resp))
        self.assertEqual(resp.completion_text, "“真通了”，也能把那条视频点开。")
        # 结果关口的消息数组拆壳在微信路径也执行
        result = FakeResult([Plain("['第一句。', '第二句。']")])
        asyncio.run(self.plugin.convert_faces(FakeSendEvent(result)))
        self.assertEqual(self._sent_text(result), "第一句。\n第二句。")

    def test_no_reply_still_swallowed_on_wechat(self):
        result = FakeResult([Plain("[NO_REPLY]")])
        asyncio.run(self.plugin.convert_faces(FakeSendEvent(result)))
        self.assertEqual(result.chain, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
