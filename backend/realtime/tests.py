"""realtime 测试：SSE chunk 映射逻辑（mock astream，不跑真 agent）。"""
import asyncio
import json

from django.test import TransactionTestCase

from realtime.sse import _handle_messages, _handle_updates, _sse, map_astream_to_sse


class SseFrameTest(TransactionTestCase):
    def test_sse_frame_format(self):
        frame = _sse("step", {"node": "planner"})
        self.assertIn(b"event: step\n", frame)
        self.assertIn(b'data: {"node": "planner"}\n', frame)
        self.assertTrue(frame.endswith(b"\n\n"))

    def test_sse_unicode(self):
        frame = _sse("token", {"text": "中文"})
        self.assertIn("中文".encode(), frame)


class HandleUpdatesTest(TransactionTestCase):
    def test_planner_update(self):
        data = {"planner": {"plan": ["q1", "q2"]}}
        frames = asyncio.run(_collect(_handle_updates(data)))
        self.assertEqual(len(frames), 1)
        payload = _parse_event(frames[0])
        self.assertEqual(payload["event"], "step")
        self.assertEqual(payload["data"]["node"], "planner")
        self.assertEqual(payload["data"]["plan"], ["q1", "q2"])

    def test_citation_graph_update_emits_graph(self):
        vis = {"nodes": [{"id": 1}], "edges": []}
        data = {"citation_graph": {"citation_graph": {"vis": vis}}}
        frames = asyncio.run(_collect(_handle_updates(data)))
        events = [_parse_event(f)["event"] for f in frames]
        self.assertIn("graph", events)
        self.assertIn("step", events)

    def test_synthesizer_update(self):
        data = {"synthesizer": {"final_report": "# R"}}
        frames = asyncio.run(_collect(_handle_updates(data)))
        payload = _parse_event(frames[0])
        self.assertEqual(payload["event"], "step")
        self.assertTrue(payload["data"]["done"])

    def test_fan_out_update(self):
        data = {"fan_out_researchers": {"notes": ["n"], "sources": [{"t": 1}]}}
        frames = asyncio.run(_collect(_handle_updates(data)))
        payload = _parse_event(frames[0])
        self.assertEqual(payload["data"]["sources"], 1)
        self.assertEqual(payload["data"]["notes"], 1)


class HandleMessagesTest(TransactionTestCase):
    def test_token_from_string_content(self):
        class FakeMsg:
            content = "你好"
        frames = asyncio.run(_collect(_handle_messages(FakeMsg())))
        payload = _parse_event(frames[0])
        self.assertEqual(payload["event"], "token")
        self.assertEqual(payload["data"]["text"], "你好")

    def test_token_from_list_content(self):
        class FakeMsg:
            content = [{"type": "text", "text": "a"}, {"type": "text", "text": "b"}]
        frames = asyncio.run(_collect(_handle_messages(FakeMsg())))
        payload = _parse_event(frames[0])
        self.assertEqual(payload["data"]["text"], "ab")

    def test_tuple_msg_meta(self):
        class FakeMsg:
            content = "tok"
        frames = asyncio.run(_collect(_handle_messages((FakeMsg(), {"langgraph_node": "x"}))))
        self.assertEqual(len(frames), 1)

    def test_empty_content_no_frame(self):
        class FakeMsg:
            content = ""
        frames = asyncio.run(_collect(_handle_messages(FakeMsg())))
        self.assertEqual(len(frames), 0)


class MapAstreamTest(TransactionTestCase):
    def test_mix_updates_messages(self):
        async def fake_astream():
            yield ("updates", {"planner": {"plan": ["q"]}})
            yield ("messages", (type("M", (), {"content": "token1"})(), {}))

        frames = asyncio.run(_collect(map_astream_to_sse(fake_astream())))
        events = [_parse_event(f)["event"] for f in frames]
        self.assertIn("step", events)
        self.assertIn("token", events)


async def _collect(async_gen):
    out = []
    async for f in async_gen:
        out.append(f)
    return out


def _parse_event(frame: bytes) -> dict:
    text = frame.decode()
    lines = text.strip().split("\n")
    event = lines[0].split(": ", 1)[1]
    data = json.loads(lines[1].split("data: ", 1)[1])
    return {"event": event, "data": data}
