import { ProjectBatchDirectory } from "@/components/project-batch-directory";

type ProjectBatchPageProps = {
  params: Promise<{
    customer: string;
  }>;
};

export default async function ProjectBatchPage({ params }: ProjectBatchPageProps) {
  const { customer } = await params;
  return <ProjectBatchDirectory customer={decodeURIComponent(customer)} />;
}
