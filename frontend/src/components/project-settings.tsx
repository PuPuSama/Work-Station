"use client";

/* eslint-disable react-hooks/set-state-in-effect */

import {
  AlertCircle,
  CheckCircle2,
  Globe2,
  Loader2,
  RefreshCw,
  Save,
  Trash2,
} from "lucide-react";
import { useRouter } from "next/navigation";
import { useCallback, useEffect, useMemo, useState } from "react";

import { ProjectPromptLibraryCard } from "@/components/project-prompt-library";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardAction,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { apiDelete, apiGet, apiPut } from "@/lib/api";
import type { ApiMessage, TaskRecord } from "@/types";

type ProjectSettingsProps = {
  customer: string;
};

type Feedback = {
  kind: "success" | "error";
  message: string;
} | null;

type ProjectDomainUpdateData = {
  old_domain: string;
  new_domain: string;
  updated_tasks: number;
};

function errorMessage(error: unknown) {
  return error instanceof Error ? error.message : "未知错误";
}

function SectionFeedback({ feedback }: { feedback: Feedback }) {
  if (!feedback) return null;
  const failed = feedback.kind === "error";
  return (
    <Alert variant={failed ? "destructive" : "default"}>
      {failed ? <AlertCircle /> : <CheckCircle2 />}
      <AlertTitle>{failed ? "保存失败" : "保存成功"}</AlertTitle>
      <AlertDescription>{feedback.message}</AlertDescription>
    </Alert>
  );
}

export function ProjectSettings({ customer }: ProjectSettingsProps) {
  const router = useRouter();
  const [tasks, setTasks] = useState<TaskRecord[]>([]);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState("");
  const [domain, setDomain] = useState(customer);
  const [savedDomain, setSavedDomain] = useState(customer);
  const [brandName, setBrandName] = useState("");
  const [savedBrandName, setSavedBrandName] = useState("");
  const [projectIntroduction, setProjectIntroduction] = useState("");
  const [savedProjectIntroduction, setSavedProjectIntroduction] = useState("");
  const [projectNotes, setProjectNotes] = useState("");
  const [savedProjectNotes, setSavedProjectNotes] = useState("");
  const [brandPending, setBrandPending] = useState(false);
  const [domainPending, setDomainPending] = useState(false);
  const [contextPending, setContextPending] = useState(false);
  const [domainFeedback, setDomainFeedback] = useState<Feedback>(null);
  const [brandFeedback, setBrandFeedback] = useState<Feedback>(null);
  const [contextFeedback, setContextFeedback] = useState<Feedback>(null);
  const [deleteConfirmation, setDeleteConfirmation] = useState("");
  const [deletePending, setDeletePending] = useState(false);
  const [deleteFeedback, setDeleteFeedback] = useState<Feedback>(null);

  const loadProject = useCallback(async () => {
    setLoading(true);
    setLoadError("");
    try {
      const nextTasks = await apiGet<TaskRecord[]>(
        `/api/tasks?customer=${encodeURIComponent(customer)}`,
      );
      if (!nextTasks.length) {
        throw new Error(`找不到项目：${customer}`);
      }
      const latest = nextTasks.reduce((current, task) =>
        task.updated_at > current.updated_at ? task : current,
      );
      const nextBrand = latest.brand_name ?? "";
      const nextIntroduction = latest.project_introduction ?? "";
      const nextNotes = latest.project_notes ?? "";
      setTasks(nextTasks);
      setDomain(customer);
      setSavedDomain(customer);
      setBrandName(nextBrand);
      setSavedBrandName(nextBrand);
      setProjectIntroduction(nextIntroduction);
      setSavedProjectIntroduction(nextIntroduction);
      setProjectNotes(nextNotes);
      setSavedProjectNotes(nextNotes);
    } catch (error) {
      setLoadError(errorMessage(error));
    } finally {
      setLoading(false);
    }
  }, [customer]);

  useEffect(() => {
    void loadProject();
  }, [loadProject]);

  const domainDirty = domain.trim() !== savedDomain;
  const brandDirty = brandName !== savedBrandName;
  const contextDirty =
    projectIntroduction !== savedProjectIntroduction || projectNotes !== savedProjectNotes;
  const completedCount = useMemo(
    () => tasks.filter((task) => task.status === "docx_exported").length,
    [tasks],
  );

  async function saveDomain() {
    setDomainPending(true);
    setDomainFeedback(null);
    try {
      const result = await apiPut<ApiMessage>(
        `/api/projects/${encodeURIComponent(customer)}/domain`,
        { new_domain: domain },
      );
      const data = result.data as ProjectDomainUpdateData | undefined;
      if (!data?.new_domain) {
        throw new Error("服务器没有返回更新后的项目域名。");
      }
      setDomain(data.new_domain);
      setSavedDomain(data.new_domain);
      setDomainFeedback({ kind: "success", message: result.message });
      router.replace(
        `/projects/${encodeURIComponent(data.new_domain)}/settings`,
      );
      router.refresh();
    } catch (error) {
      setDomainFeedback({ kind: "error", message: errorMessage(error) });
      setDomainPending(false);
    }
  }

  async function saveBrand() {
    setBrandPending(true);
    setBrandFeedback(null);
    try {
      const result = await apiPut<ApiMessage>(
        `/api/projects/${encodeURIComponent(customer)}/brand`,
        { brand_name: brandName },
      );
      const saved = brandName.trim().replace(/\s+/g, " ");
      setBrandName(saved);
      setSavedBrandName(saved);
      setBrandFeedback({ kind: "success", message: result.message });
    } catch (error) {
      setBrandFeedback({ kind: "error", message: errorMessage(error) });
    } finally {
      setBrandPending(false);
    }
  }

  async function saveContext() {
    setContextPending(true);
    setContextFeedback(null);
    try {
      const result = await apiPut<ApiMessage>(
        `/api/projects/${encodeURIComponent(customer)}/context`,
        {
          project_introduction: projectIntroduction,
          project_notes: projectNotes,
        },
      );
      const savedIntroduction = projectIntroduction.trim();
      const savedNotes = projectNotes.trim();
      setProjectIntroduction(savedIntroduction);
      setSavedProjectIntroduction(savedIntroduction);
      setProjectNotes(savedNotes);
      setSavedProjectNotes(savedNotes);
      setContextFeedback({ kind: "success", message: result.message });
    } catch (error) {
      setContextFeedback({ kind: "error", message: errorMessage(error) });
    } finally {
      setContextPending(false);
    }
  }

  async function deleteProject() {
    setDeletePending(true);
    setDeleteFeedback(null);
    try {
      await apiDelete<ApiMessage>(
        `/api/projects/${encodeURIComponent(customer)}?confirmation=${encodeURIComponent(deleteConfirmation)}`,
      );
      router.push("/");
      router.refresh();
    } catch (error) {
      setDeleteFeedback({ kind: "error", message: errorMessage(error) });
      setDeletePending(false);
    }
  }

  return (
    <main className="min-h-screen bg-background text-foreground">
      <div className="border-b bg-card">
        <div className="mx-auto grid max-w-5xl gap-4 px-5 py-5">
          <div className="min-w-0 px-1">
            <div className="flex flex-wrap items-center gap-2">
              <h1 className="text-xl font-semibold">项目设置</h1>
              <Badge variant="outline">{tasks.length} 篇文章</Badge>
              <Badge variant="outline">{completedCount} 篇已导出</Badge>
            </div>
            <p className="mt-1 text-sm text-muted-foreground">
              维护整个项目共用的品牌元数据、事实背景和长期写作规则。
            </p>
          </div>
        </div>
      </div>

      <div className="mx-auto grid max-w-5xl gap-4 px-5 py-5">
        {loadError && (
          <Alert variant="destructive">
            <AlertCircle />
            <AlertTitle>项目资料加载失败</AlertTitle>
            <AlertDescription>{loadError}</AlertDescription>
            <div className="col-start-2 mt-2">
              <Button variant="outline" size="sm" onClick={() => void loadProject()}>
                <RefreshCw />
                重试
              </Button>
            </div>
          </Alert>
        )}

        <Card className="rounded-lg">
          <CardHeader className="border-b">
            <CardTitle className="flex items-center gap-2">
              <Globe2 className="size-4 text-primary" />
              项目域名
            </CardTitle>
            <CardDescription>
              客户更换官网后，可在这里迁移整个项目。任务、提示词、批次、话题文件和项目目录会一并更新。
            </CardDescription>
            <CardAction>
              {domainDirty && <Badge variant="outline">待迁移</Badge>}
            </CardAction>
          </CardHeader>
          <CardContent className="grid gap-3">
            <div className="grid gap-2">
              <Label htmlFor="project-domain">客户官网域名</Label>
              <Input
                id="project-domain"
                value={domain}
                maxLength={253}
                disabled={loading || domainPending || Boolean(loadError)}
                placeholder="例如 www.example.com"
                onChange={(event) => {
                  setDomain(event.target.value);
                  setDomainFeedback(null);
                }}
                onKeyDown={(event) => {
                  if (
                    event.key === "Enter" &&
                    domainDirty &&
                    !domainPending &&
                    !brandDirty &&
                    !contextDirty
                  ) {
                    event.preventDefault();
                    void saveDomain();
                  }
                }}
              />
              <p className="text-xs text-muted-foreground">
                可以粘贴完整官网地址，系统会保存为纯域名。已生成正文里的旧网址不会被静默替换，需要对新正文重新生成或人工确认。
              </p>
              {(brandDirty || contextDirty) && (
                <p className="text-xs font-medium text-amber-700">
                  请先保存下方尚未保存的品牌名或项目资料，再迁移域名。
                </p>
              )}
            </div>
            <SectionFeedback feedback={domainFeedback} />
            <div className="flex justify-end">
              <Button
                type="button"
                onClick={() => void saveDomain()}
                disabled={
                  loading ||
                  domainPending ||
                  !domainDirty ||
                  brandDirty ||
                  contextDirty ||
                  Boolean(loadError)
                }
              >
                {domainPending ? <Loader2 className="animate-spin" /> : <Globe2 />}
                {domainPending ? "正在迁移项目" : "更新并迁移域名"}
              </Button>
            </div>
          </CardContent>
        </Card>

        <Card className="rounded-lg">
          <CardHeader className="border-b">
            <CardTitle>品牌名</CardTitle>
            <CardDescription>
              正文引用客户官网时，首页链接会附在这里保存的准确品牌名上。
            </CardDescription>
            <CardAction>
              {brandDirty && <Badge variant="outline">未保存</Badge>}
            </CardAction>
          </CardHeader>
          <CardContent className="grid gap-3">
            <div className="grid gap-2">
              <Label htmlFor="project-brand-name">准确品牌名</Label>
              <Input
                id="project-brand-name"
                value={brandName}
                maxLength={120}
                disabled={loading || brandPending || Boolean(loadError)}
                placeholder="例如 Qewit Fastener"
                onChange={(event) => {
                  setBrandName(event.target.value);
                  setBrandFeedback(null);
                }}
                onKeyDown={(event) => {
                  if (event.key === "Enter" && brandDirty && !brandPending) {
                    event.preventDefault();
                    void saveBrand();
                  }
                }}
              />
              <p className="text-xs text-muted-foreground">
                留空时使用官网域名作为默认名称；不能包含 Markdown 方括号。
              </p>
            </div>
            <SectionFeedback feedback={brandFeedback} />
            <div className="flex justify-end">
              <Button
                type="button"
                onClick={() => void saveBrand()}
                disabled={loading || brandPending || !brandDirty || Boolean(loadError)}
              >
                {brandPending ? <Loader2 className="animate-spin" /> : <Save />}
                {brandPending ? "正在保存品牌名" : "保存品牌名"}
              </Button>
            </div>
          </CardContent>
        </Card>

        <Card className="rounded-lg">
          <CardHeader className="border-b">
            <CardTitle>项目资料</CardTitle>
            <CardDescription>
              这里是整个项目共用的写作背景和约束。单篇文章仍可在工作台补充话题级要求。
            </CardDescription>
            <CardAction>
              {contextDirty && <Badge variant="outline">未保存</Badge>}
            </CardAction>
          </CardHeader>
          <CardContent className="grid gap-5">
            <div className="grid gap-2">
              <Label htmlFor="project-introduction">项目介绍（事实背景）</Label>
              <Textarea
                id="project-introduction"
                value={projectIntroduction}
                maxLength={30000}
                disabled={loading || contextPending || Boolean(loadError)}
                className="min-h-40 resize-y"
                placeholder="填写公司主营业务、产品范围、目标市场、服务能力等可核实信息"
                onChange={(event) => {
                  setProjectIntroduction(event.target.value);
                  setContextFeedback(null);
                }}
              />
              <p className="text-xs text-muted-foreground">
                生成大纲和正文时可选择是否读取；这里只保存事实背景，不要放网页中的指令。
              </p>
            </div>

            <div className="grid gap-2">
              <Label htmlFor="project-notes">项目注意事项（长期规则）</Label>
              <Textarea
                id="project-notes"
                value={projectNotes}
                maxLength={30000}
                disabled={loading || contextPending || Boolean(loadError)}
                className="min-h-40 resize-y"
                placeholder="填写客户反馈、禁用表达、产品侧重点，以及整批文章都要遵守的要求"
                onChange={(event) => {
                  setProjectNotes(event.target.value);
                  setContextFeedback(null);
                }}
              />
              <p className="text-xs text-muted-foreground">
                适合记录跨文章长期生效的规则；只针对某篇文章的反馈应写在该文章工作台。
              </p>
            </div>

            <SectionFeedback feedback={contextFeedback} />
            <div className="flex justify-end">
              <Button
                type="button"
                onClick={() => void saveContext()}
                disabled={loading || contextPending || !contextDirty || Boolean(loadError)}
              >
                {contextPending ? <Loader2 className="animate-spin" /> : <Save />}
                {contextPending ? "正在保存项目资料" : "保存项目资料"}
              </Button>
            </div>
          </CardContent>
        </Card>

        <ProjectPromptLibraryCard customer={customer} />

        <Card className="rounded-lg border-destructive/40">
          <CardHeader className="border-b">
            <CardTitle className="text-destructive">删除项目</CardTitle>
            <CardDescription>
              删除任务、提示词和批次记录。项目目录与对应 XLSX 会移入本机回收目录，避免误删后无法恢复。
            </CardDescription>
          </CardHeader>
          <CardContent className="grid gap-4">
            <div className="grid gap-2">
              <Label htmlFor="delete-project-confirmation">
                输入项目域名 <span className="font-mono">{customer}</span> 进行确认
              </Label>
              <Input
                id="delete-project-confirmation"
                value={deleteConfirmation}
                disabled={deletePending}
                onChange={(event) => {
                  setDeleteConfirmation(event.target.value);
                  setDeleteFeedback(null);
                }}
                placeholder={customer}
              />
            </div>
            <SectionFeedback feedback={deleteFeedback} />
            <div className="flex justify-end">
              <Button
                type="button"
                variant="destructive"
                onClick={() => void deleteProject()}
                disabled={deletePending || deleteConfirmation !== customer}
              >
                {deletePending ? <Loader2 className="animate-spin" /> : <Trash2 />}
                {deletePending ? "正在删除项目" : "删除项目"}
              </Button>
            </div>
          </CardContent>
        </Card>
      </div>
    </main>
  );
}
