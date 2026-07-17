import { ArticleWorkbench } from "@/components/article-workbench";

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
    <ArticleWorkbench
      customer={decodeURIComponent(customer)}
      initialTaskId={decodeURIComponent(taskId)}
      initialStep={step}
      focusMode
    />
  );
}
