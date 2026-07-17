import { ProjectArticleList } from "@/components/project-article-list";

type ProjectArticlesPageProps = {
  params: Promise<{
    customer: string;
  }>;
};

export default async function ProjectArticlesPage({ params }: ProjectArticlesPageProps) {
  const { customer } = await params;
  return <ProjectArticleList customer={decodeURIComponent(customer)} />;
}
