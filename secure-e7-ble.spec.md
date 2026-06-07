# Secure E7 (E7 Plus) — BLE Protocol Specification

A complete, implementation-ready description of the local Bluetooth Low Energy (BLE)
protocol spoken by Secure Meters' E7 / E7 Plus hot-water & heating controllers.

The term **client** below means the host issuing commands (e.g. a phone or hub); the
**device** (or **controller**) is the E7. Normative protocol facts (ids, wire keys,
encodings, framing) are stated directly; non-normative implementation guidance is collected
in **§14 Implementation notes**.

---

## Table of contents

1. [Overview & terminology](#1-overview--terminology)
2. [GATT layer (UUIDs)](#2-gatt-layer-uuids)
3. [Packetization](#3-packetization)
4. [Framing & encryption](#4-framing--encryption)
5. [Four-pass authentication](#5-four-pass-authentication)
6. [JSON-RPC envelope](#6-json-rpc-envelope)
7. [Connection lifecycle](#7-connection-lifecycle)
8. [Timeouts, pacing & retries](#8-timeouts-pacing--retries)
9. [Identifier catalog (services, handlers, events, errors)](#9-identifier-catalog)
10. [Value enums & characteristics](#10-value-enums--characteristics)
11. [Data structures (DTOs)](#11-data-structures-dtos)
12. [Request catalog](#12-request-catalog)
13. [Commissioning & device discovery](#13-commissioning--device-discovery)
14. [Implementation notes & gotchas](#14-implementation-notes--gotchas)

---

## 1. Overview & terminology

The controller exposes a **UART-style GATT service** (a write characteristic + a notify
characteristic) with custom UUIDs. Over that link the client speaks a **packetized,
length-prefixed, AES-encrypted, JSON-RPC** protocol. Every operation is one JSON-RPC
*Request* addressed to a **service** (`SI`) + **handler** (`HI`) on a **gateway**
(`GMI`), returning one *Response*.

| Term | Meaning |
|---|---|
| **Gateway / receiver** | The E7 controller itself. Identified by its MAC. |
| **GMI** | Gateway MAC Id — the MAC as a **decimal** string, `str(int(mac_hex, 16))`. |
| **SI** | Service Id — which service (e.g. HotWater = 16). See §9. |
| **HI** | Handler Id — which operation within a service (e.g. WriteCharacteristics = 2). |
| **BOI** | Business Object Instance id — the instance of a service to address (a "channel"/zone). Returned as `"I"` inside `GetAllServiceValues`. |
| **Characteristic** | A single readable/writable value within a service instance (`{I,V,OT,D}`). |
| **OT** | Override Type (e.g. Advance = 2 → a manual/timed boost). |
| **Zone** | E7 Plus has 2 channels: a **primary** heating/timer zone and a **hot-water/boost** zone. |

The E7 Plus is a **2-channel** device, hardware type **65**.

---

## 2. GATT layer (UUIDs)

| Role | UUID | Properties |
|---|---|---|
| UART service | `b973f2e0-b19e-11e2-9e96-0800200c9a66` | — |
| **RX characteristic** (client → device) | `da73f3e1-b19e-11e2-9e96-0800200c9a66` | Write **with response** |
| **TX characteristic** (device → client) | `e973f2e2-b19e-11e2-9e96-0800200c9a66` | Notify |
| CCCD descriptor | `00002902-0000-1000-8000-00805f9b34fb` | — |

- The client writes request packets to **RX** and receives response packets as
  notifications on **TX**.
- To enable notifications: subscribe to **TX**, then write `0x01 0x00`
  (enable-notifications value) to the TX characteristic's CCCD descriptor.
- Other standard UUIDs (DIS `180a`, firmware-rev `2a26`, tx-power `1804`/`2a07`) are
  declared but unused on the data path.

---

## 3. Packetization

A fully-framed, encrypted message (see §4) is split into BLE packets:

- **Packet size:** 19 payload bytes max per packet (`PACKET_SIZE = 19`). On-air each
  packet is `[index byte] + ≤19 data bytes` ⇒ **≤ 20 bytes** (fits default ATT MTU 23 − 3).
- **Index byte:**
  - Non-final packets are numbered **1, 2, 3, …** (1-based).
  - The **final packet uses index `255` (0xFF)**, regardless of its sequence number.
- **Split:** `total_packets = floor(len / 19) + 1`. Packet *k* (1-based) carries
  `bytes[(k-1)*19 : k*19]`; the last carries the remainder. If `len` is an exact
  multiple of 19, the final `0xFF` packet carries an empty data slice but is still sent.
- **Reassembly (receive):** collect packets into a map keyed by index byte; concatenate
  in **ascending index order** (so `0xFF` sorts last — correct for ≤254 normal packets).
  Completion is signalled by arrival of the **`0xFF`** packet. Duplicate indices are ignored.

> A single message therefore supports up to 254 leading packets + 1 final = up to
> ~4845 framed bytes. In practice messages are far smaller.

---

## 4. Framing & encryption

Order of operations when **sending** an application (JSON) payload:

1. **Serialize** the JSON-RPC request to a UTF-8 string.
2. **Length prefix:** prepend 2 bytes, **big-endian**, equal to the **count of UTF-16 code
   units** in the JSON string (not the UTF-8 byte count). For pure-ASCII JSON (the normal
   case) these are identical. Frame = `[len_hi, len_lo] + utf8_payload`.
3. **Zero-pad** the frame to a multiple of **16 bytes** (append `0x00` bytes). *(Not PKCS#7.)*
4. **Encrypt** the padded frame with **AES-128 / ECB / NoPadding** using the 16-byte
   session key (see §5). *(Encryption applies once the connection is authenticated; the
   4-pass handshake frames themselves are sent un-encrypted at the transport layer — see §5.)*
5. **Packetize** (§3) and write packet-by-packet to RX.

When **receiving**:

1. Reassemble packets (§3) into the encrypted byte stream.
2. **Decrypt** with AES-128/ECB.
3. Read the 2-byte big-endian **length prefix**, take exactly that many following bytes as
   the payload (this strips the zero padding), decode UTF-8 → JSON.

`length_from_bytes(b0, b1) = ((b0 & 0xFF) << 8) | (b1 & 0xFF)`.

> **Crypto summary:** AES-128, ECB, no padding, zero-pad-to-16, key = the 16-byte
> device BLE key (§13.2). No IV. No other ciphers are used on the BLE link.

---

## 5. Four-pass authentication

Immediately after notifications are enabled (and before any JSON-RPC), the client performs a
challenge/response that proves both sides hold the shared 16-byte BLE key and arms
transport encryption. The handshake **frame** is 20 bytes:
`[type, ack, valueLen, 0x00] + value(16)`.

Each frame travels the **normal transport** (§3/§4): it is carried with the same **2-byte
length prefix** and packetized, so the framed payload is **22 bytes** (2-byte prefix +
20-byte frame). It is **NOT AES-encrypted** — the session key is not armed until the
handshake succeeds. (The prefix value is not meaningful for these binary frames; the
receiver simply strips the 2 prefix bytes before parsing the frame header. Observed on a
real device as `framed_len=22, payload_len=20`.)

| Pass | Dir | Bytes |
|---|---|---|
| 1 | client → dev | `[0x01, 0x00, 0x10, 0x00]` + `appRandom` (16 random bytes) |
| 2 | dev → client | type `0x02`, ack `0x00`, len `0x10` + `meterRandom` (16 bytes) |
| 3 | client → dev | `[0x03, 0x00, 0x10, 0x00]` + `AES_ECB_encrypt(key, meterRandom)` |
| 4 | dev → client | type `0x04`, ack `0x00`, len `0x10` + `AES_ECB_encrypt(key, appRandom)` |

- **Verification:** after pass 4 the client computes `AES_ECB_decrypt(key, value)` and asserts
  it equals the `appRandom` it sent in pass 1. Match ⇒ authenticated; the session key is
  now armed for all subsequent RPC traffic. Mismatch ⇒ key error, disconnect.
- **ACK / error bytes:** `0x00` = success, `0x01` = invalid message format, `0x02` = key
  mismatch. A frame of **type `0x05`** is an explicit rejection.
- **Receive:** strip the 2-byte length prefix, then parse the 20-byte frame by its header
  (`type, ack, valueLen` then `valueLen` value bytes) — do not assume a fixed 20-byte read.
- Nonces are 16 bytes; the header length byte `0x10` = 16.

---

## 6. JSON-RPC envelope

### Request (client → device)

```json
{
  "V": "1.0",
  "DTS": 1717600000,
  "I": "<unique-request-id>",
  "M": "Request",
  "P": [ { "GMI": "<decimal-mac>", "HI": <handlerId>, "SI": <serviceId> }, <argsArray?> ]
}
```

| Key | Meaning |
|---|---|
| `V` | RPC version, always `"1.0"`. |
| `DTS` | Unix timestamp in **seconds** (`currentTimeMillis()/1000`). |
| `I` | Request id. Must be unique; the device **echoes it** in the response. Any unique string works; a conventional form is `"<sessionId>-<random int>"` (the random int may be negative). |
| `M` | Method, always `"Request"` for gateway calls. |
| `P[0]` | The service header: `{GMI, HI, SI}`. |
| `P[1]` | The positional arguments array (omitted entirely if the call has no args). |

`GMI` is the MAC as a **decimal** string: `str(int("54FEEB8A327E", 16))`.

### Response — success (device → client)

```json
{ "V": "1.0", "I": "<echoed-request-id>", "R": <result>, "DTS": 1717600001 }
```

- Match the response to its request by the echoed `"I"`. The result is in `"R"`.
- A common result shape for reads is `{ "V": [ ... ] }` (a list of value objects);
  many writes return `0` / `"0"` / `null` as a bare acknowledgement.

### Response — error (device → client)

```json
{ "I": "<id>", "E": { "C": <code>, "M": "<message>" } }
```

`"E"` present (and `"R"` absent) ⇒ failure; `C` is the error code (see §9.4 / §9.5).
A client that synthesises an error **locally** (e.g. no-connection 20003, invalid-request
20004, invalid-response 20005, or a generic exception with `C = -1`) conventionally uses an
*Application-Error* wrapper:
`{"C":-1,"M":"Application Error","D":{"ES":<serviceId>,"EC":<errorCode>,"ET":-1,"EA":{"Sender":"","ErrorMsg":"<msg>"}}}`.
(A request timeout is generated this way too, with `EC = 20001` — but timeouts are driven
by the request timer, not the device.)

### Notify / event (device → client, unsolicited)

```json
{ "V":"1.0", "M":"Notify", "P":[ {"GMI":<mac>,"SI":<serviceId>,"HI":<eventId>}, [ <eventPayload...> ] ] }
```

A message whose `"I"` is empty/absent is a notification. Dispatch key = `"<SI>_<eventId>"`
(e.g. `203_1` gateway-connected). Event ids are listed in §9.3. Clients may ignore
notifications and rely on polling.

### Nested JSON on the wire

The `P` array contains **real nested JSON objects** (the service header and the args), not
strings. Emit them directly as shown above. (If a serializer double-encodes nested objects
as escaped strings, un-escape `"{` → `{`, `\"` → `"`, `}"` → `}` before transmission so the
device receives genuine objects.)

---

## 7. Connection lifecycle

```
scan  →  connect (GATT)  →  discover services  →  enable TX notifications
      →  4-pass auth  →  READY  →  [request → full response]*  →  disconnect
```

1. **Scan** (LE scan; stop after ~10 s) — identify the target device (§13.5).
2. **Connect** GATT (`autoConnect = false`). On a GATT *error* status, retry up to
   3 times; a clean disconnect (status 0) is not retried.
3. **Discover services.**
4. **Enable TX notifications** (write CCCD).
5. **4-pass auth** (§5). On success the link is `READY`; on failure, disconnect.
6. **Issue requests** — strictly **one request at a time**: the next request is not sent
   until the current one's full (`0xFF`-terminated) response arrives or it times out.
   Within a request, packets are sent one-at-a-time, each awaiting its write-ack (§8).
7. **Disconnect** when idle.

The transport is single-flight: a "response-awaited" gate plus a send queue ensure exactly
one in-flight request→response round trip. (A client may track several outstanding request
ids each with its own timeout, but must still drip-feed them to the device serially.)

---

## 8. Timeouts, pacing & retries

| Parameter | Value | Meaning |
|---|---|---|
| Per-request timeout | **30 000 ms** | default deadline for a request's response. |
| Send/receive timeout | **15 000 ms** | per-packet write timeout & response-read window. |
| Inter-packet write delay | **100 ms** | wait **before every packet write**. |
| Write-ack gating | — | each packet is written **with response**; the next packet is sent only after the GATT write completes. |
| Write-busy retry | **10 ms**, ~5 attempts | if a GATT write returns "busy", wait 10 ms and retry, up to ~5 times. |
| GATT connect attempts | **3** | retried on GATT error status. |
| LE scan timeout | **10 000 ms** | auto-stop scan. |

A device that is going to answer does so within ~1–3 s; a dropped request returns nothing
and only the timeout fires. See §14 for more aggressive, field-tuned timeout/retry guidance.

---

## 9. Identifier catalog

Service-header wire keys: `GMI` (gateway MAC, decimal string), `SI` (service id),
`HI` (handler id). A message = *which service* (`SI`) + *which operation* (`HI`).

### 9.1 ServiceType (`SI`)

| Name | SI | | Name | SI |
|---|---|---|---|---|
| None | 0 | | Rule | 28 |
| BBServer | 1 | | SmokeSensor | 29 |
| BBClient | 2 | | FloodSensor | 30 |
| hanmanagement | 11 | | **Timer** *(E7 primary channel)* | 33 |
| Area | 12 | | **DynamicTariff** | 36 |
| Mode | 13 | | ZWaveBridge | 76 |
| Light | 14 | | DeviceSimulator | 77 |
| **Thermostat** | 15 | | SysManager | 101 |
| **HotWater** | 16 | | **WLAN** | 102 |
| **Schedule** | 17 | | **TimeManagement** | 103 |
| HANCertification | 19 | | Button | 104 |
| Appliance | 20 | | mDNS | 105 |
| TemperatureSensor | 21 | | **CommsManager** | 151 |
| HumiditySensor | 22 | | OTAManager | 152 |
| Cooling | 23 | | Monitoring | 153 |
| DoorWindowSensor | 26 | | WAC | 154 |
| COSensor | 27 | | App / BackOffice / BBHANDevice / Owner | 201 / 202 / 204 / 254 |

> On the E7 Plus the **primary heating/timer channel** is service **Timer (33)** and the
> **hot-water/boost channel** is **HotWater (16)**. Older/generic firmware may surface the
> primary channel as **Thermostat (15)** or legacy **Mode (13)** — a robust client resolves
> the primary service as "the first instance whose SI ∈ {33, 15, 13}".

### 9.2 Handler ids (`HI`) by service

**BBServer (SI=1)** — `GetAllServiceDefinition=2, GetAllServiceValues=3, GetAllMaxLastUpdatedTimeStamp=4, GetAllBBAlarm=5, GetHeartBeat=100`

**hanmanagement (SI=11)** — `AddPhysicalDevice=1, CreateBBDevice=2, RemovePhysicalDevice=4, UpdatePhysicalDeviceDetails=6, GetAllPhysicalDevices=7, GetSignalStrength=8, Abort=9, UpgradeDevice=14, GetPhysicalDeviceAttributeValue=16, GetPhysicalDeviceAllAttributeValues=17, AddAndUpdatePhysicalDevice=19, ResetPhysicalDevice=20, ReplacePhysicalDevice=25, LinkExternalDeviceToPhysicalDevice=30, DeLinkExternalDeviceToPhysicalDevice=31, GetAllLinkedExternalDevicesWithPhysicalDevice=32, AddHANDevices=48, GetHANDevicesAddStatus=49, GetBLEKey=50` *(complete enum has more; these are the protocol-relevant ones)*

**Mode (SI=13)** — `AddMode=1, UpdateMode=2, RemoveMode=3, GetAllMode=4, ChangeState=5`

**Schedule (SI=17)** — `UpdateScheduleData=1, GetActiveSchedules=2, SetActiveScheduleId=3, GetSchedules=4, SetWeekEnd=5, GetWeekEnd=6, … SetSecureConnectSchedules=21, GetSecureConnectSchedules=22`

**DynamicTariff (SI=36)** — `setDynamicTariffState=1, getDynamicTariffState=2, getDynamicTariffSchedule=3, setDynamicTariffSchedule=4, setDynamicTariffName=5, getDynamicTariffName=6, setDynamicTariffServiceProvider=7, getDynamicTariffServiceProvider=8, getConsumptionState=9`

**WriteData (generic R/W, used with a target SI)** — `WriteCharacteristics=2, ReadCharacteristics=4, WriteConfiguration=11, ReadConfiguration=13`

**BBDevice (SI=204)** — `GetServiceDefinition=1, WriteCharacteristic=2, ReadCharacteristic=3, UpdateObject=4, RemoveObject=5, GetServiceValue=6, GetServiceValuesByObjectId=7, CancelBoost=8, SetFallbackData=9`

**TimeManagement (SI=103)** — `GetTime=1, SetTime=2, GetTimeZone=3, SetTimeZone=4, ResetTimeZone=5, GetTimeZoneId=6, SetTimeZoneId=7`

**CommsManager (SI=151)** — `SetLogLevel=1, SetWLANAPModeInfo=2, SetWLANStationModeInfo=3, SetOwnerDetails=4, GetOwnerDetails=5, GetPinInfo=6, SetOwnerInReceiver=7, GetOwnerInReceiver=8, GetHeartBeat=100`

**WLAN (SI=102)** — `SetStationModeCredentials=1, GetStationModeSignalLevel=2, StartAccessPointMode=3, StopAccessPointMode=4, SetWifiCredential=5, GetWiFiSignalLevel=6`

**Rule (SI=28)** — `SetOptimalSetting=1, GetOptimalSetting=2, ResetClock=41, GetGasSafetyStatus=42, ChangeGasSafetyMode=43, IsHeatingOn=60`

**SysManager (SI=101)** — `SetLogLevel=1, LeaveSystem=2, ResetGateway=3, AbortResetGateway=4, GetHeartBeat=100`

### 9.3 Event ids (notifications) — selected

- **BBDevice events:** `OnCharacteristicsChanged=4, OnBusinessObjectStateChanged=6, OnBusinessObjectRemoved=7, OnBusinessObjectUpdated=8, onHotWaterAlarmReceived=12`
- **Mode:** `OnModeStateChanged=4`
- **Schedule:** `OnActiveSchedulesChanged=2, OnScheduleSyncToDeviceFailed=4, OnScheduleSyncToDeviceSuccess=5`
- **WLAN:** `OnWlanProgress=1, OnWlanError=2, OnWlanSuccess=3`
- **HANManagement:** `OnPhysicalDeviceAdded=1, OnActionCompleted=3, OnInstallationError=5, OnSetCommissioningStatus=10, …`
- **Gateway link:** `203_1` = connected, `203_2` = disconnected.

### 9.4 Error ids (gateway, per service)

A near-universal base set is shared by most services:
`InternalError=1, InvalidHandlerIdError=2, InvalidParametersError=3, InvalidDataValueError=4, InvalidMasterDataError=5/6, DuplicateDataError=6/7/9/15, InvalidLengthError=7/16, StaleDataError=8, SyncInProgressError=9`.

Notable service-specific codes:

- **BBDevice:** `HANError=10, TimeoutError=11, NoOverrideInProgress=20, HolidayInProgress=21, OutOfRangeError=23`
- **CommsManager:** `NoMoreLocalConnectionsAllowed=5, UnAuthorizedAccessError=7, OwnerMismatch=14, RequestInProgress=15, TimeOutError=16`
- **HANManagement:** `DeviceAlreadyAddedError=7, HANError=10, TimeoutError=11, RequestAlreadyInProgress=17, NoRequestInProgress=19, NoOverrideInProgress=20, SecurityMisMatch=29, SecurityFailed=30`
- **Schedule:** `ScheduleLimitExceed=7`
- **WLAN:** `InvalidStationModeSecurityType=6, StationModeConnectionFailure=7, SSIDNotFound=8, SecurityTypeMismatch=9`

### 9.5 Transport-level error codes

`ERROR_GW_UNKNOWN=-1, ERROR_RECEIVER_KEY_MISMATCH=-2, ERROR_GW_REQUEST_TIMEOUT=20001,
REQUEST_ABORTED=20002, NO_CONNECTION=20003, INVALID_REQUEST=20004, INVALID_RESPONSE=20005`.

`SetOwnerInReceiver` returning **20001** during commissioning is retried (see §13.1).

---

## 10. Value enums & characteristics

### 10.1 DeviceCharacteristics (`I` field of a characteristic)

```
None=0, TargetTemperature=1, CurrentTemperature=2, HeatingState=3, HotWaterState=4,
TimerState=5, Mode=6, AwayModeSetPoint=7, AmbientHumidity=8, ScheduleNextPeriod=9,
ScheduleNextSetPoint=10, FrostSetPoint=11, DoorWindowState=12, COLevel=13, COAlarm=14,
Voltage=15, Current=16, PowerFactor=17, ActivePower=18, ActiveEnergy=19,
ApparentEnergy=20, SmokeAlarm=22, FloodAlarm=23, ScheduleEnableDisable=27,
DST_EnableDisable=28, DeviceLock=30
```

### 10.2 OverrideType (`OT` field)

```
None=0, Permanent=1, Advance=2, DurationBased=3, UntilNextMode=4,
BackUpCurrentTempDueToPowerCycle=5, BackUpCurrentTempDueToHANFailure=6,
DurationBasedFromDevice=7, UntilNextmodeFromDevice=8, PermanentFromDevice=9,
Optimal=10, HeatingDiagnostic=11, GeoFenceEntry=12, GeoFenceExit=13
```

**`OT = Advance (2)` is the manual / timed boost / advance override** — the key value for
hot-water boost.

### 10.3 Other enums

- **HomeAway (Mode characteristic value):** `Off=0, Away=1, Home=2`. "Powered on / home" = **2**, "off" = **0**.
- **HardwareType:** `BB_Gateway=10, Receiver_2_Channel=53, Receiver_4_Channel=54, NewFW_Receiver_2_Channel=62, NewFW_Receiver_4_Channel=63, E7Plus_2_Channel=65`.
- **WiFiSecurityTypes:** `None=1, WPA_PSK=2, WEP=3`.
- **ModeID:** `Sleep=1, Away=2, Relax=3, HotWater=4`. **OverrideTriggerType:** `None=0, UserAction=1, GeoFencing=2`.
- **Role:** `Unknown=0, Owner=1, User=2, Guest=3, Installer=4`. **AccessLevel:** `None=0, ViewOnly=1, UpdateOnly=2, All=3`.
- **MeasurementUnitType:** `Celsius=1, Fahrenheit=2, State=3, Unit=4, Percent=5, …, Watt=11, KWH=12, KVAH=13`.
- **HANType:** `ZWave=1, ZigBee=3, WiFi=4, BLE=5`. **BBDeviceType:** `Light=1, BinarySensor=2, Appliance=3, Thermostat=4, Meter=5, HotWater=6, …`.

### 10.4 Value encodings

- **Temperatures:** integer tenths of °C (e.g. `215` = 21.5 °C).
- **Durations (`D`):** minutes. `255` is the conventional "cancel / until-cancelled" sentinel for holds.
- **Energy (ActiveEnergy, I=19):** raw value ÷ 1000 = kWh.
- **Schedule slot minutes:** minute-of-day 0–1439; sentinel `65535` = empty slot.
- **Timestamps:** Unix epoch seconds (`DTS`, schedule "next period", etc.).

---

## 11. Data structures (DTOs)

All structures are JSON; the keys below are the **exact wire keys**.

### 11.1 Characteristic (read value **and** write payload)

```jsonc
// CharacteristicDTO
{ "I": <charId>, "V": <value:long>, "OT": <overrideType?>, "D": <durationMinutes?> }
```
`OT` and `D` are nullable/omittable. This object is both the element of a service
instance's `V[]` (read) and the second argument of a characteristic write.

### 11.2 GetAllServiceValues response

```jsonc
// result "R": { "V": [ ServiceValuesDTO, ... ] }
// ServiceValuesDTO
{ "I": <BOI>, "S": <state>, "SI": <serviceId>, "V": [ CharacteristicDTO, ... ] }
```
For the E7 Plus a snapshot typically returns two instances: `SI:33` (primary, BOI 1) with
characteristics like `[5,6,9,10,28,30]`, and `SI:16` (hot-water, BOI 2) with `[4,9,10,27]`.

### 11.3 Configuration

```jsonc
// ConfigurationDTO
{ "CI": <configId>, "CV": <configValue> }
// AttributesDTO (configuration container)
{ "BOI": <boi>, "SI": <serviceId>, "V": [ ConfigurationDTO, ... ] }
```

### 11.4 Weekly schedule (Secure-Connect schedules)

```jsonc
// ScheduleDTO
{ "I": <zoneIndex/BOI>, "D": [ DaySchedule, ... ] }   // exactly 42 entries: 7 days × 6 slots
// DaySchedule
{ "O": <minuteOfDay 0..1439>, "T": <type> }
```
- `D` is a **flat list of 7 days × 6 transitions = 42** objects, day 0 (Monday) first, 6 slots per day.
- Each transition: `O` = minute-of-day, `T` = transition type: **`1` = ON, `0` = OFF**.
- Up to 3 ON + 3 OFF transitions per day; sort by minute (ON before OFF on ties).
- **Unused slot sentinel:** `O = 65535`, `T = 255`.

### 11.5 Consumption (dynamic tariff)

```jsonc
// result "R": [ DynamicScheduleDTO ]   (take element [0])
// DynamicScheduleDTO
{ "I": <recordId>, "D": [ DynamicDaySchedule, ... ] }   // ~7 day rows
// DynamicDaySchedule
{ "T": <epochOrIndex>, "OP": <offPeak>, "BP": <boost>, "OS": <offPeakSched>,
  "BS": <boostSched>, "OA": <offPeakActual>, "BA": <boostActual> }
```
Field meanings: `T` = report day (epoch seconds); `BA`/`OA` = **actual**
runtime in **minutes** (hours = `/60`); `BS`/`OS` = **scheduled** minutes; `BP`/`OP` =
**energy in Wh** (kWh = `/1000`). Despite the names ("…Current"), `OP`/`BP` carry energy.

### 11.6 Commissioning payloads

```jsonc
// CommissioningDeviceDetails (HAN device list)
{ "ZT": <zoneType>, "CN": <channel>, "DT": <deviceType>, "ZN": <zoneNumber>,
  "MC": <macId>, "SN": "<serial>", "ZNM": "<zoneName>", "RHT": <receiverHwType>, "CT": <commandType> }
// ThermostatDetails (device details)
{ "P": <physicalDeviceId>, "N": "<name>", "L": "<lat>", "LO": "<lon>", "FT": <fuelType> }
// OwnerDetails
{ "UEA": "<email>", "SN": "<systemName>", "MN": "<mobileOrNull>" }
// WifiCredentialsDTO
{ "ID": "<ssid>", "P": "<password>", "S": <securityType>, "T": <activationEpochMs> }
// WiFiConnectionStatusDTO (response)
{ "S": <status>, "V": <value> }   // S: 0=ok,1=ssid-not-found,2/3=cannot-connect,4=security-mismatch,5=retry
```
`CT` (commandType): `0` = add, `255` = rename. `RHT` = 65 for E7 Plus.

### 11.7 Notification / alarm payloads

```jsonc
// CharacteristicNotificationValues (pushed update)   note OR (not OT) and T
{ "I": <charId>, "V": <value>, "OR": <overrideType?>, "T": <time?> }
// AlarmResponse / AlarmsDTO
{ "I": <boi>, "SI": <serviceId>, "V": [ { "ALI": <alarmId>, "OR": <status>, "TS": <ts>, "AB": <by> } ] }
```

### 11.8 Persisted characteristic with override timing (gateway-returned)

```jsonc
// BBCharacteristics
{ "I":<id>, "V":<val>, "OT":<overrideType>, "D":<durationMin>,
  "ST":<startEpochSec>, "ET":<endEpochSec>, "GT":<gatewayRefEpochSec>, "LUT":<lastUpdated> }
```
`ST/ET/GT` let a client compute remaining override time: `remaining = ET − GT`.

> **Key-letter quick reference:** `I`=id/BOI · `V`=value or value-list · `SI`=serviceId ·
> `OT`=overrideType (`OR` in notifications/alarms) · `D`=duration **or** daySchedule list ·
> `O`/`T`=schedule slot minute/type · `CI`/`CV`=config id/value · `BOI`=business-object id ·
> `MC`/`SN`/`ZT`/`ZN`/`ZNM`/`CN`/`DT`/`RHT`/`CT`=device descriptors ·
> `T`/`OP`/`BP`/`OS`/`BS`/`OA`/`BA`=consumption fields · `GMI`/`HI`/`SI`=service header.

---

## 12. Request catalog

Every operation below is one JSON-RPC Request: `P = [ {GMI, HI, SI}, args ]`.
"Write" = handler **2** (WriteCharacteristics) on the target service; args
`[BOI, CharacteristicDTO]`. "Read snapshot" = handler **3** on BBServer (SI 1).

### 12.1 Reads

| Operation | SI | HI | Args | Result |
|---|---|---|---|---|
| **Get all service values** (state snapshot) | 1 | 3 | `null` | `{V:[ServiceValuesDTO…]}` — all instances + characteristics |
| **Get all alarms** | 1 | 5 | `null` | `{V:[AlarmResponse…]}` |
| **Get physical-device attributes** | 11 | 17 | `null` | `PhysicalDeviceAllAttributeValues` |
| **Get HAN devices / add-status** | 11 | 49 | `null` *(or `[null]`)* | `[CommissioningDeviceDetails…]` |
| **Get owner details** | 151 | 5 | `null` | `OwnerDetails` |
| **Get weekly schedule** | 17 | 22 | `[zoneIndex/BOI:int]` | `[ScheduleDTO]` → `[0].D` (42 slots) |
| **Get consumption state** | 36 | 9 | `[recordSelector:int]` | `[DynamicScheduleDTO]` → `[0].D` |
| **Get optimal setting** (service = Rule) | 28 | 2 | `[]` | optimal config |
| **Get Wi-Fi signal/status** (`GetWiFiSignalLevel`) | 102 | 6 | `null` | signal-level / `WiFiConnectionStatusDTO` |

### 12.2 Writes (characteristic, HI = 2)

| Operation | SI | Args `[BOI, CharacteristicDTO]` |
|---|---|---|
| **Turn controller ON** (mode = home) | 15 *(primary heating)* / 33 *(device-off screen)* | `[boi, {"I":6,"V":2}]` |
| **Turn controller OFF** | 15 / 33 | `[boi, {"I":6,"V":0}]` |
| **Start / extend timed boost** | 16 | `[boi, {"I":4,"V":0,"OT":2,"D":<minutes>}]` |
| **Stop timed boost** | 16 | `[boi, {"I":4,"V":0,"OT":2,"D":0}]` |
| **Enable / disable timed-boost (schedule)** | 16 | `[boi, {"I":27,"V":1|0}]` |
| **"Start now" (apply next scheduled set point)** | 15 | `[boi, {"I":1,"V":<nextSetPoint>}]` |
| **Set zone target temperature (permanent)** | 15 | `[boi, {"I":1,"V":<°C×10>,"OT":1,"D":0}]` |
| **Hold temperature for a duration** | 15 | `[boi, {"I":1,"V":<°C×10>,"OT":3,"D":<min>}]` *(D=255 cancels)* |
| **Set away temperature** | 15 | `[1, {"I":7,"V":<°C×10>}]` |

> Typical boost presets are **30 / 60 / 120** minutes; the protocol accepts any positive
> minute value. "Boost active" is detected by reading the HotWater `I:4` characteristic and
> checking **`OT == 2`**; remaining duration is its `D`.
>
> **Mode-write BOI:** the on/off (Mode `I:6`) write is commonly issued with a **hardcoded
> BOI of `1`** (on Thermostat 15 or Timer 33). A robust client should instead resolve the
> primary BOI from the snapshot (§13.3), which is more general; BOI 1 is simply the primary
> instance.

### 12.3 Writes (configuration, HI = 11)

`[BOI, {"CI":<configId>,"CV":<value>}]` to the target SI — used for min/max temperature,
optimum start/stop, heat mode, etc.

### 12.4 Schedule write

| Operation | SI | HI | Args |
|---|---|---|---|
| **Set weekly schedule** | 17 | 21 | `[ScheduleDTO]` = `[{"I":zone,"D":[42 × {O,T}]}]` |

### 12.5 Commissioning / management

| Operation | SI | HI | Args |
|---|---|---|---|
| Set time | 103 | 2 | `[epochSeconds:long]` |
| Set timezone id | 103 | 7 | `[timeZoneId:int]` |
| Update device details | 11 | 6 | `[ThermostatDetails]` |
| Set owner in receiver | 151 | 7 | `[OwnerDetails]` → owner token |
| Get BLE key | 11 | 50 | `null` → base64 key |
| Add / commission HAN devices | 11 | 48 | `[CommissioningDeviceDetails…]` |
| Rename zone | 11 | 48 | `[CommissioningDeviceDetails{…,CT:255}]` |
| Remove / de-link device | 11 | 31 | `[CommissioningDeviceDetails]` |
| Set Wi-Fi credentials | 102 | 5 | `[WifiCredentialsDTO]` |
| Set optimal setting | 28 | 1 | `[value:int]` |

> Each commissioning step should tear down the BLE connection on failure and stop the
> sequence.

---

## 13. Commissioning & device discovery

### 13.1 Fresh-commission sequence (ordered)

Each step is one Request; proceed only on success.

1. **Set timezone id** — `SI 103, HI 7`, `[timeZoneId]`.
2. **Set time** — `SI 103, HI 2`, `[epochSeconds]`.
3. **Update device details** — `SI 11, HI 6`, `[ThermostatDetails{P:1,N,L,LO,FT}]`.
4. **Set owner in receiver** — `SI 151, HI 7`, `[OwnerDetails{UEA,SN,MN}]` → owner token.
   **Retry rule:** if this returns error **20001**, retry up to **5 times** (15 s timeout each).
5. **Get BLE key** — `SI 11, HI 50`, `null` → base64 16-byte key (persist it).
6. *(optional)* **Set Wi-Fi credentials** — `SI 102, HI 5`, `[WifiCredentialsDTO]`.
7. **Add HAN devices** — `SI 11, HI 48`, `[CommissioningDeviceDetails…]`.

**Already-commissioned fast path:** try **Get BLE key** first; if a key comes back, you are
done (no ownership change). Otherwise do **Set owner** (with the 20001 retry) then **Get BLE key**.

For the E7 Plus 2-channel device the default HAN device list is:

```jsonc
[ {"ZT":1,"CN":1,"DT":0,"ZN":0,"MC":0,"SN":"","ZNM":"Hot Water","RHT":65,"CT":0},
  {"ZT":2,"CN":2,"DT":0,"ZN":0,"MC":0,"SN":"","ZNM":"Timer","RHT":65,"CT":0} ]
```

### 13.2 The BLE key

- Obtained via **Get BLE key** (`SI 11, HI 50`, no args). The device returns a **base64
  string** that decodes to **16 bytes** (AES-128).
- It is the key for (a) the 4-pass challenge and (b) AES-ECB encryption of every framed RPC
  thereafter. No derivation/KDF — store the base64 form, decode to 16 raw bytes for crypto.

### 13.3 Characteristic semantics (definitive)

| Service (SI) | Char `I` | Meaning | Encoding |
|---|---|---|---|
| Timer (33) / Thermostat (15) / Mode (13) | 6 (Mode) | System power / mode | `0` off, `2` home/on (HomeAway) |
| Timer (33) | 5 (TimerState) | Timer channel state | device-specific |
| Timer (33) | 19 (ActiveEnergy) | Primary cumulative energy | kWh = V/1000 |
| HotWater (16) | 4 (HotWaterState) | Hot-water on/off + **boost** | V: 0 off / 1 on; **OT=2 (Advance)=boost**, D=minutes |
| HotWater (16) | 27 (ScheduleEnableDisable) | Timed-boost / schedule enable | 1 enabled / 0 disabled |
| HotWater (16) | 19 (ActiveEnergy) | Boost-zone cumulative energy | kWh = V/1000 |
| HotWater (16) | 10 (ScheduleNextSetPoint) | Next scheduled state | 1 on / 0 off |
| HotWater (16) | 9 (ScheduleNextPeriod) | Time of next change | epoch (a max value = none) |
| any | 30 (DeviceLock) | Schedule locked | 1 locked |

**Zone resolution:** match services by `SI`; the per-instance `"I"` is the **BOI** you
address in writes. Primary/mode BOI = first instance with `SI ∈ {33, 15, 13}`; hot-water/
boost BOI = the `SI = 16` instance. Schedule zones map to the two `SI = 17` Schedule BOIs
(primary first, boost second).

### 13.4 Snapshot interpretation

From one **Get all service values** call:
- `primary_power_on` = (Timer/Mode `I:6` value == 2).
- `timed_boost_active` = (HotWater `I:4` `OT` == 2); `timed_boost_duration` = its `D`.
- `timed_boost_enabled` = (HotWater `I:27` value != 0).
- `primary_energy_kwh` / `boost_energy_kwh` = respective `I:19` value / 1000.

### 13.5 Device discovery & advertisement parsing

During LE scan the client identifies/validates the target from the raw advertisement bytes:

- **Name** (`BluetoothDevice.getName()`) = the device **serial number**.
- **Address** → MAC; `gatewayMacId = int(address_without_colons, 16)` (decimal for `GMI`).
- **`advRecord[21]`** = **hardware type**; must be **65** for E7 Plus → implies 2 channels.
- **`advRecord[30]`** (per-bit status): bit0..3 = channels 1–4 present, bit4 = owner set,
  bit5 = Wi-Fi module connected, bit6 = connection secure.
- Validate that the UART service UUID is advertised, the name matches the expected serial,
  and `advRecord[21] == 65` before connecting.

---

## 14. Implementation notes & gotchas

Non-normative guidance for robust clients; adopt or ignore per environment.

- **Tuned timeouts (recommended):** the meter answers in ~1–3 s or not at all on a given
  connection. A per-request timeout of **~8 s** (vs the 30 s default) fails fast and lets the
  time be spent on a fresh reconnect; a separate consumption-read timeout of ~5 s works well.
  Retry a failed command ~3 attempts, each on a freshly reconnected session, with a short
  (2–3 s) quiet gap between attempts.
- **One request at a time:** keep a strictly serial request queue. Do **not** parallelise on
  one connection — it overwhelms the meter. Within a request, keep the **~100 ms inter-packet
  delay** and write-with-response; bursting a 9-packet write is a primary cause of dropped
  responses.
- **A timed-out write did not apply.** Empirically, when a write times out the action did
  **not** take effect on the device (no "applied but un-acked" case) — so a timeout can be
  treated as a clean failure.
- **`getConsumptionState` selector:** the int arg selects a DynamicTariff record. That
  service is **not** enumerated by GetAllServiceValues, so its id can't be resolved; probe
  small values — the meter answers either `0` or `1` with an equivalent 7-day profile. Use
  "try `1`, fall back to `0`, remember whichever answered."
- **Length prefix = UTF-16 char count**, not UTF-8 byte count. Identical for ASCII JSON;
  diverges only if non-ASCII ever appears in a payload.
- **AES is ECB + zero-pad to 16**, *not* PKCS#7. On receive, decrypt then trim using the
  2-byte length prefix (the zero padding is discarded by the length).
- **Final packet index is `255`**; normal packets start at `1`; reassemble by ascending index.
- **`HI=14` overload:** one observed read path reuses `hanmanagement HI=14` (UpgradeDevice)
  as "get all configuration attributes". Treat as suspect / verify against a real device
  before relying on it.
- **GMI is decimal**, derived from the hex MAC. Some non-BLE transports use a *reversed* MAC —
  not relevant to BLE.

---

*All numeric ids, wire keys, byte layouts, and encodings in this document are normative
unless explicitly marked as a §14 implementation note.*
