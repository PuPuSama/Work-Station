import assert from "node:assert/strict";
import test from "node:test";
import { batchHighAiRate } from "./batch-ai-policy.ts";

const skipped = (score) => ({ action_kind: "humanize", status: "skipped",
  output_summary: { skip_reason: "initial_ai_rate_above_threshold", initial_ai_rate: score } });

test("batch warning uses a completed skip decision and strict 40% boundary", () => {
  for (const score of [null, undefined, "90", NaN, Infinity, 0, 40, 101]) {
    assert.equal(batchHighAiRate([skipped(score)]), null);
  }
  assert.equal(batchHighAiRate([skipped(40.1)]), 40.1);
  assert.equal(batchHighAiRate([skipped(100)]), 100);
  assert.equal(batchHighAiRate([{ ...skipped(90), status: "pending" }]), null);
  assert.equal(batchHighAiRate([{ ...skipped(90), action_kind: "review" }]), null);
});

test("refresh preserves the warning; ordinary and historical low-score skips do not show it", () => {
  assert.equal(batchHighAiRate(JSON.parse(JSON.stringify([skipped(72)]))), 72);
  assert.equal(batchHighAiRate([{ action_kind: "humanize", status: "skipped",
    output_summary: { humanization_skipped: true, initial_ai_rate: 20 } }]), null);
  assert.equal(batchHighAiRate([]), null);
});
