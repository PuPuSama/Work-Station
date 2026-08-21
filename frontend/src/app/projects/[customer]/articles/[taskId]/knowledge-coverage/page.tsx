import { ServerKnowledgeCoverageDetail } from "@/components/server-knowledge-coverage-detail";

type KnowledgeCoveragePageProps = {
  params: Promise<{
    customer: string;
    taskId: string;
  }>;
};

export default async function KnowledgeCoveragePage({
  params,
}: KnowledgeCoveragePageProps) {
  const { customer, taskId } = await params;
  return (
    <ServerKnowledgeCoverageDetail
      customer={decodeURIComponent(customer)}
      taskId={decodeURIComponent(taskId)}
    />
  );
}
