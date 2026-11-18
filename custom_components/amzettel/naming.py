"""Display names that tell shared objects apart: several shared lists are often called the same.

`OwnerName` is a read field of the backend, set only when the caller is NOT the owner (the owner's
name, else e-mail). Without it, the caller's role decides: a list where I am not `owner`, a meal plan
whose `OwnerId` is not me, is marked as shared. Without my own id nothing is added. Only the display
name changes; entity ids stay as they were registered.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

_WORDS = {"de": ("von", "geteilt"), "en": ("from", "shared")}


def _language(language: str | None) -> str:
    return "de" if (language or "").lower().startswith("de") else "en"


def _same(a: Any, b: Any) -> bool:
    return a is not None and b is not None and str(a).lower() == str(b).lower()


def is_shared_with_me(source: Mapping[str, Any], user_id: str | None) -> bool:
    """Am I a member but not the owner? (lists: `Members[].Role`; meal plans: `OwnerId`)."""
    if isinstance(source.get("IsOwner"), bool):
        return not source["IsOwner"]
    if not user_id:
        return False
    if "OwnerId" in source:
        return not _same(source.get("OwnerId"), user_id)
    for member in source.get("Members") or []:
        if _same(member.get("UserId"), user_id):
            return (member.get("Role") or "").lower() != "owner"
    return False


def display_name(name: str, source: Mapping[str, Any], user_id: str | None, language: str | None) -> str:
    """ "Wocheneinkauf (von Anna)" / "Wocheneinkauf (geteilt)" / "Wocheneinkauf"."""
    by, shared = _WORDS[_language(language)]
    owner = (source.get("OwnerName") or "").strip()
    if owner:
        return f"{name} ({by} {owner})"
    if is_shared_with_me(source, user_id):
        return f"{name} ({shared})"
    return name
