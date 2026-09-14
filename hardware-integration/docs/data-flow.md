# Data Flow

## Live session (insole simulator or real device connected)

```mermaid
sequenceDiagram
    participant Sim as forge-insole-sim
    participant Api as backend/Api
    participant Db as SQL Server
    participant Hub as VitalsHub (/hubs/vitals)
    participant Web as athlete-dashboard / athlete-mobile

    Sim->>Api: POST /api/insole/ingest {deviceId, readings[], source:"Live"}
    Api->>Db: insert InsoleReadings, update Devices.LastSyncAt
    Api->>Hub: broadcast InsoleUpdate to group athlete-{id}
    Hub-->>Web: InsoleUpdate (cadence, balance, asymmetry...)
    Web->>Web: update body-map callouts live
```

## Live session (wearable simulator)

```mermaid
sequenceDiagram
    participant Sim as khoi-wearable-sim
    participant Api as backend/Api
    participant Db as SQL Server
    participant Hub as VitalsHub (/hubs/vitals)
    participant Web as athlete-dashboard / athlete-mobile

    Sim->>Api: POST /api/wearable/ingest {deviceId, readings[]}
    Api->>Db: insert WearableReadings, update Devices.LastSyncAt
    Api->>Hub: broadcast WearableUpdate to group athlete-{id}
    Hub-->>Web: WearableUpdate (heartRate, recovery, hydration...)
```

## Real Khoi Cloud path (future, currently disabled)

```mermaid
sequenceDiagram
    participant Cloud as Khoi Cloud API
    participant Sync as KhoiSyncService (BackgroundService)
    participant Client as KhoiClient (KhoiIntegration project)
    participant Svc as IHardwareTelemetryService
    participant Hub as VitalsHub

    loop every poll interval
        Sync->>Client: GetLatestReadingsAsync(athleteId)
        Client->>Cloud: GET /v1/athletes/{id}/readings
        Cloud-->>Client: KhoiCloudReading[]
        Client-->>Sync: normalized readings
        Sync->>Svc: IngestWearableReadingAsync(...)
        Svc->>Hub: broadcast WearableUpdate
    end
```

## Offline sync (insole reconnects after buffering)

```mermaid
sequenceDiagram
    participant App as athlete-mobile
    participant Api as backend/Api
    participant Db as SQL Server

    Note over App: BLE disconnected — readings buffered in local queue
    App->>App: BLE reconnects
    App->>Api: POST /api/insole/ingest {readings:[...50 buffered...], source:"OfflineSync"}
    Api->>Db: bulk insert, create SyncSessions row (Source=OfflineSync, PacketCount=50)
    Api->>Db: update Devices.LastSyncAt = now
    Api-->>App: 200 OK { accepted: 50 }
    App->>App: clear local buffer, show "Last sync: just now"
```
