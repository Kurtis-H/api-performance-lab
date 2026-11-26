// Smoke: one user for a short while. Does everything work at all?
import { summary, thresholds, userJourney, withCounts } from "./lib/traffic.js";

export const options = {
  vus: 1,
  duration: __ENV.DURATION || "30s",
  thresholds: withCounts(thresholds()),
};

export default function () {
  userJourney();
}
export const handleSummary = summary("smoke");
