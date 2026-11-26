// Soak: steady, moderate load for a long time. Finds problems that only build
// up slowly, like memory leaks. Default is 30 minutes; SOAK_DURATION=5m for a
// quick run.
import { sleep } from "k6";
import {
  residentMemoryMb,
  rssGrowthMb,
  summary,
  thresholds,
  userJourney,
  withCounts,
} from "./lib/traffic.js";

// Judged as a rate so the same threshold works for a 5-minute or 8-hour soak.
// The fixed app settles at ~1 MB/min early on (caches filling); the leak
// grows ~16 MB/min under this traffic.
const MAX_GROWTH_MB_PER_MIN = Number(__ENV.MAX_GROWTH_MB_PER_MIN || 5);

export const options = {
  setupTimeout: "2m",
  scenarios: {
    soak: {
      executor: "constant-vus",
      vus: Number(__ENV.VUS || 30),
      duration: __ENV.SOAK_DURATION || "30m",
    },
  },
  thresholds: withCounts({
    ...thresholds(),
    // Memory growth per minute, from after warm-up to the end.
    rss_growth_mb: [`value<${MAX_GROWTH_MB_PER_MIN}`],
  }),
};

export function setup() {
  // Warm up first so normal start-up allocation doesn't count as growth.
  for (let i = 0; i < 300; i++) userJourney(false);
  sleep(2);
  return { startMb: residentMemoryMb(), startedAt: Date.now() };
}

export default function () {
  userJourney();
}

export function teardown({ startMb, startedAt }) {
  sleep(2);
  const endMb = residentMemoryMb();
  const minutes = (Date.now() - startedAt) / 60000;
  rssGrowthMb.add((endMb - startMb) / minutes);
  console.log(
    `memory: ${startMb.toFixed(1)} MB -> ${endMb.toFixed(1)} MB over ${minutes.toFixed(1)} min`,
  );
}

export const handleSummary = summary("soak");
