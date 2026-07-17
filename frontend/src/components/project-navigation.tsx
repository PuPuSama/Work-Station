"use client";

import { ArrowLeft, Files, Layers3, PackageCheck, Settings2 } from "lucide-react";
import Link from "next/link";
import { usePathname } from "next/navigation";

import { ProjectJobCenter } from "@/components/project-job-center";
import { buttonVariants } from "@/components/ui/button";
import { cn } from "@/lib/utils";

type ProjectNavigationProps = {
  customer: string;
  className?: string;
};

export function ProjectNavigation({ customer, className }: ProjectNavigationProps) {
  const pathname = usePathname().replace(/\/$/, "");
  const projectPath = `/projects/${encodeURIComponent(customer)}`;
  const items = [
    {
      label: "文章任务",
      href: `${projectPath}/articles`,
      icon: Files,
      active: pathname.startsWith(`${projectPath}/articles`),
    },
    {
      label: "批量处理",
      href: `${projectPath}/batches`,
      icon: Layers3,
      active: pathname.startsWith(`${projectPath}/batches`),
    },
    {
      label: "交付记录",
      href: `${projectPath}/deliveries`,
      icon: PackageCheck,
      active: pathname.startsWith(`${projectPath}/deliveries`),
    },
    {
      label: "项目设置",
      href: `${projectPath}/settings`,
      icon: Settings2,
      active: pathname.startsWith(`${projectPath}/settings`),
    },
  ];

  return (
    <nav
      aria-label={`${customer} 项目导航`}
      className={cn(
        "flex flex-col gap-3 rounded-lg border bg-card/80 p-3 lg:flex-row lg:items-center lg:justify-between",
        className,
      )}
    >
      <div className="flex min-w-0 items-center gap-3">
        <Link
          href="/"
          aria-label="返回所有项目"
          className={cn(buttonVariants({ variant: "outline", size: "sm" }))}
        >
          <ArrowLeft />
          返回所有项目
        </Link>
        <span className="hidden h-5 w-px bg-border sm:block" aria-hidden="true" />
        <span className="min-w-0 truncate text-sm text-muted-foreground" title={customer}>
          {customer}
        </span>
      </div>

      <div className="flex flex-col gap-2 sm:flex-row sm:items-center">
        <div className="grid grid-cols-2 gap-1 rounded-lg bg-muted/70 p-1 sm:grid-cols-4">
          {items.map(({ label, href, icon: Icon, active }) => (
            <Link
              key={href}
              href={href}
              aria-current={active ? "page" : undefined}
              className={cn(
                buttonVariants({ variant: active ? "outline" : "ghost", size: "sm" }),
                active && "bg-background shadow-sm hover:bg-background",
              )}
            >
              <Icon />
              {label}
            </Link>
          ))}
        </div>
        <ProjectJobCenter customer={customer} />
      </div>
    </nav>
  );
}
