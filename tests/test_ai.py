import asyncio
import unittest

from dbcli.ai import Assistant, Chat, catalog_text, last_sql, system_prompt, user_message
from dbcli.config import AISettings
from dbcli.ui import answer_lines, wrap_text


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


if __name__ == "__main__":
    unittest.main()
