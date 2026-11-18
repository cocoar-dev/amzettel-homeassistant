# Release notes

**English** · [Deutsch](de/release-notes.md)

[Documentation overview](README.md)

## 0.1.0

### Features

- Lists as HA to-do entities: read, add, edit, complete and delete items.
- Read calendars and, with Premium, create, edit and delete events;
  selected categories can have separate calendars.
- Meal plans as read-only calendars and structured queries.
  Stored meal times and their time zones remain authoritative after changes to
  templates or the plan's zone.
- Realtime updates, custom HA actions and events for automations.
- Device sign-in through amZettel's branded Modgud page without a client secret in HA;
  selectable permissions and objects.
- Identifiable signed-in account, distinguishable shared objects and
  a redacted account label in diagnostics.
- German and English guides with genuine installation, amZettel sign-in,
  device-approval and calendar screenshots using dedicated demo accounts on production.

### Known limitations

- Meal plans remain read-only, including with Premium.
- A stored meal instant has no invented duration; HA may display **18:00–18:00**.
  Legacy named meals without a snapshot remain all-day.
- During the repeated autumn hour, HA's calendar `on`/`off` display can differ because
  of local-time comparisons in the HA framework. Queried instants remain correct;
  an expected regression test documents this limitation.
- Modgud's English interface currently shows amZettel's permission descriptions
  in German. The English guide explains the labels actually displayed.
- No automatic Alexa integration. Voice control uses a suitably configured HA Assist setup.

### Installation and use

The [installation guide](installation.md) covers manual copying and the complete sign-in flow.
The [usage guide](configuration.md) illustrates moving an event, shared lists,
the Free/Premium difference and all-day demo meals.
