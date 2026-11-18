# Troubleshooting

**English** · [Deutsch](de/troubleshooting.md)

[Documentation overview](README.md)

## amZettel is missing from Add integration

- Is `manifest.json` at `/config/custom_components/amzettel/manifest.json`?
- Did you copy the entire folder, including `translations` and `brand`?
- Did you **restart HA completely**, not just the browser page?
- Are you running HA 2026.10.0 or newer?
- After restarting, fully reload the page and check **Settings → System → Logs**.

A missing logo alone is not a reason to remove the integration or sign in again.
The warning that a custom integration has not been tested by Home Assistant is expected
for a custom integration and does not itself indicate a connection failure.

## The code expired or was denied

Restart the setup flow and use its new link.
A link and code belong to that specific attempt; an older browser tab will not help.
A denial is not an HA error: no connection is created without approval.

## A generic sign-in form appears instead of amZettel

Use a current integration version with the default server,
`https://app.amzettel.at`, and restart setup. Open only the new link supplied by HA:
it opens the amZettel sign-in page at `amzettel.auth.cocoar.dev`.
Do not use an old tab, construct a login address yourself or rewrite a device link's host.
The [sign-in screenshots](installation.md#4-sign-in-to-amzettel) show
e-mail-code and passkey options, not a generic username/password form.

The subsequent **Connect Home Assistant?** device-approval page has a different
layout. It is not a second sign-in form.

## amZettel rejected the sign-in

Approval at the sign-in service does not necessarily mean the API token was accepted.
Check that the server is `https://app.amzettel.at`. Other servers are not supported.
If sign-in is still rejected there, contact amZettel support.
Do not copy tokens or passwords from logs into public reports.

## HA asks for sign-in after revocation

This is expected. Under **Settings → Devices & services**, follow the reauthentication prompt
and use the **same amZettel account**.
The existing selection is preserved through this reauthentication flow.
If you no longer want the connection, remove the entry from HA instead.

A temporarily unreachable server, in contrast, should not require permanent reauthentication.
If this happens repeatedly, check diagnostics and logs.

## Lists or calendars are missing

- Can the connected amZettel account see the object, or has it been shared with that account?
- Was the corresponding area granted when connecting?
- Is the object selected under **Configure**?
- Is **Add new lists, calendars and meal plans automatically** disabled?
- Has the event been moved to a separate category calendar?

The main calendar does not also display categories that have been split out.
Meal-plan entries with calendar display disabled are deliberately absent from the calendar entity.

## Reading works, writing does not

Check the three independent restrictions:

1. The **write permission granted during sign-in**.
2. Access in amZettel under **Profile → Connected systems**.
3. **Plan and object access:** writing calendar events requires Premium;
   shared or frozen objects may have additional restrictions.

A repair message about Premium or missing permission disappears after the next successful
write in the affected area.
**Configure** cannot request new scopes:
see [changing permissions](installation.md#change-permissions),
including the warning about local synchronization ownership.

## Assist cannot find the list

- Start with a **typed** sentence and the local HA conversation agent in English.
- Use the actual display name, for example “Add milk to the Weekly shopping list.”
- If two lists have the same name, rename them unambiguously or assign suitable aliases in HA.
  An ambiguous name may be rejected.
- Expose the entity to Assist if HA asks.
- If the item ended up in HA's own shopping list, the sentence may have been understood
  as a general shopping-list command. Explicitly name the target list.

Alexa, microphones and speech recognition are not part of this integration.
A successful typed Assist test does not prove that an Alexa connection has been configured.

## Event times are off by one or two hours

Check the time zone configured **inside HA**.
Setting `TZ=Europe/Vienna` on the container is not the HA setting.

You can explicitly choose `time_zone: Europe/Vienna` in `amzettel.add_event`.
A timestamp with an offset already identifies an instant, which is preserved during conversion.
A date without a time remains all-day. For calendar actions, an all-day end is the **following day**.
The amZettel editor shows the stored zone; UTC is not silently converted to Vienna time.

At the autumn clock change, distinguish the actual instant from the calendar entity's `on`/`off` state;
see [known limitations](actions.md#known-limitations).

## `sync_events` rejects an empty list

This protects against deletion when a source is temporarily unavailable.
Check the source fetch first. Use `allow_empty: true` deliberately only when the complete
period really should be empty. Do not enable it as general error handling.

If later synchronization fails to remove old events, its local ownership mapping may have
been lost, for example by removing and adding the integration entry again.
Existing events are not simply claimed as owned.

## Bought items cannot be deleted

Shopping lists have a purchase history. “Remove completed items” does not delete bought items
from that history.
Mark a purchase as “not completed” to undo it; then delete the open item if needed.
Do not confuse this with finishing a shopping trip in the amZettel app.

## Changes do not arrive immediately

- Check connectivity from HA to the amZettel server.
- HA uses SSE at `/api/integrations/v1/changes`.
  Proxies must not buffer the response. HA catches up after disconnects and loads a complete
  snapshot when the cursor expires or access changes. When the server returns `429`, HA backs off
  before retrying and keeps the affected data hidden until a complete sync confirms access.
  If a write conflicts, check the current
  state before retrying rather than overwriting blindly.

A `calendar` state of `off` means no event is currently active.
`unavailable`, in contrast, indicates missing data or connectivity.

## Server not supported

Use `https://app.amzettel.at`. If HA reports that REST/SSE v1 is unavailable there,
contact amZettel support. Signing in again will not fix this.

## Diagnostics and support

Download diagnostics from the integration entry under **Settings → Devices & services**,
provided that the entry is loaded. Also search the HA logs for `amzettel`.

The integration redacts tokens and personal content from its diagnostics.
Still inspect the file yourself before sharing it.
**Never** attach `.storage/core.config_entries`, login codes, tokens, passwords or the
entire HA configuration directory to a public report.

A useful bug report includes:

- HA and integration versions,
- the server address used (only `https://app.amzettel.at` is supported),
- the action and exact error message,
- expected versus observed behavior,
- redacted diagnostics and a short relevant log excerpt.
