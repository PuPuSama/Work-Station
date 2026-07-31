import { ProjectKnowledgeWorkspace } from "@/components/project-knowledge-workspace";

type ProjectKnowledgePageProps = {
  params: Promise<{
    customer: string;
  }>;
};

export default async function ProjectKnowledgePage({
  params,
}: ProjectKnowledgePageProps) {
  const { customer } = await params;
  const decodedCustomer = decodeURIComponent(customer);
  return (
    <ProjectKnowledgeWorkspace customer={decodedCustomer} />
  );
}
