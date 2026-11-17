"""Read-only command classification; the caller owns scope, clearing and stopping.

No AstrBot imports, event mutation, I/O, sending, or model calls belong here.
Only a command at the start, optionally after addressing prefixes, is matched;
quoted or sentence-internal /help and ordinary URLs remain conversation text.
"""
from __future__ import annotations

import re


__all__ = ("is_command_text", "should_ignore_command")

_COMMAND_FILTER_NAMES = frozenset(("CommandFilter", "CommandGroupFilter"))
_CONTROLS = re.compile(r"^鲸鱼(?:记忆|风格)(?:\s|$)")
_ADDRESS_SEPARATORS = re.compile(r"^[\s,，:：、]+")
_CQ_AT = re.compile(r"^\[CQ:at,qq=[^\]\r\n]+\]")
_TEXT_AT = re.compile(r"^[@＠][^\s/／@＠,，:：、。！？!?\[\]<>\"'`“”‘’]+")
# Protocol-relative URLs also start with '/', but are not slash commands.
_PROTOCOL_RELATIVE_URL = re.compile(r"^[/／]{2}[^\s/／]+(?:[/／]|$)")


def _alias_names(aliases):
    if isinstance(aliases, str):
        aliases = (aliases,)
    return tuple(sorted({name.strip() for name in (aliases or ())
                         if isinstance(name, str) and name.strip()},
                        key=lambda name: (-len(name), name)))


def is_command_text(text, aliases=()) -> bool:
    """Classify an initial slash/fullwidth-slash or whale management command.

    Leading configured aliases, textual @mentions and CQ at segments may precede
    the command. Do not search later in a sentence or unwrap quotations. A bare
    slash is conservatively treated as an empty command prefix. Slash commands
    need not be known to any command registry.
    """
    if not isinstance(text, str):
        return False
    value = text.strip()
    names = _alias_names(aliases)
    while value:
        if _PROTOCOL_RELATIVE_URL.match(value):
            return False
        if value.startswith(("/", "／")) or _CONTROLS.match(value):
            return True
        mention = _CQ_AT.match(value) or _TEXT_AT.match(value)
        if mention:
            value = value[mention.end():]
        else:
            name = next((name for name in names if value.startswith(name)), None)
            if name is None:
                return False
            value = value[len(name):]
        value = _ADDRESS_SEPARATORS.sub("", value)
    return False


def should_ignore_command(event, aliases=()) -> bool:
    """Classify both original and wake-stripped text, then activated filters.

    Native waking can remove '/', so activated CommandFilter/CommandGroupFilter
    metadata is authoritative, including subclasses and this plugin's handlers.
    A wake/@ flag by itself never makes plain 'help' into a command. Scope,
    ignore_commands, night and self-echo policy remain the main plugin's job.
    """
    names = _alias_names(aliases)
    original = getattr(getattr(event, "message_obj", None), "message_str", None)
    if (is_command_text(original, names)
            or is_command_text(getattr(event, "message_str", None), names)):
        return True
    get_extra = getattr(event, "get_extra", None)
    if not callable(get_extra):
        return False
    handlers = get_extra("activated_handlers", []) or []
    for handler in handlers:
        for item in getattr(handler, "event_filters", ()) or ():
            if any(cls.__name__ in _COMMAND_FILTER_NAMES for cls in type(item).__mro__):
                return True
    return False
