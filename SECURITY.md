# Security policy

## Reporting a vulnerability

**Do not open a public issue for a potential security vulnerability.**
Report it privately to **bwi@cocoar.dev**.

Include:

- The affected integration version or commit and Home Assistant version.
- A description, minimal reproduction and expected impact.
- Any suggested mitigation.

Do not send access tokens, refresh tokens, sign-in codes, unredacted diagnostics,
or other people's personal data. Redact examples before sharing them.
See the [diagnostics guidance](docs/troubleshooting.md#diagnostics-and-support).

## Scope and safe testing

This policy covers this Home Assistant integration, including authentication,
token handling, synchronization and write operations.
If a dependency vulnerability affects the integration, explain that impact.

Test only installations and accounts you own or have explicit permission to test.
Do not probe production services or access other users' data.
If a report concerns Home Assistant or the amZettel service rather than this
integration, identify that component so it can be routed to the appropriate maintainer.

## Fixes and disclosure

Security fixes target the latest integration version and current development code.
Backports to older versions are not guaranteed.
Responses and fixes are best-effort; no fixed response time is promised.
Please allow time to investigate and coordinate a fix before public disclosure.
