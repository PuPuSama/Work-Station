"use client";

import {
  ArrowLeft,
  BookOpenText,
  Clock3,
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
import { ServerProjectJobCenter } from "@/components/server-project-job-center";
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
import { sameProjectId } from "@/lib/project-id";
import { formatProjectDate } from "@/lib/project-date";
import type { AccessibleProject, TaskRecord, WorkflowStatus } from "@/types";

const sectionNames = {
  articles: "文章任务",
  knowledge: "知识库",
  batches: "批量处理",
  deliveries: "交付记录",
  settings: "项目设置",
} as const;

const RECENT_TASK_LIMIT = 5;
type RecentArticleTask = Pick<
  TaskRecord,
  "id" | "topic_index" | "topic" | "selected_title" | "status" | "updated_at"
>;

const recentTaskStatusLabels: Record<WorkflowStatus, string> = {
  new: "待生成标题",
  titles_ready: "待选择标题",
  title_selected: "待确认产品",
  outline_ready: "待审阅大纲",
  outline_confirmed: "待生成初稿",
  draft_ready: "待填写 AI-rate",
  initial_ai_checked: "待人化",
  humanized_ready: "待终检",
  final_ai_checked: "待恢复链接",
  links_verified: "待准备图片",
  images_ready: "待导出",
  docx_exported: "已导出",
};

function recentTaskStep(status: WorkflowStatus) {
  if (status === "new" || status === "titles_ready") return "setup";
  if (status === "title_selected" || status === "outline_ready") return "outline";
  if (status === "outline_confirmed" || status === "draft_ready") return "draft";
  if (status === "initial_ai_checked" || status === "humanized_ready" || status === "final_ai_checked") return "review";
  return "delivery";
}

export function ProjectShell({
  customer,
  children,
}: {
  customer: string;
  children: React.ReactNode;
}) {
  const [role, setRole] = useState<AccessibleProject["effective_role"] | null>(null);
  const [isProjectOwner, setIsProjectOwner] = useState(false);
  const [recentTasks, setRecentTasks] = useState<RecentArticleTask[]>([]);
  const pathname = usePathname().replace(/\/$/, "");
  const projectPath = `/projects/${encodeURIComponent(customer)}`;
  const segments = pathname.split("/").filter(Boolean);
  const section = segments[2] as keyof typeof sectionNames | undefined;
  const isDetail =
    (section === "articles" || section === "batches") && segments.length > 3;
  const isArticleDetail = section === "articles" && isDetail;

  useEffect(() => {
    let active = true;
    void apiGet<AccessibleProject[]>("/api/projects")
      .then((projects) => {
        if (!active) return;
        const project = projects.find((item) => sameProjectId(item.project_id, customer));
        setRole(project?.effective_role ?? null);
        setIsProjectOwner(project?.is_project_owner === true);
      })
      .catch(() => {
        if (active) setRole(null);
      });
    return () => {
      active = false;
    };
  }, [customer]);

  useEffect(() => {
    if (!isArticleDetail) {
      return;
    }
    let active = true;
    void apiGet<RecentArticleTask[]>(
      `${projectPath}/tasks/recent?limit=${RECENT_TASK_LIMIT}`,
    )
      .then((tasks) => {
        if (!active) return;
        setRecentTasks(tasks);
      })
      .catch(() => {
        if (active) setRecentTasks([]);
      });
    return () => {
      active = false;
    };
  }, [isArticleDetail, pathname, projectPath]);

  const items = [
    ["文章任务", "单篇内容与状态", "articles", FileText],
    ["知识库", "Inbox、发布与产品确认", "knowledge", BookOpenText],
    ["批量处理", "批量生成与队列", "batches", Layers3],
    ["交付记录", "成品与导出历史", "deliveries", PackageCheck],
  ] as const;
  const canManageSettings =
    role === "org_admin" ||
    role === "team_lead" ||
    (role === "editor" && isProjectOwner);
  const sectionLabel = section ? sectionNames[section] : customer;

  return (
    <SidebarProvider>
      <Sidebar collapsible="icon" variant="inset">
        <SidebarHeader className="px-3 py-3">
          <SidebarMenu>
            <SidebarMenuItem>
              <SidebarMenuButton size="lg" tooltip="Article Agent" render={<Link href="/" />}>
                <span className="flex size-8 items-center justify-center rounded-lg bg-primary text-primary-foreground">
                  <PenLine className="size-4" />
                </span>
                <span className="grid min-w-0 flex-1 text-left leading-tight">
                  <span className="truncate font-semibold">Article Agent</span>
                  <span className="truncate text-xs text-sidebar-foreground/60">SEO 内容运营台</span>
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
              <div className="mx-2 mb-3 rounded-lg border border-sidebar-border bg-sidebar-accent/50 px-3 py-2 group-data-[collapsible=icon]:hidden">
                <div className="text-[11px] font-medium uppercase tracking-[0.14em] text-sidebar-foreground/50">Project</div>
                <div className="mt-1 truncate text-sm font-semibold">{customer}</div>
              </div>
              <SidebarMenu>
                <SidebarMenuItem>
                  <SidebarMenuButton
                    isActive={pathname === "/batch-writing"}
                    tooltip="批量写作"
                    render={
                      <Link
                        href={`/batch-writing?project=${encodeURIComponent(customer)}`}
                        aria-current={pathname === "/batch-writing" ? "page" : undefined}
                      />
                    }
                    className="h-10"
                  >
                    <Layers3 />
                    <span className="grid min-w-0 flex-1 leading-tight">
                      <span>批量写作</span>
                      <span className="truncate text-[11px] font-normal text-sidebar-foreground/55">结构化配置并生成计划</span>
                    </span>
                  </SidebarMenuButton>
                </SidebarMenuItem>
                {items.map(([label, description, key, Icon]) => {
                  const href = `${projectPath}/${key}`;
                  return (
                    <SidebarMenuItem key={href}>
                      <SidebarMenuButton
                        isActive={section === key}
                        tooltip={label}
                        render={<Link href={href} aria-current={section === key ? "page" : undefined} />}
                        className="h-10"
                      >
                        <Icon />
                        <span className="grid min-w-0 flex-1 leading-tight">
                          <span>{label}</span>
                          <span className="truncate text-[11px] font-normal text-sidebar-foreground/55">{description}</span>
                        </span>
                      </SidebarMenuButton>
                    </SidebarMenuItem>
                  );
                })}
              </SidebarMenu>
            </SidebarGroupContent>
          </SidebarGroup>
          {isArticleDetail && <SidebarGroup className="mt-auto group-data-[collapsible=icon]:hidden">
            <SidebarGroupLabel><Clock3 />最近修改</SidebarGroupLabel>
            <SidebarGroupContent>
              {recentTasks.length ? (
                <SidebarMenu>
                  {recentTasks.map((task) => {
                    const href = `${projectPath}/articles/${encodeURIComponent(task.id)}?step=${recentTaskStep(task.status)}`;
                    const title = task.selected_title || task.topic;
                    return (
                      <SidebarMenuItem key={task.id}>
                        <SidebarMenuButton
                          isActive={segments[3] === task.id}
                          tooltip={title}
                          render={<Link href={href} aria-label={`打开最近修改文章：${title}`} />}
                          className="h-11"
                        >
                          <FileText />
                          <span className="grid min-w-0 flex-1 leading-tight">
                            <span className="truncate">{title}</span>
                            <span className="truncate text-[11px] font-normal text-sidebar-foreground/55">
                              {recentTaskStatusLabels[task.status]} · {formatProjectDate(task.updated_at, { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" })}
                            </span>
                          </span>
                        </SidebarMenuButton>
                      </SidebarMenuItem>
                    );
                  })}
                </SidebarMenu>
              ) : <p className="px-2 text-xs leading-5 text-sidebar-foreground/55">暂无最近修改文章</p>}
            </SidebarGroupContent>
          </SidebarGroup>}
        </SidebarContent>
        <SidebarFooter className="px-3 pb-3">
          <SidebarMenu>
            <SidebarMenuItem>
              <SidebarMenuButton isActive={pathname === "/settings"} tooltip="全局设置" render={<Link href="/settings" />}>
                <Settings2 /><span>全局设置</span>
              </SidebarMenuButton>
            </SidebarMenuItem>
            {canManageSettings && (
              <SidebarMenuItem>
                <SidebarMenuButton isActive={section === "settings"} tooltip="项目设置" render={<Link href={`${projectPath}/settings`} />}>
                  <Settings2 /><span>项目设置</span>
                </SidebarMenuButton>
              </SidebarMenuItem>
            )}
            <SidebarMenuItem>
              <SidebarMenuButton tooltip="返回全部项目" render={<Link href="/" />}>
                <ArrowLeft /><span>返回全部项目</span>
              </SidebarMenuButton>
            </SidebarMenuItem>
          </SidebarMenu>
        </SidebarFooter>
        <SidebarRail />
      </Sidebar>
      <SidebarInset className="min-w-0 overflow-x-hidden">
        <header className="sticky top-0 z-20 flex h-14 items-center gap-3 border-b bg-background/95 px-4 backdrop-blur">
          <SidebarTrigger className="-ml-1" />
          <div className="h-4 w-px bg-border" aria-hidden="true" />
          <Breadcrumb className="hidden min-w-0 flex-1 sm:block">
            <BreadcrumbList className="flex-nowrap">
              <BreadcrumbItem><BreadcrumbLink render={<Link href={`${projectPath}/articles`} />}>{customer}</BreadcrumbLink></BreadcrumbItem>
              {section && <><BreadcrumbSeparator /><BreadcrumbItem>{isDetail ? <BreadcrumbLink render={<Link href={`${projectPath}/${section}`} />}>{sectionLabel}</BreadcrumbLink> : <BreadcrumbPage>{sectionLabel}</BreadcrumbPage>}</BreadcrumbItem></>}
              {isDetail && <><BreadcrumbSeparator /><BreadcrumbItem><BreadcrumbPage>{section === "articles" ? "文章工作台" : "批次详情"}</BreadcrumbPage></BreadcrumbItem></>}
            </BreadcrumbList>
          </Breadcrumb>
          <ServerProjectJobCenter
            customer={customer}
            role={role}
            isProjectOwner={isProjectOwner}
          />
          <Link
            href="/settings"
            className="inline-flex size-8 shrink-0 items-center justify-center rounded-md text-muted-foreground transition-colors hover:bg-accent hover:text-foreground"
            aria-label="打开全局设置"
          >
            <Settings2 className="size-4" />
          </Link>
          <LogoutButton iconOnly />
        </header>
        <div className="min-w-0 flex-1">{children}</div>
      </SidebarInset>
    </SidebarProvider>
  );
}
