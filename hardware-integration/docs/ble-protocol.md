# Forge Insole — BLE GATT Protocol (proposed)

Not implemented in code yet — the physical sensor array is still being redesigned
(see `ForgeInsole_Product_Concept.pdf`, section 6-7: FSR sensors failed validation,
capacitive/piezoresistive redesign in progress). This is the contract
`react-native-ble-plx` will integrate against once firmware exists, kept here so the
mobile app, backend, and firmware teams build to the same spec.

## Advertised service

| Item | Value |
|---|---|
| Device name prefix | `ForgeInsole-` (+ serial suffix, e.g. `ForgeInsole-A1B2`) |
| Service UUID | `0000FA01-0000-1000-8000-00805F9B34FB` (placeholder — assign real UUID before manufacturing) |
| Connection interval | BLE 5.0, ~15-30ms, favoring latency over battery during a live session |

## Characteristics

| Characteristic | UUID (placeholder) | Properties | Payload |
|---|---|---|---|
| Pressure Stream | `...FA02` | Notify | packed pressure-map frame, ~20Hz during active movement |
| Gait/Balance | `...FA03` | Notify | cadence, ground contact time, foot strike, balance score, ~5Hz |
| Battery Level | `...FA04` | Read/Notify | standard BLE battery service, 0-100 |
| Device Status | `...FA05` | Read/Notify | firmware version, pairing state |
| Config | `...FA06` | Write | sample rate, calibration trigger |

## Packet → API mapping

Each notified frame is decoded by the mobile app into the shape defined in
`schemas/insole-packet.json`, buffered, and flushed to `POST /api/insole/ingest` either:
- every ~2s while a training/match session is active (`source: "Live"`), or
- as one batch on reconnect after an offline gap (`source: "OfflineSync"`).

This is exactly what `hardware-integration/simulators/forge-insole-sim` emits today
over HTTP instead of BLE — so the mobile app's ingestion call and the backend's
`InsoleController` don't change when BLE parsing is added; only a new
`insoleBleService.ts` (BLE read → packet decode → same POST call) gets added to
`athlete-mobile`.

## Open questions for the firmware redesign

- Final pressure-map resolution (sensor count/positions) once the capacitive array is
  finalized — `PressureMapJson` is intentionally a free-form JSON blob so the schema
  doesn't need to change when sensor count changes.
- Whether balance/asymmetry is computed on-device (ESP32) or derived server-side from
  raw per-foot pressure — currently assumed on-device, matching the product concept's
  "MPU6050 IMU... measures movement, gait, balance" placement.
