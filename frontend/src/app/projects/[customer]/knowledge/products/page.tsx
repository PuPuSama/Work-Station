import { ProjectProductLibrary } from "@/components/project-product-library";

type ProjectProductLibraryPageProps = {
  params: Promise<{ customer: string }>;
};

export default async function ProjectProductLibraryPage({
  params,
}: ProjectProductLibraryPageProps) {
  const { customer } = await params;
  return <ProjectProductLibrary customer={decodeURIComponent(customer)} />;
}
