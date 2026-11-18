# Examples

**English** · [Deutsch](de/examples.md)

[Documentation overview](README.md) · [Action reference](actions.md)

The entity IDs below are **examples**. Replace them with the actual IDs under
**Settings → Devices & services → Entities**.
Try an individual action under **Developer tools → Actions → YAML mode**.
Complete automations belong in the YAML editor of a new automation.

## 1. Add a shopping item with a quantity

```yaml
action: amzettel.add_item
target:
  entity_id: todo.weekly_shopping
data:
  name: Milk
  quantity: 2
  unit: L
  category: Dairy
  note: For the school snack
```

Repeating this is not side-effect-free: a second call may increase the quantity.

To test Assist, type:

> Add milk to the Weekly shopping list.

To complete the item:

> Complete milk on the Weekly shopping list.

## 2. An event without duplicates, in a chosen time zone

```yaml
action: amzettel.add_event
target:
  entity_id: calendar.household
data:
  summary: Dentist
  start_date_time: "2026-11-24 10:00:00"
  time_zone: Europe/Vienna
  description: Bring your insurance card
  external_id: dentist-appointment-4711
```

Without an end, this is a point in time. You can later change the time using the same `external_id`;
an identical call does not create a duplicate.
To set a category, also supply `category` with its actual name or ID.

An all-day event:

```yaml
action: amzettel.add_event
target:
  entity_id: calendar.household
data:
  summary: Recycling collection
  start_date: "2026-11-26"
  end_date: "2026-11-27"
  external_id: recycling-2026-11-26
```

`end_date` is the **following day**, not the final day itself.

## 3. Import waste-collection dates from another HA calendar

A different integration must provide a calendar containing collection dates.
This example uses `calendar.waste_source` as the **source** and
`calendar.household` as the **amZettel target**.
Installing or configuring a specific waste-collection integration is outside the scope of amZettel.

The automation queries the next 60 days each day.
It stops if fetching the source fails or returns no events.

```yaml
alias: Waste collection dates to amZettel
triggers:
  - trigger: time
    at: "06:00:00"
actions:
  - action: calendar.get_events
    target:
      entity_id: calendar.waste_source
    data:
      start_date_time: "{{ today_at().isoformat() }}"
      end_date_time: "{{ (today_at() + timedelta(days=60)).isoformat() }}"
    response_variable: source
  - condition: template
    value_template: "{{ source['calendar.waste_source']['events'] | count > 0 }}"
  - action: amzettel.sync_events
    target:
      entity_id: calendar.household
    data:
      source: waste_calendar
      start: "{{ today_at().isoformat() }}"
      end: "{{ (today_at() + timedelta(days=60)).isoformat() }}"
      events: >-
        {% set ns = namespace(events=[]) %}
        {% for e in source['calendar.waste_source']['events'] %}
          {% set key = (e.get('uid') or e.summary) ~ '|' ~ e.start %}
          {% set item = dict(external_id=key, summary=e.summary, start=e.start) %}
          {% if e.get('end') %}
            {% set item = dict(item, end=e.end) %}
          {% endif %}
          {% set ns.events = ns.events + [item] %}
        {% endfor %}
        {{ ns.events }}
mode: single
```

Start with a few test events and check the result.
Synchronization may delete events only when the source supplies the entire period.
Do not add `continue_on_error` to the source fetch.
The empty list is deliberately **not** accepted with `allow_empty: true` in this example.

Many calendars do not supply `uid`. This example then uses title and start as the identifier.
Two events with the same title and start would have duplicate identifiers and be rejected.
If your source has its own stable collection ID, prefer that and adapt the mapping.
When the start changes as part of the identifier, the new event is created and the old one
is removed within the synchronization period.

## 4. A waste-collection reminder the evening before

For this automation, the **Waste** category has been selected as a separate calendar,
`calendar.household_waste`, in the integration.
The example creates a notification inside HA.

```yaml
alias: Waste collection tomorrow
triggers:
  - trigger: calendar
    entity_id: calendar.household_waste
    event: start
    offset: "-05:00:00"
actions:
  - action: persistent_notification.create
    data:
      title: Waste collection tomorrow
      message: "Tomorrow: {{ trigger.calendar_event.summary }}"
mode: queued
```

This fits **all-day** collection dates: midnight minus five hours is 19:00 the day before.
For a timed event, the offset is relative to that time.
Test calendar triggers by creating a new event sufficiently far in the future;
do not expect an already-passed start to trigger.

Replace the last action with your phone notification or TTS/speaker action if you prefer.
Its target and setup depend on your HA installation.

## 5. Query today's meal plan

This script creates an HA notification from structured meal data:

```yaml
alias: What is on the menu today
sequence:
  - action: amzettel.get_meals
    target:
      entity_id: calendar.my_meal_plan
    data:
      start: "{{ now().date().isoformat() }}"
      end: "{{ now().date().isoformat() }}"
    response_variable: plan
  - action: persistent_notification.create
    data:
      title: Today's meal plan
      message: >-
        {% set meals = plan['calendar.my_meal_plan']['meals'] %}
        {% set ns = namespace(lines=[]) %}
        {% for meal in meals %}
          {% set names = meal.dishes | map(attribute='name') | join(', ') %}
          {% set ns.lines = ns.lines + [names] %}
        {% endfor %}
        {{ ns.lines | join('; ') if ns.lines else 'Nothing planned yet.' }}
```

Meals hidden from the calendar can also appear in this response.
`time` and `time_zone` report a stored meal snapshot when present.
Without a snapshot, a named meal time is not a binding clock time; the automation decides when to run.

## 6. Node-RED

With a connection from Node-RED to HA already configured, a flow can use the normal HA entities,
actions and events. It does not need an additional **amZettel token**.
Node-RED's authentication to HA is separate.

### Checklist for HA automations and Node-RED

- **Prevent duplicate triggers:** Debounce noisy sensors, avoid feedback loops and do not
  retry writes blindly. Each call to
  [`amzettel.add_item`](actions.md#amzetteladd_item) can increase the quantity again.
- **Make calendar writes repeatable:** Give
  [`amzettel.add_event`](actions.md#amzetteladd_event) a stable `external_id` so another
  call updates the same event instead of creating a duplicate.
- **Permissions still apply:** Calendar writes require Premium and write permission;
  meal plans remain read-only. Node-RED does not bypass
  [amZettel permissions](configuration.md#which-permissions-does-ha-need).
- **Use query responses:** For `get_items`, `get_events` and `get_meals`, specify
  `response_variable` in HA scripts and automations. In Node-RED, configure the action
  node's response output, such as the `results` output type.
  See [Use responses](actions.md#use-responses).
- **Events are not a complete change log:** If the flow needs the current contents,
  also query them after a restart or reconnect.
  The [event reference](actions.md#events-for-automations-and-node-red) explains which
  changes emit events and which may be combined.
- **Only synchronize complete calendar sources:** Pass the complete contents of the
  chosen period to [`sync_events`](actions.md#amzettelsync_events).
  Stop if fetching the source fails; do not substitute an empty list or authorize it
  with `allow_empty: true`.

Example: “coffee is running low”:

1. Use your existing sensor/threshold flow.
2. In an HA action node, called “Call service” in older versions, select **`amzettel.add_item`**.
3. Target `todo.weekly_shopping` with data such as
   `{"name":"Coffee","quantity":1,"unit":"pack"}`.

Example: “react to a new item”:

1. Subscribe to `amzettel_item_added` in the event node, often called “Events: all.”
2. Start with a debug node to inspect the actual message output.
3. Filter by `list_id` or `list_name` in the event data.
4. Pass `name`, `quantity` and `unit` to the next step.

The exact location of the event data in a Node-RED message depends on the node version and its
output configuration. That is why these instructions do not assume a path such as `msg.payload.event.name`.
The complete event fields are listed in the
[reference](actions.md#events-for-automations-and-node-red).
