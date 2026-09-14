# Hardware Integration Architecture

Status: **simulator-first**. Neither physical device exists in production form yet —
the Forge Insole pressure array is being redesigned (see `ForgeInsole_Product_Concept.pdf`)
and there is no live Khoi Cloud endpoint reachable from this environment. Everything in
this folder is built so that when either device ships, only the data *source* changes —
the ingestion contract, database schema, and dashboards do not.

## 1. Three data sources

| Source | What it captures | Transport today | Transport when hardware ships |
|---|---|---|---|
| Forge Insole (TechXM) | foot pressure map, cadence, ground contact time, foot strike, stride asymmetry, impact force, balance | `simulators/forge-insole-sim` → REST | ESP32 → BLE 5.0 GATT → athlete-mobile (`react-native-ble-plx`) → REST, batched |
| Khoi wearable | heart rate, recovery score, stress, SpO2, hydration, stamina/load | `simulators/khoi-wearable-sim` → REST | Khoi Cloud API → `backend/KhoiIntegration` (polling background service) |
| Video | match/training footage for pose analysis | not built yet | upload → `ai-service` (OpenCV/MediaPipe), out of scope for this phase |

Both simulators speak the **exact same wire format** the real hardware/cloud will use
(see `schemas/`), and POST to the **exact same ingestion endpoints**
(`/api/insole/ingest`, `/api/wearable/ingest`) that the real devices will call. Nothing
downstream — DB schema, controllers, SignalR hub, dashboard UI — needs to change when
real hardware arrives.

## 2. Ingestion paths

### Insole (mobile-mediated, for the real device)
```
ESP32 (BLE 5.0 GATT) → athlete-mobile (react-native-ble-plx)
                      → buffers locally while offline
                      → POST /api/insole/ingest (batch) when connected
                      → live session also opens /hubs/vitals (SignalR) for real-time callouts
```
See `docs/ble-protocol.md` for the proposed GATT service/characteristic layout.

### Insole (simulator, today)
```
forge-insole-sim → POST /api/insole/ingest  (same endpoint, same JSON shape)
```

### Wearable (real Khoi Cloud, for later)
```
Khoi Cloud API → backend/KhoiIntegration (KhoiClient, polling KhoiSyncService)
              → normalizes to WearableReading
              → same internal ingestion path as InsoleController/WearableController
              → broadcasts on /hubs/vitals
```
`KhoiSyncService` is a hosted `BackgroundService` in `backend/Api` that is **disabled by
default** (`Khoi:Enabled=false` in config) because there is no reachable Khoi Cloud
endpoint yet. See `docs/khoi-api-contract.md` for the expected request/response shape it
will poll.

### Wearable (simulator, today)
```
khoi-wearable-sim → POST /api/wearable/ingest  (same endpoint, same JSON shape)
```

Both real and simulated paths funnel through `IHardwareTelemetryService`
(`backend/Api/Services`), which is the single place that persists a reading and
broadcasts it — so `InsoleController`, `WearableController`, and the future
`KhoiSyncService` never duplicate that logic.

## 3. Database schema (migration `AddHardwareTelemetry`)

- **Devices** — `Id, AthleteId, Type (Insole|Wearable), SerialNumber, FirmwareVersion, PairedAt, LastSyncAt, BatteryPercent, Status (Connected|Disconnected|Syncing)`
- **InsoleReadings** — `Id, DeviceId, AthleteId, Timestamp, Foot (L|R), PressureMapJson, Cadence, GroundContactMs, FootStrike, StrideAsymmetryPct, ImpactForce, BalanceScore`
- **WearableReadings** — `Id, DeviceId, AthleteId, Timestamp, HeartRate, RecoveryScore, StressLevel, SpO2, HydrationPct, StaminaPct`
- **SyncSessions** — `Id, DeviceId, StartedAt, CompletedAt, PacketCount, Source (Live|OfflineSync)`

`AthleteId` is a `string` matching `AspNetUsers.Id` (Identity's default key type), same
convention as `Drill.CoachId` / `SessionParticipant.AthleteId` elsewhere in the schema.

## 4. Offline + sync

The insole buffers locally when the phone is out of BLE range or offline. On
reconnect, the mobile app POSTs a batch to `/api/insole/ingest` with
`source: "OfflineSync"`. Every successful ingest (live or batch) updates
`Devices.LastSyncAt` and creates/updates a `SyncSessions` row so dashboards can render
"Connected · last sync 2 min ago" vs. a stale/disconnected state. `GET
/api/devices` exposes this directly so the UI never has to compute it client-side.

## 5. POPIA note

Insole and wearable readings are biometric/health-adjacent data. This phase does not
implement consent flags or retention policies — it only establishes the schema and
transport. Before this goes beyond a demo: add an `athlete consent` flag controlling
doctor/coach visibility of raw readings, and a retention/deletion policy for
`InsoleReadings`/`WearableReadings` (these tables will grow fast — plan for
partitioning or rollup aggregation, not raw-row retention forever).

## 6. Related docs
- `docs/data-flow.md` — sequence diagrams (mermaid) for live and offline-sync paths
- `docs/ble-protocol.md` — proposed BLE GATT layout for the real Forge Insole firmware
- `docs/khoi-api-contract.md` — expected Khoi Cloud API shape `KhoiClient` will call
- `schemas/insole-packet.json`, `schemas/wearable-packet.json` — the wire format both
  simulators and (eventually) real hardware must produce
