// Spike: a sudden rush (tickets go on sale), then back to normal.
// The API may slow down during the spike but must not fall over.
import { summary, thresholds, userJourney, withCounts } from "./lib/traffic.js";

export const options = {
  scenarios: {
    spike: {
      executor: "ramping-vus",
      stages: [
        { duration: "20s", target: 10 },
        { duration: "10s", target: 300 },
        { duration: "40s", target: 300 },
        { duration: "10s", target: 10 },
        { duration: "30s", target: 10 },
      ],
    },
  },
  // During the rush, reads may take up to ~1.5 s at p95 (7.5x the normal
  // budget) on a single worker, but nothing may fail.
  thresholds: withCounts(thresholds({ latencyScale: 7.5, errorRate: 0.01 })),
};

export default function () {
  userJourney();
}
export const handleSummary = summary("spike");
