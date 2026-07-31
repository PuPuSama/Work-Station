import { ProjectArticleDirectory } from "@/components/project-article-directory";

type ProjectArticlesPageProps = {
  params: Promise<{
    customer: string;
  }>;
};

export default async function ProjectArticlesPage({ params }: ProjectArticlesPageProps) {
  const { customer } = await params;
  return <ProjectArticleDirectory customer={decodeURIComponent(customer)} />;
}
