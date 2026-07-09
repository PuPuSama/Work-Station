import { ArticleWorkbench } from "@/components/article-workbench";

type ProjectPageProps = {
  params: Promise<{
    customer: string;
  }>;
};

export default async function ProjectPage({ params }: ProjectPageProps) {
  const { customer } = await params;
  return <ArticleWorkbench customer={decodeURIComponent(customer)} />;
}
