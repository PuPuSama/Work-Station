import { ProjectArticleWorkspace } from "@/components/project-article-workspace";

type ArticlePageProps = {
  params: Promise<{
    customer: string;
    taskId: string;
  }>;
  searchParams: Promise<{
    step?: string;
  }>;
};

export default async function ArticlePage({ params, searchParams }: ArticlePageProps) {
  const { customer, taskId } = await params;
  const { step } = await searchParams;
  return (
    <ProjectArticleWorkspace
      customer={decodeURIComponent(customer)}
      taskId={decodeURIComponent(taskId)}
      initialStep={step}
    />
  );
}
