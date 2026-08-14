import { ServerProjectBatchCenter } from "@/components/server-project-batch-center";

export function ProjectBatchDirectory({ customer }: { customer: string }) {
  return <ServerProjectBatchCenter customer={customer} />;
}
