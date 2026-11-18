"""Sample data of the public integrations API v1.

The constants are the views the coordinator holds (what `feed_api.map_object` makes of a wire
object); `wire()` turns a view back into the object `GET /api/integrations/v1/objects/…` and the
snapshot pages carry, so the fake server and the tests share one source.
"""

from __future__ import annotations

import copy
from typing import Any

SHOPPING_ID = "aaaaaaaa-0000-0000-0000-000000000001"
CHECKLIST_ID = "aaaaaaaa-0000-0000-0000-000000000002"
TODO_ID = "aaaaaaaa-0000-0000-0000-000000000003"
EVENTS_ID = "aaaaaaaa-0000-0000-0000-000000000004"
PLAN_ID = "bbbbbbbb-0000-0000-0000-000000000001"

MILK_ID = "cccccccc-0000-0000-0000-000000000001"
BREAD_CHECK = "dddddddd-0000-0000-0000-000000000001"
OLD_CHECK = "dddddddd-0000-0000-0000-000000000002"
PASS_ID = "cccccccc-0000-0000-0000-000000000002"
CASE_ID = "cccccccc-0000-0000-0000-000000000003"
TAX_ID = "cccccccc-0000-0000-0000-000000000004"
EVENT_ID = "cccccccc-0000-0000-0000-000000000005"
CATEGORY_ID = "eeeeeeee-0000-0000-0000-000000000001"
PERSON_ID = "ffffffff-0000-0000-0000-000000000001"

SHOPPING: dict[str, Any] = {
    "Id": SHOPPING_ID,
    "ETag": '"shopping-1"',
    "Name": "Einkauf",
    "Type": "Shopping",
    "IsOwner": True,
    "Items": [
        {
            "Id": MILK_ID,
            "Name": "Milch",
            "Quantity": 2,
            "Unit": "L",
            "Category": "Kühlregal",
            "Store": "Billa",
            "Note": "laktosefrei",
            "PhotoId": "99999999-0000-0000-0000-000000000001",
        },
        {"Id": "cccccccc-0000-0000-0000-000000000009", "Name": "Eier", "Quantity": 1},
    ],
    "RecentPurchases": [
        {
            "CheckId": BREAD_CHECK,
            "ItemId": "cccccccc-0000-0000-0000-000000000010",
            "Name": "Brot",
            "Quantity": 1,
            "CheckedAt": "2026-11-13T10:00:00+00:00",
        },
        {
            "CheckId": OLD_CHECK,
            "ItemId": "cccccccc-0000-0000-0000-000000000011",
            "Name": "Butter",
            "Quantity": 1,
            "CheckedAt": "2026-11-10T10:00:00+00:00",
        },
    ],
    "CartClosedAt": "2026-11-11T00:00:00+00:00",
}

CHECKLIST: dict[str, Any] = {
    "Id": CHECKLIST_ID,
    "ETag": '"checklist-1"',
    "Name": "Urlaub",
    "Type": "Checklist",
    "IsOwner": True,
    "Items": [
        {"Id": PASS_ID, "Name": "Pass", "Quantity": 1, "Done": False},
        {
            "Id": CASE_ID,
            "Name": "Koffer",
            "Quantity": 1,
            "Done": True,
            "DoneAt": "2026-11-12T08:00:00+00:00",
        },
    ],
    "RecentPurchases": [],
}

TODO: dict[str, Any] = {
    "Id": TODO_ID,
    "ETag": '"todo-1"',
    "Name": "Aufgaben",
    "Type": "Todo",
    "IsOwner": True,
    "Items": [
        {
            "Id": TAX_ID,
            "Name": "Steuer",
            "Quantity": 1,
            "Note": "bis Freitag",
            "Due": {
                "Date": "2026-11-20",
                "Time": "10:00",
                "Zone": "Europe/Vienna",
                "ReminderMinutes": 30,
            },
        }
    ],
    "RecentPurchases": [],
}

EVENTS: dict[str, Any] = {
    "Id": EVENTS_ID,
    "ETag": '"calendar-1"',
    "Name": "Familie",
    "Type": "Events",  # set by the mapping of a Calendar object
    "IsOwner": True,
    "EventCategories": [{"Id": CATEGORY_ID, "Name": "Müll", "Color": "t3"}],
    "Persons": [{"Id": PERSON_ID, "Name": "Oma"}],
    "Items": [
        {
            "Id": EVENT_ID,
            "Name": "Gelbe Tonne",
            "Note": "vor 6 Uhr",
            "CategoryId": CATEGORY_ID,
            "PersonIds": [PERSON_ID],
            "Due": {"Date": "2026-11-16"},
            "Recurrence": {
                "Rule": "RRULE:FREQ=WEEKLY;INTERVAL=2",
                "RDateLines": None,
                "ExDateLines": ["EXDATE;VALUE=DATE:20261130"],
            },
        }
    ],
}

PLAN: dict[str, Any] = {
    "Id": PLAN_ID,
    "ETag": '"plan-1"',
    "Name": "Essen",
    "IsOwner": True,
    "DefaultPeople": 4,
    "Slots": [
        {"Id": "slot-lunch", "Name": None, "Timing": "Lunch", "SortMinutes": 720},
        {"Id": "slot-jause", "Name": "Jause", "Timing": None, "SortMinutes": 960},
    ],
    "Entries": [
        {
            "Id": "meal-1",
            "Date": "2026-11-16",
            "Timing": "Lunch",
            "SlotId": "slot-lunch",
            "People": 3,
            "Dishes": [{"Id": "d1", "Name": "Spaghetti"}, {"Id": "d2", "Name": "Salat"}],
            "Note": "Nudeln kaufen",
        },
        {
            "Id": "meal-2",
            "Date": "2026-11-16",
            "Timing": "Unspecified",
            "SlotId": "slot-jause",
            "People": 2,
            "Dishes": [{"Id": "d3", "Name": "Brote"}],
        },
        {
            "Id": "meal-3",
            "Date": "2026-11-17",
            "Timing": "ExactTime",
            "Time": "18:30",
            "People": 4,
            "Dishes": [{"Id": "d4", "Name": "Pizza"}],
        },
    ],
}


def lists() -> list[dict[str, Any]]:
    return copy.deepcopy([SHOPPING, CHECKLIST, TODO, EVENTS])


def plans() -> list[dict[str, Any]]:
    return copy.deepcopy([PLAN])


def area_of(view: dict[str, Any]) -> str:
    if "Entries" in view:
        return "MealPlans"
    return "Calendar" if view.get("Type") == "Events" else "Lists"


KIND = {"Lists": "List", "Calendar": "Calendar", "MealPlans": "MealPlan"}


def wire(view: dict[str, Any]) -> dict[str, Any]:
    """The v1 object of a view: Calendar data has `Events` and `RDates`/`ExDates`, no `Type`."""
    area = area_of(view)
    data = copy.deepcopy(view)
    ident, etag = data.pop("Id"), data.pop("ETag")
    if area == "Calendar":
        data.pop("Type", None)
        data["Events"] = [
            {**item, "Recurrence": {
                "Rule": rec.get("Rule"), "RDates": rec.get("RDateLines"), "ExDates": rec.get("ExDateLines"),
            }} if isinstance(rec := item.get("Recurrence"), dict) else item
            for item in data.pop("Items")
        ]
    return {"Area": area, "Kind": KIND[area], "Id": ident, "ETag": etag, "Data": data}
