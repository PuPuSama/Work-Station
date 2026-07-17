import { redirect } from "next/navigation";

type ProjectPageProps = {
  params: Promise<{
    customer: string;
  }>;
};

export default async function ProjectPage({ params }: ProjectPageProps) {
  const { customer } = await params;
  const projectName = decodeURIComponent(customer);
  redirect(`/projects/${encodeURIComponent(projectName)}/articles`);
}
