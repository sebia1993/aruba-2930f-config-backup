# Aruba 2930F Hostname Direct Discovery Design (2026-11-18)

## Issue and verified root cause
v0.10.0 obtains Hostname from only `hostname_from_prompt(session.get_prompt())` for PoE/LLDP collection and in both `PortControlService` inspections. Special/space-bearing AOS-S EXEC prompts are valid but return `None`, displayed as `확인 불가`. Backup collection has a separate `hostname_from_running_config` fallback that PoE does not use.

## Goal
Retrieve authoritative AOS-S `System Name` once per switch from read-only `show system information`, consistently across collection and before/after PoE control; maintain safe fallbacks and provide diagnostic source. Use manufacturer documented read-only command (AOS-S 16.08/16.10/16.11) without assuming all WC.16.01 installations support identical output.

## Protocol
1. After trusted SSH, enabling EXEC, disabling pager and setting terminal width, validate the model via existing `show version` and `show modules`.
2. Call **exact, allowlisted** `show system information` to extract `System Name : ...`. Accept printable 1-255 character names, ignore ANSI SGR, normalize whitespace/CRLF and reject conflicting duplicates. Never parse System Contact/Location as a name.
3. If the command is **explicitly unsupported/denied** (`COMMAND_REJECTED`) or no safe System Name is present, fall back to the already verified EXEC prompt hostname.
4. If the prompt also supplies no usable name, make a **single last-resort read-only** `show running-config` call and reuse the existing banner-safe hostname parser; check bounded output before parsing. Never log or persist raw config in the hostname resolver.
5. A network timeout, SSH key mismatch, loss of connection, canceled query or other transport failure must not be converted to a false successful Hostname. Propagate non-`COMMAND_REJECTED` exceptions.
6. When none of the sources yields a name, the UI shows `확인 불가`, plus `hostname_source=unavailable`; PoE ON's existing pinned host-key/model/port safeguards are preserved. Do not infer the name from IP or LLDP AP.
7. Never compare untrusted `System Name` as a substitute for SSH key/model/port identity. Two explicitly known, different names remain a conflict in existing PoE safety logic.
8. Leave privileged-port command prompt safety, PoE OFF LLDP constraints, staged write-ahead audit, no automatic write retries and all four GUI pages unchanged.

## User-observable outcome
- `PoEQueryResult.identity.hostname` and `snapshot.hostname` reflect the authoritative `System Name` when available, consistently for all explicitly selected interfaces on one switch.
- `DeviceIdentity.hostname_source` provides `system-information`, `prompt`, `running-config` or `unavailable`, and port result details show it.
- Reading model + system information should add only one fixed read-only command per switch; never per port.
- AOS-Special prompts may still block configuration writes if the independent command prompt validator cannot ensure an exact EXEC terminal; do not silently loosen this.

## Release scope
- Version **v0.10.1**, new validated Windows x64 **GitHub Pre-release** on this existing PR #56 development branch.
- Existing `v0.10.0` assets/tag and main branch remain unchanged. Windows CI must validate tests, typing, security audit, package smoke, SHA-256, SBOM and branch-only provenance before publish.
- Field PoE verification is user-run and not implied by CI success.
