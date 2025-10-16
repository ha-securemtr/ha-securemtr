# Secure Controls / Beanbag — WebSocket Function Catalog (v1.2.0)

**Scope:** Complete, confirmed message catalog for the `com.secure.heatConnect` app’s cloud WebSocket.
Two immersion circuits are exposed:
- **slot 1** → primary immersion (**state block `SI:33`**), on/off via mode write.
- **slot 2** → boost immersion (**state block `SI:16`**), timed boost & feature toggle.

## Connection (confirmed)
- WS URL: `wss://app.beanbag.online/api/TransactionRestAPI/ConnectWebSocket`
- Subprotocol: `BB-BO-01` (negotiated in the opening handshake).  
- Required upgrade headers: `Authorization: Bearer {token}` · `Session-id: {sessionId}` · `Request-id: 1`

**Envelopes**
- Client request:
  ```json
  { "V":"1.0","DTS":{epoch},"I":"{sessionId}-{rand32}","M":"Request",
    "P":[ { "GMI":"{gatewayID}","HI":<op>,"SI":<sub> }, [ /* args */ ] ] }
  ```
- Server reply: `{ "V":"1.0","DTS":{epoch},"I":"...","R": <payload_or_0> }`
- Server notify:
  ```json
  { "V":"1.0","M":"Notify","DTS":{epoch},
    "P":[ { "GMI":"...","SI":<block>,"HI":4 }, [ <slot>, { "I":<item>,"V":<val>,"OT":<op>,"D":<aux> } ] ] }
  ```

## Refresh burst (read‑only, observed order)
`zones.read (49/11)` → `time.tick (2/103,[DTS])` → `schedules.summary (5/1)` → `device.metadata.read (17/11)` → `device.config.read (14/11)` → `state.read (3/1)`.

---

## Function Table (complete)

| Name | HI/SI | Dir | Slot | Args (second tuple) | Response (`R`) | Notify (block/items) | Notes |
|---|---|---|---:|---|---|---|---|
| **zones.read** | 49/11 | C→S | — | — | `[{ZT,CN,ZN,ZNM},…]` | — | Topology: two zones observed. |
| **time.tick** | 2/103 | C→S | — | `[ <DTS> ]` | `0` | — | Keepalive/clock tick used in bursts. |
| **schedules.summary** | 5/1 | C→S | — | — | `{"V":[{"I":1,"SI":16,"V":[{"ALI","OR","AB","TS"}…]}]}` | — | Program overview (aliases/last‑updated). |
| **device.metadata.read** | 17/11 | C→S | — | — | e.g., `{"BOI":1,"UI":1,"N":"<friendlyName>","SN":"<serialNumber>","FV":"<firmwareVersion>","MD":2,"AS":-1,"LUT":...}` | — | Device info. |
| **device.config.read** | 14/11 | C→S | — | — | `{"V":[{"BOI":1,"SI":16,"V":[{"CI":1,"CV":50},{"CI":2,"CV":300},{"CI":4,"CV":1}]}]}` | — | Key/value config entries. |
| **state.read** | 3/1 | C→S | — | — | `{"V":[{"I":1,"SI":33,…},{"I":2,"SI":16,…}]}` | — | Live state for primary & boost blocks. |
| **energy.history.read** | 9/36 | C→S | — | `[ <windowIndex> ]` | `[{ "I":0, "D":[ {T, OP, BP, OS, OA, BS, BA}, … ]}]` | — | `OP/BP` **kWh**; `OS/OA/BS/BA` **minutes** (÷60 → hours). |
| **program.read.primary** | 22/17 | C→S | — | `[ 1 ]` | `[{ "I":1, "D":[ {"O","T"}, … ]}]` | — | **Index 1 = primary schedule**. |
| **program.read.boost** | 22/17 | C→S | — | `[ 2 ]` | `[{ "I":2, "D":[ {"O","T"}, … ]}]` | — | **Index 2 = boost schedule**. |
| **program.write.primary** | 21/17 | C→S | — | `[ { "I":1, "D":[ {"O","T"}, … ] } ]` | `0` | (opt) readback | Daily: `ON 04:45 (O:285) → OFF 07:45 (O:465)`; Tue add `ON 19:45 (O:1185) → OFF 20:15 (O:1215)`. |
| **program.write.boost** | 21/17 | C→S | — | `[ { "I":2, "D":[ {"O","T"}, … ] } ]` | `0` | (opt) readback | Example: Sun `ON 01:45 (O:105) → OFF 08:45 (O:525)`. |
| **mode.write.primary** | 2/15 | C→S | 1 | `[ 1, { "I":6, "V":0|2 } ]` | `0` | `SI:33` / `{I:6,V:0|2}` | `0=Off`, `2=On`. |
| **boost.timed.start** | 2/16 | C→S | 2 | `[ 2, { "D":<minutes>, "I":4, "OT":2, "V":0 } ]` | `0` | `SI:16` / `{I:4,V:1,OT:2,D:<min>}`, `{I:9,V:<endMin>}`, `{I:10,V:0}` | End time (`I:9`) is minutes since midnight. |
| **boost.timed.stop** | 2/16 | C→S | 2 | `[ 2, { "D":0, "I":4, "OT":2, "V":0 } ]` | `0` | `SI:16` / `{I:4,V:0,D:0}`, `{I:9,V:<baseline>}`, `{I:10,V:0}` | Cancels active boost. |
| **boost.timed.toggle** | 2/16 | C→S | 2 | `[ 2, { "I":27, "V":0|1 } ]` | `0` | `SI:16` / `{I:27,V:0|1}` | Feature toggle; **manual timed boost still works**. |

### Schedule model (confirmed)
- Weekly arrays are **Monday → Sunday** (7 day‑blocks).
- **Index 1 = primary**, **Index 2 = boost**.
- `O` = **minutes since midnight**. `T` codes: `1=ON`, `0=OFF`, `255=sentinel/unused` (`{O:65535,"T":255}` padding).

### Item quick‑reference (from `state.read`/Notifies)
- **Block `SI:33` (slot 1 — primary):** `I:6` mode (`0/2`), `I:10` status (0/1), others (`I:5`,`I:9`,`I:28`,`I:30`) observed.
- **Block `SI:16` (slot 2 — boost):** `I:4` active/countdown, `I:9` end‑time minutes, `I:10` status, `I:27` timed‑boost toggle.

---

## Versioning
This catalogue: **v1.2.0** — complete, synchronized with OpenAPI + AsyncAPI v1.2.0 (full payload examples for all WS functions).
