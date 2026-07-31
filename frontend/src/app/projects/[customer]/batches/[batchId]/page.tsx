import { ProjectBatchWorkspace } from "@/components/project-batch-workspace";

type ProjectBatchDetailPageProps = {
  params: Promise<{
    customer: string;
    batchId: string;
  }>;
};

export default async function ProjectBatchDetailPage({ params }: ProjectBatchDetailPageProps) {
  const { customer, batchId } = await params;
  return (
    <ProjectBatchWorkspace
      customer={decodeURIComponent(customer)}
      batchId={decodeURIComponent(batchId)}
    />
  );
}
