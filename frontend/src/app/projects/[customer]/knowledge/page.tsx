import { ProjectKnowledgeLibrary } from "@/components/project-knowledge-library";
import { ProjectEvidenceWorkbench } from "@/components/project-evidence-workbench";
import { ProjectResearchWorkspace } from "@/components/project-research-workspace";

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
    <>
      <ProjectKnowledgeLibrary customer={decodedCustomer} />
      <ProjectResearchWorkspace customer={decodedCustomer} />
      <ProjectEvidenceWorkbench customer={decodedCustomer} />
    </>
  );
}
