import { ProjectBatchDetail } from "@/components/project-batch-detail";

type ProjectBatchDetailPageProps = {
  params: Promise<{
    customer: string;
    batchId: string;
  }>;
};

export default async function ProjectBatchDetailPage({ params }: ProjectBatchDetailPageProps) {
  const { customer, batchId } = await params;
  return (
    <ProjectBatchDetail
      customer={decodeURIComponent(customer)}
      batchId={decodeURIComponent(batchId)}
    />
  );
}
