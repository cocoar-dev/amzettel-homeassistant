"""Which lists, calendars and meal plans appear in Home Assistant (entry.options).

Stored: the chosen ids per kind, `auto_add_new`, `known` = every id ever offered when the choice was
saved, and `category_calendars` = the event categories that get a calendar of their own
(`<listId>:<categoryId>`). An object is visible when it was chosen, or when it is new (not in
`known`) and `auto_add_new` is on. An entry without stored options (set up before the choice
existed) shows everything and splits no category.

Saving MERGES: what was not offered this time (an area answered 403, a list was deleted meanwhile)
keeps its previous status, so a temporary gap never forgets a choice.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

import voluptuous as vol
from homeassistant.helpers.selector import (
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)

from .const import LIST_TYPE_EVENTS, LIST_TYPE_SHOPPING, TODO_LIST_TYPES
from .naming import display_name

KIND_LISTS = "lists"
KIND_CALENDARS = "calendars"
KIND_MEAL_PLANS = "meal_plans"
KINDS = (KIND_LISTS, KIND_CALENDARS, KIND_MEAL_PLANS)

OPT_AUTO_ADD = "auto_add_new"
OPT_KNOWN = "known"
OPT_CATEGORIES = "category_calendars"

# The scope prefix that lets the token read a kind.
_KIND_SCOPE = {KIND_LISTS: "lists.", KIND_CALENDARS: "calendar.", KIND_MEAL_PLANS: "mealplans."}


def category_key(list_id: str, category_id: str) -> str:
    """How a split category is stored."""
    return f"{list_id}:{category_id}"


def named_categories(view: Mapping[str, Any]) -> dict[str, str]:
    """Category id → name of an event list, only categories WITH a name (EventCategories[].Name)."""
    result: dict[str, str] = {}
    for category in view.get("EventCategories") or []:
        name = (category.get("Name") or "").strip()
        if name and category.get("Id"):
            result[str(category["Id"])] = name
    return result


class Visibility:
    """The visibility rule over `entry.options`."""

    def __init__(self, options: Mapping[str, Any] | None) -> None:
        options = options or {}
        self._configured = OPT_KNOWN in options
        self._chosen = {kind: set(options.get(kind) or []) for kind in KINDS}
        self._known = set(options.get(OPT_KNOWN) or [])
        self._auto_add = bool(options.get(OPT_AUTO_ADD, True))
        self._split = set(options.get(OPT_CATEGORIES) or [])

    def is_visible(self, kind: str, object_id: str) -> bool:
        """Chosen, or new while new objects are taken over (everything without stored options)."""
        if not self._configured:
            return True
        if object_id in self._chosen.get(kind, set()):
            return True
        return object_id not in self._known and self._auto_add

    def is_split(self, list_id: str, category_id: str) -> bool:
        """Does this category of this calendar get a calendar of its own?"""
        return category_key(list_id, category_id) in self._split

    def split_categories(self, view: Mapping[str, Any]) -> dict[str, str]:
        """The split categories of an event list that still exist with a name (id → name)."""
        list_id = str(view.get("Id"))
        return {cid: name for cid, name in named_categories(view).items() if self.is_split(list_id, cid)}


def kinds_for(scopes: Iterable[str]) -> list[str]:
    """The kinds the token may read — only these get a field."""
    scopes = list(scopes)
    return [kind for kind in KINDS if any(s.startswith(_KIND_SCOPE[kind]) for s in scopes)]


def choices(
    lists: Iterable[Mapping[str, Any]],
    plans: Iterable[Mapping[str, Any]],
    visibility: Visibility | None = None,
    user_id: str | None = None,
    language: str | None = None,
) -> dict[str, dict[str, str]]:
    """Id → label per kind, plus the named categories of the visible calendars. Labels name the
    owner of shared objects (see naming.display_name)."""
    visibility = visibility or Visibility(None)
    result: dict[str, dict[str, str]] = {kind: {} for kind in (*KINDS, OPT_CATEGORIES)}
    for view in lists:
        list_id = str(view["Id"])
        name = view.get("Name") or list_id
        kind_type = view.get("Type") or LIST_TYPE_SHOPPING
        if kind_type == LIST_TYPE_EVENTS:
            result[KIND_CALENDARS][list_id] = display_name(name, view, user_id, language)
            if visibility.is_visible(KIND_CALENDARS, list_id):
                for category_id, category in named_categories(view).items():
                    result[OPT_CATEGORIES][category_key(list_id, category_id)] = display_name(
                        f"{name} · {category}", view, user_id, language
                    )
        elif kind_type in TODO_LIST_TYPES:
            result[KIND_LISTS][list_id] = display_name(name, view, user_id, language)
    for plan in plans:
        result[KIND_MEAL_PLANS][str(plan["Id"])] = display_name(
            plan.get("Name") or str(plan["Id"]), plan, user_id, language
        )
    return result


def _multi_select(names: Mapping[str, str]) -> SelectSelector:
    options = [
        SelectOptionDict(value=value, label=label)
        for value, label in sorted(names.items(), key=lambda pair: pair[1].casefold())
    ]
    return SelectSelector(SelectSelectorConfig(options=options, multiple=True, mode=SelectSelectorMode.LIST))


def schema(
    offered: Mapping[str, Mapping[str, str]], scopes: Iterable[str], visibility: Visibility, auto_add: bool
) -> vol.Schema:
    """One multi-select per readable kind (defaults: what is visible now), the category calendars
    (with calendar access; default: none) and the auto-add switch."""
    kinds = kinds_for(scopes)
    fields: dict[Any, Any] = {}
    for kind in kinds:
        names = offered.get(kind, {})
        default = [object_id for object_id in names if visibility.is_visible(kind, object_id)]
        fields[vol.Optional(kind, default=default)] = _multi_select(names)
    if KIND_CALENDARS in kinds:
        categories = offered.get(OPT_CATEGORIES, {})
        default = [key for key in categories if visibility.is_split(*key.split(":", 1))]
        fields[vol.Optional(OPT_CATEGORIES, default=default)] = _multi_select(categories)
    fields[vol.Optional(OPT_AUTO_ADD, default=auto_add)] = bool
    return vol.Schema(fields)


def _merged(previous: Iterable[str], offered: Iterable[str], chosen_now: Iterable[str]) -> list[str]:
    """(previously chosen − offered) ∪ (chosen now ∩ offered): not offered keeps its status."""
    offered = set(offered)
    return sorted((set(previous) - offered) | (set(chosen_now) & offered))


def options_from_input(
    user_input: Mapping[str, Any],
    offered: Mapping[str, Mapping[str, str]],
    scopes: Iterable[str],
    previous: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """What goes into entry.options, merged with the previous options (see the module docstring)."""
    previous = previous or {}
    kinds = kinds_for(scopes)
    options: dict[str, Any] = {}
    known = set(previous.get(OPT_KNOWN) or [])
    for kind in kinds:
        ids = offered.get(kind, {})
        options[kind] = _merged(previous.get(kind) or [], ids, user_input.get(kind, []))
        known |= set(ids)
    if KIND_CALENDARS in kinds:
        options[OPT_CATEGORIES] = _merged(
            previous.get(OPT_CATEGORIES) or [], offered.get(OPT_CATEGORIES, {}), user_input.get(OPT_CATEGORIES, [])
        )
    options[OPT_AUTO_ADD] = bool(user_input.get(OPT_AUTO_ADD, True))
    options[OPT_KNOWN] = sorted(known)
    return options
