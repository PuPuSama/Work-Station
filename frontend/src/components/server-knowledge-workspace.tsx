import { ServerKnowledgeInbox } from "@/components/server-knowledge-inbox";

export function ServerKnowledgeWorkspace({ customer }: { customer: string }) {
  return <ServerKnowledgeInbox customer={customer} />;
}
