// Load: the traffic we expect on a busy day. Must stay inside every budget.
// This scenario also feeds the CI regression gate (perf/compare.py).
import { summary, thresholds, userJourney, withCounts } from "./lib/traffic.js";

const VUS = Number(__ENV.VUS || 50);

export const options = {
  scenarios: {
    load: {
      executor: "ramping-vus",
      stages: [
        { duration: __ENV.RAMP || "30s", target: VUS },
        { duration: __ENV.DURATION || "2m", target: VUS },
        { duration: "10s", target: 0 },
      ],
    },
  },
  thresholds: withCounts(thresholds()),
};

export default function () {
  userJourney();
}
export const handleSummary = summary("load");
