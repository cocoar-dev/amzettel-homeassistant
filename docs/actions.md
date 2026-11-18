# Actions and events

**English** · [Deutsch](de/actions.md)

[Documentation overview](README.md) · [Examples](examples.md)

Find the custom actions under **Developer tools → Actions**, with the `amzettel` prefix.
Choose an entity belonging to this integration as the target. The examples use fictional entity IDs:
replace them with your installation's IDs, not just their display names.

## Use responses

`get_items`, `get_events` and `get_meals` require a response; specify `response_variable`
in scripts and automations. `add_item`, `add_event` and `sync_events` can optionally return responses.
For multiple targets, the response contains an entry for each entity ID:

```yaml
action: amzettel.get_items
target:
  entity_id: todo.weekly_shopping
data:
  include_completed: true
response_variable: shopping
```

The items are then available under `shopping['todo.weekly_shopping']['items']`.
A response does not automatically create a new HA entity.

## `amzettel.add_item`

Target: a list (`todo`).

| Field | Meaning |
|---|---|
| `name` | Required: item name |
| `quantity` | Positive quantity; default 1 |
| `unit` | Optional, for example `L` |
| `category` | Optional shopping category as text |
| `note` | Optional note |

The response contains `item_id`. amZettel may merge quantities for matching shopping items;
the returned ID is then the merged item's ID.
Repeating the call is **not idempotent** and may add more quantity.

Standard actions `todo.add_item`, `todo.update_item` and `todo.remove_item` also work.
Quantity and unit are additional fields of our action.

## `amzettel.add_event`

Target: a regular amZettel calendar or category calendar (`calendar`), **not a meal plan**.
Requires `calendar.write`, Premium and continued access to the calendar.

| Field | Meaning |
|---|---|
| `summary` | Required: title |
| `start_date_time` | Start with a time; alternative to `start_date` |
| `end_date_time` | Optional end with a time |
| `time_zone` | Optional IANA zone, such as `Europe/Vienna`, `Europe/London` or `UTC` |
| `start_date` | First day of an all-day event; alternative to `start_date_time` |
| `end_date` | Optional: the day **after** the last day |
| `description` | Optional note; omitting the field leaves an existing note unchanged |
| `category` | Optional category by name or ID; omitting it leaves the category unchanged |
| `external_id` | Optional: your stable identifier for this event within the calendar |

Start and end must both be dates or both date-times.
Without an end, the result is a point in time or a single all-day event.
Category calendars create events in their own category; specifying another category is rejected.

**Repeatable calls:** the same `external_id` within the same calendar updates the same event
instead of creating another. A change returns `updated`; an identical call returns `unchanged`.
Without an identifier, every call creates a new event.
The response contains `event_id` and `result` (`created`, `updated`, `unchanged`).

This action does not manage recurrence rules. To create a recurring series, use the supported
standard `calendar.create_event` action with `rrule`.
Use the amZettel app to end a series or edit specific occurrences.

## `amzettel.sync_events`

Target: a regular amZettel calendar or category calendar.
Requires the same write permissions as `add_event`.

| Field | Meaning |
|---|---|
| `source` | Required: stable source name, such as `waste_calendar` |
| `start` | Required: start of the synchronization period |
| `end` | Required: end of the synchronization period |
| `events` | Required: list of supplied events |
| `allow_empty` | Default `false`; an empty list is accepted only with `true` |

Each element of `events` requires `external_id`, `summary`, `start` and optionally accepts
`end`, `description`, `category`. `start` and `end` may be ISO dates or ISO timestamps,
as in `calendar.get_events`. Unsupported extra fields, such as `location`, are ignored.

**What synchronization does:**

1. Create or update supplied events using their stable identifiers.
2. Remove events no longer supplied, but **only** if they
   - are recorded in HA's local ownership mapping for this source,
   - have the ID derived from that source and external identifier, and
   - start within the period according to a fresh server read.
3. Leave other events, other sources and this source's events outside the period undeleted.

A date-only end includes the entire day for the **synchronization period**.
An end timestamp is an exclusive boundary.
For **individual all-day events**, in contrast, `end` remains the day after the event.

The response contains counts for `created`, `updated`, `removed` and `unchanged`.
A failed required fresh read prevents writing.
Synchronization consists of multiple HTTP requests, not a server-side transaction:
if a write fails partway through, individual changes may already have arrived.
Stable identifiers allow the same synchronization to be retried.

> [!WARNING]
> `allow_empty: true` explicitly means “the source confirms that this period contains no events.”
> Do not use it as a substitute for a failed source fetch.
> An incompletely supplied period can delete this source's events even when the list is not empty.
> Always supply the complete contents of the selected period.

**Ownership and backups:** the mapping is stored in HA at `.storage/amzettel.sync_sources`.
Do not edit it manually. Include the HA configuration directory in your backups.
Removing the integration entry discards the mapping but leaves events in amZettel.
Without the old mapping, later synchronization cannot automatically recognize old, no-longer-supplied
events as its own.

Keep `source` and `external_id` stable between runs.
Identifiers for `add_event` and `sync_events` are deliberately separate:
an event created by `add_event` is not subsequently adopted by a similarly named `sync_events` entry.

## Queries

### `amzettel.get_items`

Target: a list. Optionally use `include_completed: true`; the default returns only open items.

Response: `list_id`, `name`, `type`, `items`.
Items include `id`, `name`, `quantity`, `unit`, `category`, `note`, `due`, `completed`
and `completed_at`.
For shopping lists, completed items are **recent purchases** according to the app's rule,
not the complete purchase history.

### `amzettel.get_events`

Target: a calendar or category calendar. Optional fields: `start`, `end`, `category` (name or ID).
Without a period: the next seven days from now. A date-only end includes the entire day.

Response: `calendar_id`, `start`, `end`, `events`.
Recurring series are expanded into occurrences. Entries include `uid`, `recurrence_id`, `summary`,
`start`, `end`, `all_day`, `category_id`, `category`, `note`, `persons`, `recurring`.
For all-day results, `end` is the day after the event.

Queries on the main calendar include all categories, including those displayed separately.
A category calendar returns only that category's events.

### `amzettel.get_meals`

Target: a meal-plan calendar. Optional `start`, `end`; otherwise the next seven days from now.

Response: `meal_plan_id`, `start`, `end`, `meals`.
Meals include their ID, date, time information, dishes, number of people, note
and the calendar-display flag.
`time` and `time_zone` contain the stored snapshot time and zone; without a snapshot,
`time_zone` is null and `time` is the legacy exact time or null. The date remains the stored meal date;
the query window uses the actual instant in the stored zone.
Meals hidden from the calendar are included too.
The action does not plan meals or add ingredients to a shopping list.

## Time zones and date boundaries

Without `time_zone`, `add_event` uses HA's configured time zone.
With `time_zone`:

- `"2026-11-24 10:00:00"` and `Europe/Vienna`: **10:00 in Vienna**.
- `"2026-11-24T09:00:00Z"` and `Europe/Vienna`: **the same instant**, 10:00 in Vienna.
- `"2026-11-24 09:00:00"` and `UTC`: **09:00 UTC**, not 09:00 Vienna time.
- All-day events remain dates, even if `time_zone` is supplied.

`sync_events` has no separate `time_zone` field. Prefer timestamps with offsets there;
times without offsets use HA's configured zone.

The API stores minutes, not seconds. For repeated local clock times during the autumn clock change,
write operations use UTC to retain the exact instant.
A time in the skipped spring hour is normalized to the real, later local time.

A recurring fixed UTC time is not the same rule as a fixed clock time in `Europe/Vienna`
across a local daylight-saving transition. A location's zone cannot be inferred from an offset.

## Events for automations and Node-RED

All contain `config_entry_id`. Object and item IDs are amZettel IDs, not HA entity IDs.

| Event | Important data |
|---|---|
| `amzettel_item_added` | `list_id`, `list_name`, `item_id`, `name`, `quantity`, `unit`, `category`, `note` |
| `amzettel_item_completed` | As above; `by_user_id` only when the server supplies the buyer |
| `amzettel_item_removed` | Same as `item_added` |
| `amzettel_event_created` | `calendar_id`, `calendar_name`, `event_id`, `summary`, `start`, `all_day`, category and note |
| `amzettel_event_changed` | Same as `event_created` |
| `amzettel_event_removed` | Same as `event_created` |
| `amzettel_meal_planned` | `meal_plan_id`, `meal_plan_name`, `meal_id`, `date`, `time`, `time_zone` (snapshot zone, otherwise null), `slot`, `dishes`, `people`, `note` |

The first load, a reload or an object's first appearance does not emit events for all its contents.
Only subsequent differences trigger events.
A reconnect compares against the last state.
A purchase emits `item_completed` rather than `item_removed`.
Merging a shopping item's quantity into an existing ID does not produce `item_added`.

These events are generated from state comparisons and are **not a complete change log**.
Multiple changes between two reads may be combined.
If a flow needs the complete current state, also use a query action.

## Known limitations

- Calendars can only update or delete whole series, not individual occurrences or
  “this and future occurrences” starting at a later occurrence.
  Renaming a series preserves its recurrence rule.
- Bought items remain in purchase history. Deleting purchases, including
  `todo.remove_completed_items` when purchases exist, is rejected with an explanatory message.
  Mark a purchase as “not completed” to undo it.
- A single `todo.update_item` call that **completes an item and clears all its existing
  note/due fields** is treated as a status-only change.
  Assist sends the same incomplete data; preventing accidental data loss takes priority.
  To clear the fields deliberately, clear them first, then complete the item in a second call.
- In HA 2026.10.4, a calendar entity's `on`/`off` state can be wrong during the repeated hour
  at the autumn clock change. This is a limitation of HA's base class;
  queries and stored instants must be distinguished from that state.
