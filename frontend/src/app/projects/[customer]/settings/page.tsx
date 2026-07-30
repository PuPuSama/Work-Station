import { ProjectSettingsEntry } from "@/components/project-settings-entry";

type ProjectSettingsPageProps = {
  params: Promise<{
    customer: string;
  }>;
};

export default async function ProjectSettingsPage({ params }: ProjectSettingsPageProps) {
  const { customer } = await params;
  return <ProjectSettingsEntry customer={decodeURIComponent(customer)} />;
}
