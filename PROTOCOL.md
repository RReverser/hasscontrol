# HassControl BLE protocol (v2)

The watch talks to Home Assistant directly over Bluetooth Low Energy. Home
Assistant (the `garmin_ble` custom integration in `custom_components/`) is the
GATT **peripheral**; the watch is the **central**. No phone, no Wi-Fi, no
bridge device.

Every frame in both directions fits in **20 bytes**. Connect IQ throws
`InvalidRequestException` for characteristic writes longer than 20 bytes and
does not implement long reads or long writes, and the ATT MTU on the watch is
not negotiable from Connect IQ, so notifications are also kept to 20 bytes.

## Pairing and approval (required)

1. On the watch choose **Pair**. The watch bonds with HA using standard
   LE Secure Connections pairing with Numeric Comparison; the user confirms
   the 6-digit code on the watch. HA accepts the bond at once and records the
   code. A bond alone grants nothing.
2. HA opens a discovery flow under Settings > Devices & services:
   "Allow Garmin watch XX:XX:XX:XX:XX:XX?" with the same code. It can be
   approved at any time; until then HELLO gets RESULT NOT_APPROVED and the
   watch keeps retrying.
3. On approval HA generates a random 16-byte command key for that watch.
   The next HELLO from the watch without a stored key gets MSG_KEY (over the
   encrypted link) before the CHALLENGE. The watch stores it; every command
   is signed with it. No secret is entered anywhere.

Enforcement on HA: CMD is `secure-write` and EVT's CCCD `secure-notify`
(BlueZ rejects both on links without an LE Secure Connections key); Just
Works pairing is refused; HELLO from an address that is not approved gets
NOT_PAIRED or NOT_APPROVED; a new bond for an approved address drops the
approval and asks again. The integration's options list approved and
waiting watches and can forget them (approval, key and bond removed).

HA cannot start pairing itself: Connect IQ apps can only act as a BLE
central, so the watch never advertises anything HA could connect to.

## GATT layout

| Item | UUID | Properties |
|---|---|---|
| Service | `6a1e0001-4c7d-4b4e-9a2b-3c8f1d2e5a01` | primary, advertised |
| CMD characteristic (watch to HA) | `6a1e0002-4c7d-4b4e-9a2b-3c8f1d2e5a01` | write (with response), encrypted + authenticated link required |
| EVT characteristic (HA to watch) | `6a1e0003-4c7d-4b4e-9a2b-3c8f1d2e5a01` | notify, encrypted + authenticated link required to subscribe |

The advertisement carries the 128-bit service UUID; the local name `HA-Watch`
goes in the scan response. The characteristics need a bonded, encrypted link
(see above); the application-layer MAC below additionally binds every command
to the per-watch key and the session.

Only one central can be connected at a time on the tested controller: while a
watch is connected the advertisement is not visible to others. HA therefore
drops a connection that sends no authenticated frame for `idle_timeout`
seconds (default 30), and the watch disconnects as soon as the app closes.

## Watch to HA: command frames (CMD writes)

```
byte 0        op
byte 1        ctr8    low 8 bits of the session counter (authenticated ops only)
bytes 2..n-5  payload (max 14 bytes)
last 4 bytes  tag = HMAC-SHA256(key, nonce || ctr_be32 || op || payload)[0:4]
```

`HELLO` is the only unauthenticated op and has no `ctr8` or tag.

| op | name | payload | HA reply |
|---|---|---|---|
| 0x01 | HELLO | `version u8` (= 2), `flags u8` (bit 0: watch has a key) | KEY if the watch has none, then CHALLENGE; or RESULT |
| 0x02 | LIST | (none) | ENTITY per exposed entity, then LIST_END |
| 0x03 | GET | `idx u8` | ENTITY |
| 0x04 | ACTION | `idx u8, action u8, arg...` | RESULT, then ENTITY with the new state when it changes |
| 0x05 | BATTERY | `percent u8, charging u8` | RESULT |
| 0x07 | BYE | (none) | HA disconnects |

ACTION codes (the first eight equal hasscontrol's `Client.ENTITY_ACTION_*`):

| code | meaning | HA service |
|---|---|---|
| 0 | turn on | `<domain>.turn_on` (scene/script: `turn_on`) |
| 1 | turn off | `<domain>.turn_off` |
| 2 | lock | `lock.lock` |
| 3 | unlock | `lock.unlock` |
| 4 | close | `cover.close_cover` / `valve.close_valve` |
| 5 | open | `cover.open_cover` / `valve.open_valve` |
| 6 | cover toggle | `cover.toggle` / `valve.toggle` |
| 7 | press | `button.press` / `input_button.press` |
| 0x10 | select option | `select.select_option` / `input_select.select_option`; arg `option_index u8` |
| 0x11 | set value | `number.set_value` / `input_number.set_value`; arg `float32 big-endian` |

HA only accepts an action that fits the entity's domain, and only for
entities carrying the configured label (default `garmin`). Everything else is
rejected with status `NOT_ALLOWED`.

### Session and replay protection

1. On connect the watch writes `HELLO`.
2. HA answers `CHALLENGE` with a fresh random 8-byte `nonce` for this
   connection and resets the session counter to 0.
3. Every later frame uses the next counter value (1, 2, 3, ...). The full
   32-bit value is covered by the tag; only its low byte is sent. HA
   reconstructs it as the smallest value greater than the last accepted one
   with that low byte, and accepts it only if it is at most 32 ahead, so a
   lost write does not wedge the session but an old frame cannot be replayed.
4. `key` is the 16-byte per-watch key HA issued with MSG_KEY. On BAD_AUTH
   the watch drops its key and sends HELLO without the key flag.

HA to watch messages are not authenticated (a spoofed state display is the
worst outcome); commands are.

## HA to watch: messages (EVT notifications)

Messages are split into fragments of at most 20 bytes:

```
byte 0     header: bit 7 = last fragment, bits 0..6 = message sequence (mod 128)
bytes 1..  up to 19 bytes of message data
```

Fragments of one message are sent back to back; the watch appends data until
it sees the last-fragment bit.

| type | name | body |
|---|---|---|
| 0x81 | CHALLENGE | `nonce[8], version u8, entity_count u8` |
| 0x82 | ENTITY | `idx u8` then TLV fields |
| 0x83 | LIST_END | `count u8` |
| 0x84 | RESULT | `ctr8 u8, status u8` |
| 0x85 | KEY | `key[16]` (answer to HELLO without the key flag) |

ENTITY TLV fields are `tag u8, len u8, UTF-8 bytes` (each string cut to 255
bytes):

| tag | field | source |
|---|---|---|
| 1 | entity_id | `entity_id` |
| 2 | state | `state` |
| 3 | friendly_name | `attributes.friendly_name` |
| 4 | unit | `attributes.unit_of_measurement` |
| 5 | device_class | `attributes.device_class` |
| 6 | icon | `attributes.icon` |
| 7 | options | `attributes.options` joined with 0x1F |
| 8 | min | `attributes.min` (decimal text) |
| 9 | max | `attributes.max` |
| 10 | step | `attributes.step` |

Fields that are absent are omitted. Entity indices are stable for the
connection (entities sorted by entity_id when the connection authenticates).

RESULT status codes: 0 OK, 1 BAD_AUTH, 2 BAD_INDEX, 3 NOT_ALLOWED,
4 SERVICE_ERROR, 5 BAD_FRAME (also HELLO with another protocol version),
6 NO_SESSION, 7 NOT_PAIRED (HELLO from an unknown device), 8 NOT_APPROVED
(HELLO from a bonded watch still waiting for approval in HA). HELLO answers
use ctr8 0.

After authentication HA also pushes an ENTITY message whenever an exposed
entity changes state, so the watch does not need to poll.
