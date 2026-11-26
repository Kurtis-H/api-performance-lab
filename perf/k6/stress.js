// Stress: keep adding users past expected load to find where it bends.
// Pass/fail is on errors and query budgets only. Latency is reported, not
// judged: a single worker is expected to slow down past ~240 req/s, and the
// point of this test is to show where.
import { summary, thresholds, userJourney, withCounts } from "./lib/traffic.js";

export const options = {
  scenarios: {
    stress: {
      executor: "ramping-vus",
      stages: [
        { duration: "1m", target: 50 },
        { duration: "1m", target: 100 },
        { duration: "1m", target: 200 },
        { duration: "1m", target: 300 },
        { duration: "30s", target: 0 },
      ],
    },
  },
  thresholds: withCounts(thresholds({ latency: false, errorRate: 0.02 })),
};

export default function () {
  userJourney();
}
export const handleSummary = summary("stress");
