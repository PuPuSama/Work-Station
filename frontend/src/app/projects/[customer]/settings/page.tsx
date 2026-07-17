import { ProjectSettings } from "@/components/project-settings";

type ProjectSettingsPageProps = {
  params: Promise<{
    customer: string;
  }>;
};

export default async function ProjectSettingsPage({ params }: ProjectSettingsPageProps) {
  const { customer } = await params;
  return <ProjectSettings customer={decodeURIComponent(customer)} />;
}
