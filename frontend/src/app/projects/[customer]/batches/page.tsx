import { ProjectBatchCenter } from "@/components/project-batch-center";

type ProjectBatchPageProps = {
  params: Promise<{
    customer: string;
  }>;
};

export default async function ProjectBatchPage({ params }: ProjectBatchPageProps) {
  const { customer } = await params;
  return <ProjectBatchCenter customer={decodeURIComponent(customer)} />;
}
