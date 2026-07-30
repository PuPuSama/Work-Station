import { ProjectKnowledgeLibrary } from "@/components/project-knowledge-library";

type ProjectKnowledgePageProps = {
  params: Promise<{
    customer: string;
  }>;
};

export default async function ProjectKnowledgePage({
  params,
}: ProjectKnowledgePageProps) {
  const { customer } = await params;
  return (
    <ProjectKnowledgeLibrary customer={decodeURIComponent(customer)} />
  );
}
