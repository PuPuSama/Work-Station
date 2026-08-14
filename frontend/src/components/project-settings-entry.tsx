import { ServerProjectSettings } from "@/components/server-project-settings";

export function ProjectSettingsEntry({ customer }: { customer: string }) {
  return <ServerProjectSettings projectId={customer} />;
}
