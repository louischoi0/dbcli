"""What the Tab key does, decided without any UI dependency so it can be tested.

Order: 0) template call `@name args` -> completed query, `@na` -> template name; also inside
          a line (`select * from (@a 4` + Tab), from the `@` to the cursor
       1) expand `$abbrev`  2) accept the gray suggestion + a space  3) open the menu.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from .abbrev import Abbreviations
from .templates import NAME_PREFIX_RE, TemplateError, Templates
from .words import WordModel


@dataclass(frozen=True)
class TabAction:
    kind: Literal["expand", "accept", "complete", "error"]
    delete: int = 0  # characters to delete before the cursor
    insert: str = ""  # text to insert at the cursor
    message: str = ""  # for "error"


def _template_action(before: str, templates: Templates) -> TabAction | None:
    m = NAME_PREFIX_RE.fullmatch(before)
    if m:  # still typing the name: "@" / "@se" / "@a"
        prefix = m.group(1) or ""
        names = templates.names(prefix)
        if prefix in names:  # name is complete
            if templates.arity(prefix) == 0:
                return TabAction("expand", delete=len(before), insert=templates.expand(before))
            return TabAction("accept", insert=" ")
        if len(names) == 1:
            return TabAction("accept", insert=names[0][len(prefix):] + " ")
        return TabAction("complete")  # menu lists the matching template names
    try:
        return TabAction("expand", delete=len(before), insert=templates.expand(before))
    except TemplateError as e:
        return TabAction("error", message=str(e))


def tab_action(before: str, after: str, abbrevs: Abbreviations, words: WordModel,
               templates: Templates | None = None) -> TabAction:
    if templates is not None and not after.strip() and before.lstrip().startswith("@") \
            and "\n" not in before:
        act = _template_action(before, templates)
        if act:
            return act
    if templates is not None:
        seg = templates.call_at(before, after)
        if seg is not None:
            act = _template_action(seg, templates)
            if act.kind != "error":  # inside a line a bad call may be plain SQL: Tab as usual
                return act
    exp = abbrevs.match(before)
    if exp:
        return TabAction("expand", delete=len(exp.token), insert=exp.body)
    s = words.suggest(before, after)
    if s:
        prefix, word = s
        space = "" if after[:1].isspace() else " "
        if word.startswith(prefix):
            return TabAction("accept", insert=word[len(prefix):] + space)
        # typed "my", stored "MyTable": rewrite the prefix too
        return TabAction("accept", delete=len(prefix), insert=word + space)
    return TabAction("complete")
