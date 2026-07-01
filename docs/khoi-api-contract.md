# Khoi Cloud API — expected contract

There is no reachable Khoi Cloud endpoint in this environment. This document defines
the contract `KhoiClient` (in `backend/KhoiIntegration`) is coded against, so
`KhoiSyncService` can be flipped on (`Khoi:Enabled=true` + `Khoi:BaseUrl` +
`Khoi:ApiKey` in config) the moment real credentials exist, with no code changes.

## Auth

`Authorization: Bearer {Khoi:ApiKey}` header on every request (assumed API-key auth;
adjust `KhoiClient` if Khoi Tech's real integration uses OAuth instead).

## `GET /v1/athletes/{externalAthleteId}/readings/latest`

Polled by `KhoiSyncService` on an interval (`Khoi:PollIntervalSeconds`, default 30).

Request: none beyond the path/auth.

Response:
```json
{
  "athleteId": "khoi-ext-abc123",
  "deviceSerial": "KHOI-0042",
  "batteryPercent": 71,
  "readings": [
    {
      "timestamp": "2026-07-01T10:15:00Z",
      "heartRate": 142,
      "recoveryScore": 78,
      "stressLevel": 34,
      "spo2": 97,
      "hydrationPct": 82,
      "staminaPct": 65
    }
  ]
}
```

`KhoiClient.GetLatestReadingsAsync(externalAthleteId)` deserializes this into
`KhoiCloudReading[]`, which `KhoiSyncService` maps to our internal `WearableReadingDto`
(see `schemas/wearable-packet.json`) and passes to `IHardwareTelemetryService`,
identical to what `khoi-wearable-sim` posts directly to `/api/wearable/ingest` today.

## Athlete ID mapping

Khoi Cloud identifies athletes by its own `externalAthleteId`, not our
`AspNetUsers.Id`. Production wiring needs a mapping table (`Devices.SerialNumber` or a
new `ExternalId` column) linking a Khoi device serial to our internal `AthleteId` at
pairing time — not built yet, tracked here as a known gap.

## Known gaps (do not treat this contract as confirmed)

- Real field names, auth scheme, and polling vs. webhook delivery are **assumed**,
  not confirmed with Khoi Tech. Update this doc and `KhoiClient` together once a real
  API spec is shared.
- No webhook receiver exists yet — if Khoi Cloud pushes instead of being polled, add a
  `KhoiWebhookController` in `backend/Api` rather than extending `KhoiSyncService`.
