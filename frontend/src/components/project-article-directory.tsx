import { ServerProjectArticleList } from "@/components/server-project-article-list";

export function ProjectArticleDirectory({ customer }: { customer: string }) {
  return <ServerProjectArticleList customer={customer} />;
}
