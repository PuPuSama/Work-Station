// Use the persisted batch decision, so later article edits do not hide it.
export function batchHighAiRate(steps: ReadonlyArray<{
  action_kind: string;
  status: string;
  output_summary: Record<string, unknown>;
}>): number | null {
  for (const step of steps) {
    const score = step.output_summary.initial_ai_rate;
    if (step.action_kind === "humanize" && step.status === "skipped"
      && step.output_summary.skip_reason === "initial_ai_rate_above_threshold"
      && typeof score === "number" && Number.isFinite(score) && score > 40 && score <= 100) {
      return score;
    }
  }
  return null;
}
