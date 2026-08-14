import { ServerProjectBatchDetail } from "@/components/server-project-batch-detail";

export function ProjectBatchWorkspace({
  customer,
  batchId,
}: {
  customer: string;
  batchId: string;
}) {
  return <ServerProjectBatchDetail customer={customer} batchId={batchId} />;
}
