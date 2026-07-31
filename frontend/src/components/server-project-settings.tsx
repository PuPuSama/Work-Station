"use client";

/* eslint-disable react-hooks/set-state-in-effect */

import {
  AlertCircle,
  CheckCircle2,
  Database,
  Loader2,
  RefreshCw,
  Save,
} from "lucide-react";
import { useCallback, useEffect, useMemo, useState } from "react";

import { ServerProjectMembers } from "@/components/server-project-members";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Skeleton } from "@/components/ui/skeleton";
import { ApiError, apiGet, apiPut } from "@/lib/api";
import type { ServerProjectMetadata } from "@/types";

type ServerProjectSettingsProps = {
  projectId: string;
};

type MetadataForm = {
  customerName: string;
  officialDomain: string;
};

type FieldErrors = Partial<Record<keyof MetadataForm, string>>;

type Feedback = {
  kind: "success" | "error";
  message: string;
  canReload?: boolean;
} | null;

function normalizeCustomerName(value: string) {
  return value.trim().replace(/\s+/g, " ");
}

function normalizeOfficialDomain(value: string) {
  return value.trim().replace(/\.$/, "").toLowerCase();
}

function validateForm(form: MetadataForm): {
  errors: FieldErrors;
  normalized: MetadataForm;
} {
  const normalized = {
    customerName: normalizeCustomerName(form.customerName),
    officialDomain: normalizeOfficialDomain(form.officialDomain),
  };
  const errors: FieldErrors = {};
  if (!normalized.customerName) {
    errors.customerName = "请输入客户或品牌显示名。";
  } else if (normalized.customerName.length > 120) {
    errors.customerName = "显示名不能超过 120 个字符。";
  } else if (/[\[\]]/.test(normalized.customerName)) {
    errors.customerName = "显示名不能包含方括号。";
  }
  if (!normalized.officialDomain) {
    errors.officialDomain = "请输入官方网站域名。";
  } else if (normalized.officialDomain.length > 253) {
    errors.officialDomain = "域名不能超过 253 个字符。";
  } else if (
    /:\/\/|[\/\\@:?#\s]/.test(normalized.officialDomain)
  ) {
    errors.officialDomain =
      "只填写主机名，例如 www.example.com；不要包含协议、路径或账号信息。";
  }
  return { errors, normalized };
}

function errorMessage(error: unknown, fallback: string) {
  return error instanceof Error && error.message ? error.message : fallback;
}

function ProjectMetadataCard({ projectId }: ServerProjectSettingsProps) {
  const encodedProject = useMemo(
    () => encodeURIComponent(projectId),
    [projectId],
  );
  const [metadata, setMetadata] = useState<ServerProjectMetadata | null>(null);
  const [form, setForm] = useState<MetadataForm>({
    customerName: "",
    officialDomain: "",
  });
  const [fieldErrors, setFieldErrors] = useState<FieldErrors>({});
  const [feedback, setFeedback] = useState<Feedback>(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);

  const applyMetadata = useCallback((next: ServerProjectMetadata) => {
    setMetadata(next);
    setForm({
      customerName: next.customer_name,
      officialDomain: next.official_domain,
    });
    setFieldErrors({});
  }, []);

  const loadMetadata = useCallback(
    async (showLoader: boolean) => {
      if (showLoader) setLoading(true);
      setFeedback(null);
      try {
        const next = await apiGet<ServerProjectMetadata>(
          `/api/projects/${encodedProject}/metadata`,
        );
        applyMetadata(next);
      } catch (error) {
        setFeedback({
          kind: "error",
          message: errorMessage(error, "项目资料加载失败，请重试。"),
          canReload: true,
        });
      } finally {
        if (showLoader) setLoading(false);
      }
    },
    [applyMetadata, encodedProject],
  );

  useEffect(() => {
    void loadMetadata(true);
  }, [loadMetadata]);

  const dirty =
    metadata !== null &&
    (normalizeCustomerName(form.customerName) !== metadata.customer_name ||
      normalizeOfficialDomain(form.officialDomain) !==
        metadata.official_domain);

  function validateField(field: keyof MetadataForm) {
    const result = validateForm(form);
    setFieldErrors((current) => ({
      ...current,
      [field]: result.errors[field],
    }));
  }

  async function saveMetadata() {
    if (!metadata) return;
    const result = validateForm(form);
    setFieldErrors(result.errors);
    if (Object.keys(result.errors).length) {
      setFeedback({
        kind: "error",
        message: "请先修正项目资料中的输入问题。",
      });
      return;
    }
    setSaving(true);
    setFeedback(null);
    try {
      const updated = await apiPut<ServerProjectMetadata>(
        `/api/projects/${encodedProject}/metadata`,
        {
          revision: metadata.revision,
          customer_name: result.normalized.customerName,
          official_domain: result.normalized.officialDomain,
        },
      );
      applyMetadata(updated);
      setFeedback({
        kind: "success",
        message: `项目资料已保存为 Revision ${updated.revision}。`,
      });
    } catch (error) {
      const conflict = error instanceof ApiError && error.status === 409;
      setFeedback({
        kind: "error",
        message: conflict
          ? "项目资料已被其他成员更新。请重新载入最新 Revision 后再编辑。"
          : errorMessage(error, "项目资料保存失败，请重试。"),
        canReload: true,
      });
    } finally {
      setSaving(false);
    }
  }

  if (loading && !metadata) {
    return (
      <Card className="rounded-xl" aria-busy="true">
        <CardHeader className="border-b">
          <Skeleton className="h-5 w-40" />
          <Skeleton className="h-4 w-full max-w-xl" />
        </CardHeader>
        <CardContent className="grid gap-4 pt-4 sm:grid-cols-2">
          <Skeleton className="h-20 w-full" />
          <Skeleton className="h-20 w-full" />
          <span className="sr-only">正在读取项目资料…</span>
        </CardContent>
      </Card>
    );
  }

  return (
    <Card className="min-w-0 rounded-xl">
      <CardHeader className="border-b">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div className="min-w-0">
            <CardTitle className="flex items-center gap-2">
              <Database className="size-4 shrink-0 text-primary" />
              项目身份资料
            </CardTitle>
            <CardDescription className="mt-1 max-w-2xl leading-6">
              这里维护共享显示名与官方网站。项目 ID 不会被重命名，已有任务继续保留创建时捕获的身份，新任务录入和官网操作使用更新后的资料。
            </CardDescription>
          </div>
          {metadata && (
            <Badge variant="outline">Revision {metadata.revision}</Badge>
          )}
        </div>
      </CardHeader>
      <CardContent className="grid gap-4 pt-4">
        {feedback && (
          <Alert variant={feedback.kind === "error" ? "destructive" : "default"}>
            {feedback.kind === "error" ? <AlertCircle /> : <CheckCircle2 />}
            <AlertTitle>
              {feedback.kind === "error" ? "项目资料未保存" : "项目资料已更新"}
            </AlertTitle>
            <AlertDescription>{feedback.message}</AlertDescription>
            {feedback.kind === "error" && feedback.canReload && (
              <div className="col-start-2 mt-2">
                <Button
                  type="button"
                  variant="outline"
                  className="min-h-11"
                  onClick={() => void loadMetadata(true)}
                  disabled={saving}
                >
                  <RefreshCw />
                  重新载入
                </Button>
              </div>
            )}
          </Alert>
        )}

        <div className="grid gap-4 sm:grid-cols-2">
          <div className="grid min-w-0 gap-1.5">
            <Label htmlFor="server-project-customer-name">
              客户或品牌显示名
            </Label>
            <Input
              id="server-project-customer-name"
              className="h-11"
              value={form.customerName}
              maxLength={120}
              aria-invalid={Boolean(fieldErrors.customerName)}
              aria-describedby="server-project-customer-name-help"
              disabled={!metadata || saving || loading}
              onChange={(event) => {
                setForm((current) => ({
                  ...current,
                  customerName: event.target.value,
                }));
                setFieldErrors((current) => ({
                  ...current,
                  customerName: undefined,
                }));
              }}
              onBlur={() => validateField("customerName")}
            />
            <p
              id="server-project-customer-name-help"
              className={
                fieldErrors.customerName
                  ? "text-xs text-destructive"
                  : "text-xs text-muted-foreground"
              }
            >
              {fieldErrors.customerName ||
                "用于项目目录和未来任务的共享显示名称。"}
            </p>
          </div>

          <div className="grid min-w-0 gap-1.5">
            <Label htmlFor="server-project-official-domain">
              官方网站域名
            </Label>
            <Input
              id="server-project-official-domain"
              className="h-11 font-mono"
              value={form.officialDomain}
              maxLength={253}
              inputMode="url"
              autoCapitalize="none"
              spellCheck={false}
              placeholder="www.example.com"
              aria-invalid={Boolean(fieldErrors.officialDomain)}
              aria-describedby="server-project-official-domain-help"
              disabled={!metadata || saving || loading}
              onChange={(event) => {
                setForm((current) => ({
                  ...current,
                  officialDomain: event.target.value,
                }));
                setFieldErrors((current) => ({
                  ...current,
                  officialDomain: undefined,
                }));
              }}
              onBlur={() => validateField("officialDomain")}
            />
            <p
              id="server-project-official-domain-help"
              className={
                fieldErrors.officialDomain
                  ? "text-xs text-destructive"
                  : "text-xs text-muted-foreground"
              }
            >
              {fieldErrors.officialDomain ||
                "只填主机名，不包含 https://、路径或登录信息。"}
            </p>
          </div>
        </div>

        <div className="grid gap-2 rounded-lg border bg-muted/30 p-3 text-sm sm:grid-cols-[minmax(0,1fr)_auto] sm:items-center">
          <div className="min-w-0">
            <p className="font-medium">不可变项目 ID</p>
            <p className="mt-1 break-all font-mono text-xs text-muted-foreground">
              {projectId}
            </p>
          </div>
          <Button
            type="button"
            className="min-h-11 w-full sm:w-auto"
            onClick={() => void saveMetadata()}
            disabled={!metadata || !dirty || saving || loading}
          >
            {saving ? <Loader2 className="animate-spin" /> : <Save />}
            保存项目资料
          </Button>
        </div>

        <p className="text-xs leading-5 text-muted-foreground">
          客户事实与产品资料应发布到 Knowledge；写作规则应保存为不可变 Prompt Snapshot。此处不接收自由文本上下文。
        </p>
      </CardContent>
    </Card>
  );
}

export function ServerProjectSettings({
  projectId,
}: ServerProjectSettingsProps) {
  return (
    <ServerProjectMembers
      projectId={projectId}
      pageKind="settings"
      beforeMemberCards={<ProjectMetadataCard projectId={projectId} />}
    />
  );
}
