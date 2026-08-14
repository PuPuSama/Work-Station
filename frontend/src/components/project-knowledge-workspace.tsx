import { ServerKnowledgeWorkspace } from "@/components/server-knowledge-workspace";

export function ProjectKnowledgeWorkspace({ customer }: { customer: string }) {
  return <ServerKnowledgeWorkspace customer={customer} />;
}
