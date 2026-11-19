import asyncio
import unittest

from dbcli.ai import KEEP_TURNS, Assistant, Chat, Turn, catalog_text, cell_refs, last_sql, system_prompt, user_message
from dbcli.config import AISettings
from dbcli.ui import answer_lines, marked_refs, wrap_text


class PromptTest(unittest.TestCase):
    def test_catalog_is_sorted_and_stable(self):
        cols = {"emp": frozenset({"name", "id"}), "dept": frozenset({"id"})}
        text = catalog_text(cols, [("EMP", "table"), ("v_emp", "view")])
        self.assertIn("dept(id)\nemp(id, name)\nv_emp", text)
        self.assertEqual(text, catalog_text(dict(reversed(list(cols.items()))), [("v_emp", "view"), ("EMP", "table")]))

    def test_no_catalog_says_so(self):
        self.assertIn("Ctrl-O", catalog_text({}, []))

    def test_system_names_the_database(self):
        self.assertIn("Oracle database", system_prompt("oracle", {}, []))

    def test_user_message_carries_context_only_when_there_is_some(self):
        self.assertEqual(user_message("why?"), "why?")
        msg = user_message("why?", "select * form t", "ERROR: ORA-00923")
        self.assertIn("<context>", msg)
        self.assertIn("select * form t", msg)
        self.assertIn("ORA-00923", msg)

    def test_cell_refs(self):
        self.assertEqual(cell_refs("compare @c0 with @c2, then @c0 again"), [0, 2])
        self.assertEqual(cell_refs("mail a@c1 or @c1x or email@c3"), [])  # not a reference

    def test_named_cells_replace_the_focused_one(self):
        msg = user_message("why does @c1 fail?", "select 9", "", cells=[(1, "select * form t", "ERROR: ORA-00923")])
        self.assertIn("Cell #c1 (the question's @c1):\nselect * form t\nIts last run:\nERROR: ORA-00923", msg)
        self.assertNotIn("select 9", msg)  # the focused cell is not sent then
        self.assertIn("(empty)", user_message("@c0?", cells=[(0, "  ", "")]))

    def test_refs_are_colored(self):
        self.assertEqual(marked_refs("fix @c0 now", "b"), [("b", "fix "), ("class:ai.ref", "@c0"), ("b", " now")])
        self.assertEqual(marked_refs("", "b"), [("b", "")])


class AnswerTest(unittest.TestCase):
    def test_last_sql_block(self):
        answer = "Try:\n```sql\nselect 1 from dual\n```\nor\n```sql\nselect 2 from dual;\n```\n"
        self.assertEqual(last_sql(answer), "select 2 from dual;")
        self.assertEqual(last_sql("```\nselect 3\n```"), "select 3")  # unlabeled counts
        self.assertIsNone(last_sql("```python\nprint(1)\n```"))
        self.assertIsNone(last_sql("no code here"))

    def test_unclosed_block_while_streaming(self):
        self.assertEqual(last_sql("```sql\nselect 1"), "select 1")

    def test_wrap_text(self):
        self.assertEqual(wrap_text("aa bb cc", 5), ["aa bb", "cc"])
        self.assertEqual(wrap_text("a\n\nb", 10), ["a", "", "b"])
        self.assertEqual(wrap_text("abcdefgh", 3), ["abc", "def", "gh"])

    def test_answer_lines_mark_code(self):
        lines = answer_lines("Use this:\n```sql\nselect 1\n```", 40)
        self.assertEqual(lines[0], [("class:ai.text", "  Use this:")])
        self.assertEqual(lines[1][0], ("class:ai.code", "  │ "))
        self.assertEqual(len(lines), 2)  # the fences themselves are not drawn


class FakeStream:
    def __init__(self, parts, stop="end_turn"):
        self.parts, self.stop = parts, stop

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    @property
    def text_stream(self):
        async def gen():
            for p in self.parts:
                yield p
        return gen()

    async def get_final_message(self):
        return type("M", (), {"stop_reason": self.stop, "content": [{"type": "text", "text": "".join(self.parts)}]})()


class FakeClient:
    def __init__(self, stream):
        self.calls = []
        outer = self

        class Messages:
            def stream(self, **kw):
                outer.calls.append(kw)
                return stream
        self.beta = type("B", (), {"messages": Messages()})()


class AssistantTest(unittest.TestCase):
    def run_ask(self, stream):
        a = Assistant(AISettings())
        a._client = FakeClient(stream)
        chat, got = Chat(), []
        err = None
        try:
            asyncio.run(a.ask(chat, "sys", "q?", got.append))
        except RuntimeError as e:
            err = e
        return a._client, chat, got, err

    def test_streams_and_keeps_the_turn(self):
        client, chat, got, err = self.run_ask(FakeStream(["sel", "ect 1"]))
        self.assertIsNone(err)
        self.assertEqual(got, ["sel", "ect 1"])
        self.assertEqual([m["role"] for m in chat.messages], ["user", "assistant"])
        kw = client.calls[0]
        self.assertEqual((kw["model"], kw["fallbacks"]), ("claude-opus-5-5", "default"))
        self.assertEqual(kw["output_config"], {"effort": "medium"})

    def test_refusal_takes_the_question_back(self):
        _, chat, _, err = self.run_ask(FakeStream([], stop="refusal"))
        self.assertIn("declined", str(err))
        self.assertEqual(chat.messages, [])


    def test_history_goes_back_as_text_only(self):
        client, chat, _, _ = self.run_ask(FakeStream(["ok"]))
        self.assertEqual(chat.messages[-1], {"role": "assistant", "content": [{"type": "text", "text": "ok"}]})


class ChatHistoryTest(unittest.TestCase):
    def chat_of(self, n):
        chat = Chat()
        for i in range(n):
            chat.turns.append(Turn(f"q{i}", f"a{i}", done=True, sent=f"q{i} + context"))
        return chat

    def test_keeps_the_latest_ten(self):
        chat = self.chat_of(13)
        chat.trim()
        self.assertEqual(KEEP_TURNS, 10)
        self.assertEqual([t.question for t in chat.turns], [f"q{i}" for i in range(3, 13)])
        self.assertEqual(chat.messages[0], {"role": "user", "content": "q3 + context"})
        self.assertEqual(len(chat.messages), 20)

    def test_failed_turns_are_shown_not_sent(self):
        chat = self.chat_of(2)
        chat.turns.insert(1, Turn("bad", "half an ans", error="stopped", done=True, sent="bad"))
        chat.rebuild()
        self.assertEqual([m["content"] for m in chat.messages if m["role"] == "user"], ["q0 + context", "q1 + context"])
        self.assertEqual(len(chat.turns), 3)

    def test_round_trip_through_json(self):
        chat = self.chat_of(12)
        chat.turns.append(Turn("still running"))  # not done: not saved
        back = Chat.from_json(chat.to_json())
        self.assertEqual([t.question for t in back.turns], [f"q{i}" for i in range(2, 12)])
        self.assertEqual(back.messages[-1]["content"], [{"type": "text", "text": "a11"}])
        self.assertEqual(Chat.from_json("garbage").turns, [])


if __name__ == "__main__":
    unittest.main()
