#!/usr/bin/env node
// Emits realistic Khoi wearable readings against the exact same
// POST /api/wearable/ingest contract the real Khoi Cloud sync path (KhoiSyncService)
// will use (see hardware-integration/schemas/wearable-packet.json).
//
// Usage:
//   node index.mjs --email athlete@sportsforge.dev --password Passw0rd! [--api http://localhost:5186] [--interval 3000] [--mode live|offline-sync]

import { parseArgs, login, post, randomWalk, jitter } from "../shared.mjs";

const args = parseArgs(process.argv.slice(2));

if (!args.email || !args.password) {
  console.error("Usage: node index.mjs --email <email> --password <password> [--api url] [--interval ms] [--mode live|offline-sync]");
  process.exit(1);
}

const DEVICE_SERIAL = args.serial ?? "KHOI-SIM01";

let heartRate = 72;
let recovery  = 80;
let stress    = 25;
let hydration = 85;
let stamina   = 78;

function nextReading() {
  heartRate = randomWalk(heartRate, 55, 175, 6);
  recovery  = randomWalk(recovery, 45, 95, 2);
  stress    = randomWalk(stress, 5, 70, 3);
  hydration = randomWalk(hydration, 55, 98, 2);
  stamina   = randomWalk(stamina, 40, 95, 2);

  return {
    timestamp: new Date().toISOString(),
    heartRate: Math.round(heartRate),
    recoveryScore: Math.round(recovery),
    stressLevel: Math.round(stress),
    spo2: Math.round(jitter(97, 2)),
    hydrationPct: Math.round(hydration),
    staminaPct: Math.round(stamina),
  };
}

async function main() {
  console.log(`[khoi-wearable-sim] Logging in as ${args.email} against ${args.api} ...`);
  const { token, name } = await login(args.api, args.email, args.password);
  console.log(`[khoi-wearable-sim] Authenticated as ${name}. Device serial: ${DEVICE_SERIAL}`);

  if (args.mode === "offline-sync") {
    const burstSize = Number(args.burst ?? 20);
    console.log(`[khoi-wearable-sim] Offline-sync mode — buffering ${burstSize} readings, then uploading as one batch...`);

    const readings = [];
    for (let i = 0; i < burstSize; i++) readings.push(nextReading());

    const result = await post(args.api, "/api/wearable/ingest", token, {
      deviceSerial: DEVICE_SERIAL,
      athleteId: "",
      batteryPercent: Math.round(jitter(70, 15)),
      readings,
    });

    console.log(`[khoi-wearable-sim] Offline batch accepted: ${result.accepted} readings.`);
    return;
  }

  console.log(`[khoi-wearable-sim] Live mode — posting one reading every ${args.interval}ms. Ctrl+C to stop.`);
  setInterval(async () => {
    const reading = nextReading();
    try {
      await post(args.api, "/api/wearable/ingest", token, {
        deviceSerial: DEVICE_SERIAL,
        athleteId: "",
        batteryPercent: Math.round(jitter(70, 15)),
        readings: [reading],
      });
      console.log(`[khoi-wearable-sim] HR ${reading.heartRate} · recovery ${reading.recoveryScore} · hydration ${reading.hydrationPct} · stamina ${reading.staminaPct}`);
    } catch (err) {
      console.error(`[khoi-wearable-sim] Ingest failed: ${err.message}`);
    }
  }, Number(args.interval));
}

main().catch((err) => {
  console.error(`[khoi-wearable-sim] Fatal: ${err.message}`);
  process.exit(1);
});
