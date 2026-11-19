"""The AI tab: questions about SQL to Claude, with the session's context, answers streamed.

What is sent with each question: the database type, the catalog Ctrl-O collected (table and
column names), the focused cell's SQL and its last error or result title. Never result rows.
The catalog goes in the system prompt, sorted, so it stays a cached prefix between questions.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable

from .config import AISettings

CATALOG_LIMIT = 60_000  # characters of `table(col, …)` lines in the system prompt
CONTEXT_LIMIT = 4_000  # characters of the focused cell's SQL / error
FENCE_RE = re.compile(r"```[ \t]*([\w+-]*)[ \t]*\n(.*?)(?:\n```|\Z)", re.S)

SYSTEM = """You are the SQL assistant inside dbcli, a terminal SQL client. The user is connected to a {db} database.

Answer briefly and concretely. Put every SQL statement you suggest in a ```sql fenced block, written for {db}; the user can insert the last block into their input with one key, so make that block the one to run. Do not wrap the answer in headings. When the question is about the user's current SQL or its error, it is in <context> below the question.

{catalog}"""


def catalog_text(columns: dict, relations: list) -> str:
    """The catalog as `table(col, …)` lines, sorted (a stable prompt prefix), capped."""
    if not columns and not relations:
        return "No catalog was collected yet (Ctrl-O in the Schema tab collects it), so table and column names are unknown: ask, or say what you assume."
    names = sorted({t.lower() for t in columns} | {n.lower() for n, _ in relations})
    lines, used = [], 0
    for t in names:
        cols = columns.get(t)
        line = f"{t}({', '.join(sorted(cols))})" if cols else t
        if used + len(line) > CATALOG_LIMIT:
            lines.append(f"… and {len(names) - len(lines)} more tables not listed")
            break
        lines.append(line)
        used += len(line) + 1
    return "Tables and views of the connected schema, with their columns:\n" + "\n".join(lines)


def system_prompt(db_type: str, columns: dict, relations: list) -> str:
    label = {"oracle": "Oracle", "postgres": "PostgreSQL", "sqlite": "SQLite"}.get(db_type, db_type)
    return SYSTEM.format(db=label, catalog=catalog_text(columns, relations))


def user_message(question: str, sql: str = "", outcome: str = "") -> str:
    """The question, then what the user is working on (cut to CONTEXT_LIMIT)."""
    parts = []
    if sql.strip():
        parts.append(f"Current SQL in the focused cell:\n{sql.strip()[:CONTEXT_LIMIT]}")
    if outcome.strip():
        parts.append(f"Its last run:\n{outcome.strip()[:CONTEXT_LIMIT]}")
    if not parts:
        return question
    return f"{question}\n\n<context>\n" + "\n\n".join(parts) + "\n</context>"


def last_sql(answer: str) -> str | None:
    """The last ```sql (or unlabeled) fenced block of an answer: what `i` inserts."""
    blocks = [body.strip() for lang, body in FENCE_RE.findall(answer) if lang.lower() in ("", "sql", "plsql", "pgsql")]
    blocks = [b for b in blocks if b]
    return blocks[-1] if blocks else None


@dataclass
class Turn:
    question: str
    answer: str = ""
    error: str = ""
    done: bool = False


@dataclass
class Chat:
    """One conversation: the turns shown, and the messages sent (assistant turns as the API
    returned them, so thinking blocks go back unchanged)."""
    turns: list[Turn] = field(default_factory=list)
    messages: list = field(default_factory=list)
    busy: bool = False


class Assistant:
    def __init__(self, settings: AISettings):
        self.settings = settings
        self._client = None

    def client(self):
        if self._client is None:
            try:
                import anthropic
            except ModuleNotFoundError:
                raise RuntimeError("the AI tab needs the Anthropic SDK: .venv/bin/pip install anthropic") from None
            self._client = anthropic.AsyncAnthropic(api_key=self.settings.api_key) if self.settings.api_key \
                else anthropic.AsyncAnthropic()
        return self._client

    async def ask(self, chat: Chat, system: str, content: str, on_text: Callable[[str], None]) -> None:
        """Send `content` as the next user turn and stream the answer into `on_text`. On success
        the turn is kept in `chat.messages`; on a failure or refusal it is taken back out, so
        the conversation stays valid for the next question."""
        client = self.client()
        chat.messages.append({"role": "user", "content": content})
        try:
            async with client.beta.messages.stream(
                model=self.settings.model,
                max_tokens=64000,
                output_config={"effort": self.settings.effort},
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",  # a declined question is re-run on Anthropic's fallback model
                cache_control={"type": "ephemeral"},
                system=system,
                messages=chat.messages,
            ) as stream:
                async for text in stream.text_stream:
                    on_text(text)
                final = await stream.get_final_message()
        except BaseException:
            chat.messages.pop()
            raise
        if final.stop_reason == "refusal":
            chat.messages.pop()
            raise RuntimeError("the model declined to answer this question")
        chat.messages.append({"role": "assistant", "content": final.content})


def error_message(e: BaseException) -> str:
    """A short line for an SDK error, without guessing at its classes when it is missing."""
    try:
        import anthropic
    except ModuleNotFoundError:
        return str(e)
    if isinstance(e, anthropic.AuthenticationError):
        return "the key was refused: ./dbcli subscription add (a new key) or set (another one)"
    if isinstance(e, anthropic.PermissionDeniedError):
        return "this API key may not use that model"
    if isinstance(e, anthropic.NotFoundError):
        return "unknown model: check [ai] model in config.toml"
    if isinstance(e, anthropic.RateLimitError):
        return "rate limited: try again in a moment"
    if isinstance(e, anthropic.APIStatusError):
        return f"API error {e.status_code}: {e.message}"
    if isinstance(e, anthropic.APIConnectionError):
        return "cannot reach the Anthropic API (network?)"
    return str(e)

