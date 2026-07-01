#!/usr/bin/env node
// Emits realistic Forge Insole readings against the exact same
// POST /api/insole/ingest contract the real ESP32/mobile-BLE bridge will use
// (see hardware-integration/schemas/insole-packet.json).
//
// Usage:
//   node index.mjs --email athlete@sportsforge.dev --password Passw0rd! [--api http://localhost:5186] [--interval 2000] [--mode live|offline-sync]

import { parseArgs, login, post, randomWalk, jitter } from "../shared.mjs";

const args = parseArgs(process.argv.slice(2));

if (!args.email || !args.password) {
  console.error("Usage: node index.mjs --email <email> --password <password> [--api url] [--interval ms] [--mode live|offline-sync]");
  process.exit(1);
}

const DEVICE_SERIAL = args.serial ?? "ForgeInsole-SIM01";

let cadence = 168;
let balance = 90;
let asymmetry = 5;

function nextReading(foot) {
  cadence   = randomWalk(cadence, 150, 190, 4);
  balance   = randomWalk(balance, 60, 98, 3);
  asymmetry = randomWalk(asymmetry, 1, 22, 2.5);

  return {
    timestamp: new Date().toISOString(),
    foot,
    pressureMap: {
      heel:     Math.round(jitter(foot === "L" ? 58 : 55, 15)),
      midfoot:  Math.round(jitter(38, 10)),
      forefoot: Math.round(jitter(foot === "L" ? 74 : 70, 15)),
    },
    cadence: Math.round(cadence),
    groundContactMs: Math.round(jitter(230, 25)),
    footStrike: Math.random() > 0.15 ? "Midfoot" : (Math.random() > 0.5 ? "Heel" : "Forefoot"),
    strideAsymmetryPct: Math.round(asymmetry * 10) / 10,
    impactForce: Math.round(jitter(1.9, 0.6) * 10) / 10,
    balanceScore: Math.round(balance),
  };
}

async function main() {
  console.log(`[forge-insole-sim] Logging in as ${args.email} against ${args.api} ...`);
  const { token, name } = await login(args.api, args.email, args.password);
  console.log(`[forge-insole-sim] Authenticated as ${name}. Device serial: ${DEVICE_SERIAL}`);

  if (args.mode === "offline-sync") {
    const burstSize = Number(args.burst ?? 40);
    console.log(`[forge-insole-sim] Offline-sync mode — buffering ${burstSize} readings, then uploading as one batch...`);

    const readings = [];
    for (let i = 0; i < burstSize; i++) {
      readings.push(nextReading(i % 2 === 0 ? "L" : "R"));
    }

    const result = await post(args.api, "/api/insole/ingest", token, {
      deviceSerial: DEVICE_SERIAL,
      athleteId: "", // ignored server-side — derived from the JWT
      source: "OfflineSync",
      readings,
    });

    console.log(`[forge-insole-sim] Offline batch accepted: ${result.accepted} readings.`);
    return;
  }

  console.log(`[forge-insole-sim] Live mode — posting one reading every ${args.interval}ms. Ctrl+C to stop.`);
  let foot = "L";
  setInterval(async () => {
    foot = foot === "L" ? "R" : "L";
    const reading = nextReading(foot);
    try {
      await post(args.api, "/api/insole/ingest", token, {
        deviceSerial: DEVICE_SERIAL,
        athleteId: "",
        source: "Live",
        readings: [reading],
      });
      console.log(`[forge-insole-sim] ${reading.foot} foot · cadence ${reading.cadence} · balance ${reading.balanceScore} · asymmetry ${reading.strideAsymmetryPct}%`);
    } catch (err) {
      console.error(`[forge-insole-sim] Ingest failed: ${err.message}`);
    }
  }, Number(args.interval));
}

main().catch((err) => {
  console.error(`[forge-insole-sim] Fatal: ${err.message}`);
  process.exit(1);
});
