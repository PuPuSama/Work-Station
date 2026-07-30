import { ProjectShell } from "@/components/project-shell";

type ProjectLayoutProps = {
  children: React.ReactNode;
  params: Promise<{ customer: string }>;
};

export default async function ProjectLayout({
  children,
  params,
}: ProjectLayoutProps) {
  const { customer } = await params;

  return (
    <ProjectShell customer={decodeURIComponent(customer)}>
      {children}
    </ProjectShell>
  );
}
