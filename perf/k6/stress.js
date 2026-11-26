// Stress: keep adding users past expected load to find where it bends.
// Latency budgets are 3x looser here; errors and query counts are not.
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
  thresholds: withCounts(thresholds({ latencyScale: 3, errorRate: 0.02 })),
};

export default function () {
  userJourney();
}
export const handleSummary = summary("stress");
