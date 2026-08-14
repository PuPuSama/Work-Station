import { ServerArticleWorkbench } from "@/components/server-article-workbench";

type ProjectArticleWorkspaceProps = {
  customer: string;
  taskId: string;
  initialStep?: string;
};

export function ProjectArticleWorkspace({
  customer,
  taskId,
  initialStep,
}: ProjectArticleWorkspaceProps) {
  return (
    <ServerArticleWorkbench
      key={`${customer}:${taskId}`}
      customer={customer}
      taskId={taskId}
      initialStep={initialStep}
    />
  );
}
