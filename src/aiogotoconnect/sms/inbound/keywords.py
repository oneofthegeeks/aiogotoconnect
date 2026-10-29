"""STOP/START/HELP keyword detection and default replies (port of Node inbound/keywords.ts)."""

import re
from typing import TypedDict

from aiogotoconnect.sms.types import Keyword


class KeywordReplies(TypedDict):
    stop: str
    start: str
    help: str


DEFAULT_REPLIES: KeywordReplies = {
    "stop": "You're unsubscribed and won't get more texts from this number. "
    "Reply START to resubscribe.",
    "start": "You're resubscribed. Reply STOP to unsubscribe.",
    "help": "Reply STOP to unsubscribe. Msg & data rates may apply.",
}

_STOP_WORDS = frozenset({"STOP", "STOPALL", "UNSUBSCRIBE", "CANCEL", "END", "QUIT"})
_START_WORDS = frozenset({"START", "UNSTOP"})
_HELP_WORDS = frozenset({"HELP", "INFO"})
_TRAILING_PUNCTUATION = re.compile(r"[.!?,]+\Z")


def detect_keyword(text: str, yes_is_opt_in: bool) -> Keyword | None:
    """The keyword a text is, if any.

    Only a whole-message match counts ("Stop." and " stop " do; "Unsubscribe please" doesn't),
    so ordinary messages that mention a word aren't mistaken for an opt-out.
    """
    word = _TRAILING_PUNCTUATION.sub("", text.strip().upper())
    if word in _STOP_WORDS:
        return "STOP"
    if word in _START_WORDS or (yes_is_opt_in and word == "YES"):
        return "START"
    if word in _HELP_WORDS:
        return "HELP"
    return None


__all__ = ["DEFAULT_REPLIES", "KeywordReplies", "detect_keyword"]
