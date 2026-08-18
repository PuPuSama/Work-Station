import { ServerArticleProductSelection } from "@/components/server-article-product-selection";

type ProductSelectionPageProps = {
  params: Promise<{
    customer: string;
    taskId: string;
  }>;
};

export default async function ProductSelectionPage({
  params,
}: ProductSelectionPageProps) {
  const { customer, taskId } = await params;
  return (
    <ServerArticleProductSelection
      customer={decodeURIComponent(customer)}
      taskId={decodeURIComponent(taskId)}
    />
  );
}
