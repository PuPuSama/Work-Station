"use client";

import {
  ArrowLeft,
  BookOpenText,
  FileText,
  Layers3,
  PackageCheck,
  PenLine,
  Settings2,
} from "lucide-react";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { useEffect, useState } from "react";

import { LogoutButton } from "@/components/logout-button";
import { ProjectJobCenter } from "@/components/project-job-center";
import {
  Breadcrumb,
  BreadcrumbItem,
  BreadcrumbLink,
  BreadcrumbList,
  BreadcrumbPage,
  BreadcrumbSeparator,
} from "@/components/ui/breadcrumb";
import {
  Sidebar,
  SidebarContent,
  SidebarFooter,
  SidebarGroup,
  SidebarGroupContent,
  SidebarGroupLabel,
  SidebarHeader,
  SidebarInset,
  SidebarMenu,
  SidebarMenuButton,
  SidebarMenuItem,
  SidebarProvider,
  SidebarRail,
  SidebarSeparator,
  SidebarTrigger,
} from "@/components/ui/sidebar";
import { apiGet } from "@/lib/api";
import type { AccessibleProject, AuthStatus, PublicConfig } from "@/types";

type ProjectShellProps = {
  customer: string;
  children: React.ReactNode;
};

const sectionNames = {
  articles: "文章任务",
  knowledge: "知识库",
  batches: "批量处理",
  deliveries: "交付记录",
  settings: "项目设置",
} as const;

export function ProjectShell({ customer, children }: ProjectShellProps) {
  const [knowledgeEnabled, setKnowledgeEnabled] = useState(false);
  const [serverMode, setServerMode] = useState<boolean | null>(null);
  const [serverRole, setServerRole] = useState<
    AccessibleProject["effective_role"] | null
  >(null);
  const pathname = usePathname().replace(/\/$/, "");
  const projectPath = `/projects/${encodeURIComponent(customer)}`;
  const segments = pathname.split("/").filter(Boolean);
  const section = segments[2] as keyof typeof sectionNames | undefined;
  const isDetail =
    (section === "articles" || section === "batches") && segments.length > 3;

  useEffect(() => {
    let active = true;
    void apiGet<AuthStatus>("/api/auth/status")
      .then(async (status) => {
        const isServer = status.data?.mode === "server";
        const isLocal =
          status.data?.mode === undefined &&
          typeof status.data?.enabled === "boolean";
        if (!isServer && !isLocal) {
          throw new Error("Unrecognized application mode.");
        }
        if (!active) return;
        setServerMode(isServer);
        setServerRole(null);
        if (isServer) {
          setKnowledgeEnabled(false);
          const projects = await apiGet<AccessibleProject[]>("/api/projects");
          if (!active) return;
          setServerRole(
            projects.find((project) => project.project_id === customer)
              ?.effective_role ?? null,
          );
          return;
        }
        const value = await apiGet<PublicConfig>("/api/config");
        if (!active) return;
        setKnowledgeEnabled(Boolean(value.features?.knowledge_agent_enabled));
      })
      .catch(() => {
        // Navigation remains on the stable M0 surface when config is unavailable.
      });
    return () => {
      active = false;
    };
  }, [customer]);

  const localItems = [
    {
      label: "文章任务",
      description: "单篇内容与状态",
      href: `${projectPath}/articles`,
      icon: FileText,
      active: section === "articles",
    },
    ...(knowledgeEnabled
      ? [
          {
            label: "知识库",
            description: "来源、产品与证据",
            href: `${projectPath}/knowledge`,
            icon: BookOpenText,
            active: section === "knowledge",
          },
        ]
      : []),
    {
      label: "批量处理",
      description: "批量生成与队列",
      href: `${projectPath}/batches`,
      icon: Layers3,
      active: section === "batches",
    },
    {
      label: "交付记录",
      description: "成品与导出历史",
      href: `${projectPath}/deliveries`,
      icon: PackageCheck,
      active: section === "deliveries",
    },
  ];
  const items =
    serverMode === true
      ? localItems.filter((item) => item.href.endsWith("/deliveries"))
      : serverMode === false
        ? localItems
        : [];
  const projectHome = serverMode === true
    ? `${projectPath}/deliveries`
    : `${projectPath}/articles`;
  const canManageServerMembers =
    serverRole === "org_admin" || serverRole === "team_lead";
  const sectionLabel =
    section === "settings" && serverMode === true
      ? "项目成员"
      : section
        ? sectionNames[section]
        : customer;

  return (
    <SidebarProvider>
      <Sidebar collapsible="icon" variant="inset">
        <SidebarHeader className="px-3 py-3">
          <SidebarMenu>
            <SidebarMenuItem>
              <SidebarMenuButton
                size="lg"
                tooltip="Article Agent"
                render={<Link href="/" aria-label="返回全部项目" />}
                className="h-11"
              >
                <span className="flex size-8 shrink-0 items-center justify-center rounded-lg bg-primary text-primary-foreground">
                  <PenLine className="size-4" />
                </span>
                <span className="grid min-w-0 flex-1 text-left leading-tight">
                  <span className="truncate font-semibold">Article Agent</span>
                  <span className="truncate text-xs text-sidebar-foreground/60">
                    SEO 内容运营台
                  </span>
                </span>
              </SidebarMenuButton>
            </SidebarMenuItem>
          </SidebarMenu>
        </SidebarHeader>

        <SidebarSeparator />

        <SidebarContent>
          <SidebarGroup>
            <SidebarGroupLabel>当前项目</SidebarGroupLabel>
            <SidebarGroupContent>
              <div
                className="mx-2 mb-3 rounded-lg border border-sidebar-border bg-sidebar-accent/50 px-3 py-2 group-data-[collapsible=icon]:hidden"
                title={customer}
              >
                <div className="text-[11px] font-medium uppercase tracking-[0.14em] text-sidebar-foreground/50">
                  Project
                </div>
                <div className="mt-1 truncate text-sm font-semibold">{customer}</div>
              </div>
              <SidebarMenu>
                {items.map((item) => (
                  <SidebarMenuItem key={item.href}>
                    <SidebarMenuButton
                      isActive={item.active}
                      tooltip={item.label}
                      render={<Link href={item.href} aria-current={item.active ? "page" : undefined} />}
                      className="h-10"
                    >
                      <item.icon />
                      <span className="grid min-w-0 flex-1 leading-tight">
                        <span>{item.label}</span>
                        <span className="truncate text-[11px] font-normal text-sidebar-foreground/55">
                          {item.description}
                        </span>
                      </span>
                    </SidebarMenuButton>
                  </SidebarMenuItem>
                ))}
              </SidebarMenu>
            </SidebarGroupContent>
          </SidebarGroup>
        </SidebarContent>

        <SidebarFooter className="px-3 pb-3">
          <SidebarMenu>
            {(serverMode === false || canManageServerMembers) && (
              <SidebarMenuItem>
                <SidebarMenuButton
                  isActive={section === "settings"}
                  tooltip={
                    serverMode === true ? "项目成员" : "项目设置"
                  }
                  render={<Link href={`${projectPath}/settings`} />}
                >
                  <Settings2 />
                  <span>
                    {serverMode === true ? "项目成员" : "项目设置"}
                  </span>
                </SidebarMenuButton>
              </SidebarMenuItem>
            )}
            <SidebarMenuItem>
              <SidebarMenuButton tooltip="返回全部项目" render={<Link href="/" />}>
                <ArrowLeft />
                <span>返回全部项目</span>
              </SidebarMenuButton>
            </SidebarMenuItem>
          </SidebarMenu>
        </SidebarFooter>
        <SidebarRail />
      </Sidebar>

      <SidebarInset className="min-w-0 overflow-x-hidden">
        <header className="sticky top-0 z-20 flex h-14 shrink-0 items-center gap-3 border-b bg-background/95 px-4 backdrop-blur supports-[backdrop-filter]:bg-background/80">
          <SidebarTrigger className="-ml-1" />
          <div className="h-4 w-px bg-border" aria-hidden="true" />
          <span className="min-w-0 flex-1 truncate text-sm font-medium sm:hidden">
            {isDetail
              ? section === "articles"
                ? "文章工作台"
                : "批次详情"
              : section
                ? sectionLabel
                : customer}
          </span>
          <Breadcrumb className="hidden min-w-0 flex-1 sm:block">
            <BreadcrumbList className="flex-nowrap">
              <BreadcrumbItem className="min-w-0">
                <BreadcrumbLink
                  render={<Link href={projectHome} />}
                  className="max-w-48 truncate"
                  title={customer}
                >
                  {customer}
                </BreadcrumbLink>
              </BreadcrumbItem>
              {section && (
                <>
                  <BreadcrumbSeparator />
                  <BreadcrumbItem>
                    {isDetail ? (
                      <BreadcrumbLink render={<Link href={`${projectPath}/${section}`} />}>
                        {sectionLabel}
                      </BreadcrumbLink>
                    ) : (
                      <BreadcrumbPage>{sectionLabel}</BreadcrumbPage>
                    )}
                  </BreadcrumbItem>
                </>
              )}
              {isDetail && (
                <>
                  <BreadcrumbSeparator />
                  <BreadcrumbItem>
                    <BreadcrumbPage>
                      {section === "articles" ? "文章工作台" : "批次详情"}
                    </BreadcrumbPage>
                  </BreadcrumbItem>
                </>
              )}
            </BreadcrumbList>
          </Breadcrumb>
          {serverMode === false && <ProjectJobCenter customer={customer} />}
          <LogoutButton iconOnly />
        </header>
        <div className="min-w-0 flex-1">{children}</div>
      </SidebarInset>
    </SidebarProvider>
  );
}
