"use client";

/* eslint-disable react-hooks/set-state-in-effect */

import {
  AlertCircle,
  CheckCircle2,
  Database,
  Loader2,
  RefreshCw,
  Save,
  Sparkles,
  Trash2,
  Wifi,
} from "lucide-react";
import { useCallback, useEffect, useMemo, useState } from "react";

import { ServerProjectMembers } from "@/components/server-project-members";
import { ServerProjectPromptLibrary } from "@/components/server-project-prompt-library";
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
import { Textarea } from "@/components/ui/textarea";
import { ApiError, apiDelete, apiGet, apiPost, apiPut } from "@/lib/api";
import type { ServerProjectMetadata, WordPressCredentials } from "@/types";

type ServerProjectSettingsProps = {
  projectId: string;
};

type MetadataForm = {
  customerName: string;
  officialDomain: string;
  wordpressUrl: string;
  projectBusinessProfile: string;
  projectNotes: string;
};

type FieldErrors = Partial<Record<keyof MetadataForm, string>>;

type Feedback = {
  kind: "success" | "error";
  message: string;
  canReload?: boolean;
  title?: string;
} | null;

function normalizeCustomerName(value: string) {
  return value.trim().replace(/\s+/g, " ");
}

function normalizeOfficialDomain(value: string) {
  return value.trim().replace(/\.$/, "").toLowerCase();
}

function normalizeWordPressUrl(value: string) {
  return value.trim().replace(/\/+$/, "");
}

function normalizeWordPressUsername(value: string) {
  return value.trim().replace(/\s+/g, " ");
}

function normalizeProjectNotes(value: string) {
  return value.replace(/\r\n/g, "\n").trim();
}

function normalizeProjectBusinessProfile(value: string) {
  return value.replace(/\r\n/g, "\n").trim();
}

function validateForm(form: MetadataForm): {
  errors: FieldErrors;
  normalized: MetadataForm;
} {
  const normalized = {
    customerName: normalizeCustomerName(form.customerName),
    officialDomain: normalizeOfficialDomain(form.officialDomain),
    wordpressUrl: normalizeWordPressUrl(form.wordpressUrl),
    projectBusinessProfile: normalizeProjectBusinessProfile(
      form.projectBusinessProfile,
    ),
    projectNotes: normalizeProjectNotes(form.projectNotes),
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
  if (normalized.wordpressUrl.length > 2048) {
    errors.wordpressUrl = "WordPress 地址不能超过 2048 个字符。";
  } else if (
    normalized.wordpressUrl &&
    !/^https?:\/\/[^\s]+$/i.test(normalized.wordpressUrl)
  ) {
    errors.wordpressUrl =
      "请输入完整的 http(s)://WordPress 地址，不要包含账号或查询参数。";
  } else if (normalized.wordpressUrl) {
    try {
      const parsed = new URL(normalized.wordpressUrl);
      if (
        !parsed.hostname ||
        parsed.username ||
        parsed.password ||
        parsed.search ||
        parsed.hash
      ) {
        errors.wordpressUrl =
          "请输入不含账号、查询参数或片段的 WordPress 地址。";
      }
    } catch {
      errors.wordpressUrl =
        "请输入完整的 http(s)://WordPress 地址，不要包含账号或查询参数。";
    }
  }
  if (normalized.projectNotes.length > 30000) {
    errors.projectNotes = "项目注意事项不能超过 30000 个字符。";
  }
  if (normalized.projectBusinessProfile.length > 30000) {
    errors.projectBusinessProfile =
      "公司介绍与业务范围不能超过 30000 个字符。";
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
    wordpressUrl: "",
    projectBusinessProfile: "",
    projectNotes: "",
  });
  const [fieldErrors, setFieldErrors] = useState<FieldErrors>({});
  const [feedback, setFeedback] = useState<Feedback>(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [profileGenerating, setProfileGenerating] = useState(false);
  const [wordpressTesting, setWordpressTesting] = useState(false);
  const [wordpressCredentials, setWordpressCredentials] =
    useState<WordPressCredentials | null>(null);
  const [wordpressUsername, setWordpressUsername] = useState("");
  const [wordpressAppPassword, setWordpressAppPassword] = useState("");
  const [wordpressCredentialsLoading, setWordpressCredentialsLoading] =
    useState(true);
  const [wordpressCredentialsSaving, setWordpressCredentialsSaving] =
    useState(false);

  const applyMetadata = useCallback((next: ServerProjectMetadata) => {
    setMetadata(next);
    setForm({
      customerName: next.customer_name,
      officialDomain: next.official_domain,
      wordpressUrl: next.wordpress_url || "",
      projectBusinessProfile: next.project_business_profile || "",
      projectNotes: next.project_notes,
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

  const applyWordPressCredentials = useCallback(
    (next: WordPressCredentials) => {
      setWordpressCredentials(next);
      setWordpressUsername(next.username);
      setWordpressAppPassword("");
    },
    [],
  );

  const loadWordPressCredentials = useCallback(
    async (showLoader: boolean) => {
      if (showLoader) setWordpressCredentialsLoading(true);
      try {
        const next = await apiGet<WordPressCredentials>(
          `/api/projects/${encodedProject}/wordpress/credentials`,
        );
        applyWordPressCredentials(next);
      } catch (error) {
        setWordpressCredentials(null);
        setWordpressUsername("");
        setWordpressAppPassword("");
        setFeedback({
          kind: "error",
          message: errorMessage(error, "WordPress 账号配置加载失败，请重试。"),
          canReload: true,
        });
      } finally {
        if (showLoader) setWordpressCredentialsLoading(false);
      }
    },
    [applyWordPressCredentials, encodedProject],
  );

  useEffect(() => {
    void loadMetadata(true);
    void loadWordPressCredentials(true);
  }, [loadMetadata, loadWordPressCredentials]);

  const dirty =
    metadata !== null &&
    (normalizeCustomerName(form.customerName) !== metadata.customer_name ||
      normalizeOfficialDomain(form.officialDomain) !==
        metadata.official_domain ||
      normalizeWordPressUrl(form.wordpressUrl) !==
        (metadata.wordpress_url || "") ||
      normalizeProjectBusinessProfile(form.projectBusinessProfile) !==
        metadata.project_business_profile ||
      normalizeProjectNotes(form.projectNotes) !== metadata.project_notes);

  const wordpressCredentialsDirty =
    wordpressCredentials !== null &&
    (normalizeWordPressUsername(wordpressUsername) !==
      wordpressCredentials.username ||
      Boolean(wordpressAppPassword.trim()));

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
          wordpress_url: result.normalized.wordpressUrl,
          project_business_profile: result.normalized.projectBusinessProfile,
          project_notes: result.normalized.projectNotes,
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

  async function testWordPressConnection() {
    if (wordpressTesting || saving || loading) return;
    if (wordpressCredentialsDirty) {
      setFeedback({
        kind: "error",
        title: "请先保存 WordPress 账号",
        message: "账号或 Application Password 有未保存修改。保存后再测试连接，避免测试到旧账号。",
      });
      return;
    }
    const result = validateForm(form);
    setFieldErrors(result.errors);
    if (result.errors.wordpressUrl) return;
    setWordpressTesting(true);
    setFeedback(null);
    try {
      const response = await apiPost<{
        configured: boolean;
        url: string;
        username: string;
        message: string;
      }>(`/api/projects/${encodedProject}/wordpress/test-connection`, {
        wordpress_url: result.normalized.wordpressUrl,
      });
      setFeedback({
        kind: "success",
        title: "WordPress 连接成功",
        message: `${response.message} 当前站点：${response.url}，账号：${response.username}。如修改了站点地址，请点击“保存项目资料”保存。`,
      });
    } catch (error) {
      setFeedback({
        kind: "error",
        message: errorMessage(error, "WordPress 连接失败，请检查地址和服务端凭据。"),
      });
    } finally {
      setWordpressTesting(false);
    }
  }

  async function saveWordPressCredentials() {
    if (!wordpressCredentials || wordpressCredentialsSaving) return;
    const username = normalizeWordPressUsername(wordpressUsername);
    if (!username) {
      setFeedback({
        kind: "error",
        title: "WordPress 账号未保存",
        message: "请输入 WordPress 用户名。",
      });
      return;
    }
    if (!wordpressCredentials.configured && !wordpressAppPassword.trim()) {
      setFeedback({
        kind: "error",
        title: "WordPress 账号未保存",
        message: "首次配置必须填写 Application Password。",
      });
      return;
    }
    setWordpressCredentialsSaving(true);
    setFeedback(null);
    try {
      const updated = await apiPut<WordPressCredentials>(
        `/api/projects/${encodedProject}/wordpress/credentials`,
        {
          revision: wordpressCredentials.revision,
          username,
          app_password: wordpressAppPassword.trim() || null,
        },
      );
      applyWordPressCredentials(updated);
      setFeedback({
        kind: "success",
        title: "WordPress 账号已保存",
        message: "账号已按当前项目保存。Application Password 只在保存时发送，服务器不会再次显示它。",
      });
    } catch (error) {
      const conflict = error instanceof ApiError && error.status === 409;
      setFeedback({
        kind: "error",
        title: "WordPress 账号未保存",
        message: conflict
          ? "WordPress 账号已被其他成员更新。请重新载入最新配置后再保存。"
          : errorMessage(error, "WordPress 账号保存失败，请重试。"),
        canReload: true,
      });
    } finally {
      setWordpressCredentialsSaving(false);
    }
  }

  async function generateBusinessProfile() {
    if (!metadata || profileGenerating || saving || loading) return;
    if (
      normalizeProjectBusinessProfile(form.projectBusinessProfile) &&
      !window.confirm(
        "自动填写会替换当前公司介绍与业务范围草稿，是否继续？",
      )
    ) {
      return;
    }
    setProfileGenerating(true);
    setFeedback(null);
    try {
      const result = await apiPost<{
        draft: string;
        source_count: number;
      }>(
        `/api/projects/${encodedProject}/metadata/business-profile-draft`,
      );
      setForm((current) => ({
        ...current,
        projectBusinessProfile: result.draft,
      }));
      setFieldErrors((current) => ({
        ...current,
        projectBusinessProfile: undefined,
      }));
      setFeedback({
        kind: "success",
        title: "公司介绍草稿已生成",
        message: `已根据当前项目的 ${result.source_count} 份已发布知识生成草稿，请核对后点击保存。`,
      });
    } catch (error) {
      setFeedback({
        kind: "error",
        message: errorMessage(
          error,
          "公司介绍草稿生成失败，请确认知识库已有已发布资料后重试。",
        ),
      });
    } finally {
      setProfileGenerating(false);
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
              这里维护项目身份、公司业务背景和操作注意事项。项目 ID 不会被重命名；新任务会捕获保存时的背景快照，已有任务不会被静默覆盖。
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
              {feedback.title ||
                (feedback.kind === "error" ? "项目资料未保存" : "项目资料已更新")}
            </AlertTitle>
            <AlertDescription>{feedback.message}</AlertDescription>
            {feedback.kind === "error" && feedback.canReload && (
              <div className="col-start-2 mt-2">
                <Button
                  type="button"
                  variant="outline"
                  className="min-h-11"
                  onClick={() => {
                    void loadMetadata(true);
                    void loadWordPressCredentials(true);
                  }}
                  disabled={saving || wordpressCredentialsSaving}
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

        <div className="grid min-w-0 gap-1.5">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <Label htmlFor="server-project-wordpress-url">
              WordPress 站点地址
            </Label>
            <Button
              type="button"
              variant="outline"
              className="min-h-9"
              onClick={() => void testWordPressConnection()}
              disabled={!metadata || saving || loading || wordpressTesting}
            >
              {wordpressTesting ? (
                <Loader2 className="animate-spin" />
              ) : (
                <Wifi />
              )}
              测试连接
            </Button>
          </div>
          <Input
            id="server-project-wordpress-url"
            className="h-11 font-mono"
            value={form.wordpressUrl}
            maxLength={2048}
            inputMode="url"
            autoCapitalize="none"
            spellCheck={false}
            placeholder="https://www.example.com（留空使用服务端默认站点）"
            aria-invalid={Boolean(fieldErrors.wordpressUrl)}
            aria-describedby="server-project-wordpress-url-help"
            disabled={!metadata || saving || loading || wordpressTesting}
            onChange={(event) => {
              setForm((current) => ({
                ...current,
                wordpressUrl: event.target.value,
              }));
              setFieldErrors((current) => ({
                ...current,
                wordpressUrl: undefined,
              }));
            }}
            onBlur={() => validateField("wordpressUrl")}
          />
          <p
            id="server-project-wordpress-url-help"
            className={
              fieldErrors.wordpressUrl
                ? "text-xs text-destructive"
                : "text-xs text-muted-foreground"
            }
          >
            {fieldErrors.wordpressUrl ||
              "每个项目可以使用不同的 WordPress 地址。站点地址按项目保存；生产连接必须使用 HTTPS。"}
          </p>
        </div>

        <div className="grid min-w-0 gap-3 rounded-lg border bg-muted/20 p-3">
          <div className="flex flex-wrap items-start justify-between gap-2">
            <div className="min-w-0">
              <Label htmlFor="server-project-wordpress-username">
                WordPress 账号
              </Label>
              <p className="mt-1 text-xs leading-5 text-muted-foreground">
                每个项目单独保存账号。Application Password 只用于连接 WordPress REST API，不是后台网页登录密码。
              </p>
            </div>
            <Badge variant={wordpressCredentials?.configured ? "default" : "outline"}>
              {wordpressCredentialsLoading
                ? "读取中"
                : wordpressCredentials?.configured
                  ? "已配置"
                  : "未配置"}
            </Badge>
          </div>
          <div className="grid gap-4 sm:grid-cols-2">
            <div className="grid min-w-0 gap-1.5">
              <Label htmlFor="server-project-wordpress-username">用户名</Label>
              <Input
                id="server-project-wordpress-username"
                className="h-11"
                value={wordpressUsername}
                maxLength={200}
                autoCapitalize="none"
                autoComplete="username"
                spellCheck={false}
                placeholder="例如 article_agent_publisher"
                disabled={
                  !metadata ||
                  !wordpressCredentials ||
                  wordpressCredentialsLoading ||
                  wordpressCredentialsSaving ||
                  saving ||
                  wordpressTesting
                }
                onChange={(event) => setWordpressUsername(event.target.value)}
              />
            </div>
            <div className="grid min-w-0 gap-1.5">
              <Label htmlFor="server-project-wordpress-app-password">
                Application Password
              </Label>
              <Input
                id="server-project-wordpress-app-password"
                className="h-11 font-mono"
                type="password"
                value={wordpressAppPassword}
                maxLength={1024}
                autoComplete="new-password"
                spellCheck={false}
                placeholder={
                  wordpressCredentials?.configured
                    ? "留空保持现有密码"
                    : "首次配置必填"
                }
                disabled={
                  !metadata ||
                  !wordpressCredentials ||
                  wordpressCredentialsLoading ||
                  wordpressCredentialsSaving ||
                  saving ||
                  wordpressTesting
                }
                onChange={(event) =>
                  setWordpressAppPassword(event.target.value)
                }
              />
            </div>
          </div>
          <div className="flex flex-wrap items-center justify-between gap-2">
            <p className="text-xs leading-5 text-muted-foreground">
              {wordpressCredentials?.configured
                ? "密码不会回显；用户名不变时留空表示继续使用当前密码。修改用户名时请同时填写新密码。"
                : "首次保存后，服务器会加密保存密码；页面和任务接口都不会返回密码。"}
            </p>
            <Button
              type="button"
              variant="outline"
              className="min-h-11 w-full sm:w-auto"
              onClick={() => void saveWordPressCredentials()}
              disabled={
                !metadata ||
                wordpressCredentialsLoading ||
                !wordpressCredentialsDirty ||
                wordpressCredentialsSaving ||
                saving ||
                wordpressTesting
              }
            >
              {wordpressCredentialsSaving ? (
                <Loader2 className="animate-spin" />
              ) : (
                <Save />
              )}
              保存 WordPress 账号
            </Button>
          </div>
        </div>

        <div className="grid min-w-0 gap-1.5">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <Label htmlFor="server-project-business-profile">
              公司介绍与业务范围
            </Label>
            <Button
              type="button"
              variant="outline"
              className="min-h-9"
              onClick={() => void generateBusinessProfile()}
              disabled={!metadata || saving || loading || profileGenerating}
            >
              {profileGenerating ? (
                <Loader2 className="animate-spin" />
              ) : (
                <Sparkles />
              )}
              自动填写
            </Button>
          </div>
          <Textarea
            id="server-project-business-profile"
            className="min-h-44 resize-y"
            value={form.projectBusinessProfile}
            maxLength={30000}
            placeholder="例如：公司主要提供什么产品或服务、服务哪些客户、主要应用于哪些场景。"
            aria-invalid={Boolean(fieldErrors.projectBusinessProfile)}
            aria-describedby="server-project-business-profile-help"
            disabled={!metadata || saving || loading || profileGenerating}
            onChange={(event) => {
              setForm((current) => ({
                ...current,
                projectBusinessProfile: event.target.value,
              }));
              setFieldErrors((current) => ({
                ...current,
                projectBusinessProfile: undefined,
              }));
            }}
            onBlur={() => validateField("projectBusinessProfile")}
          />
          <div
            id="server-project-business-profile-help"
            className="flex flex-wrap justify-between gap-2 text-xs text-muted-foreground"
          >
            <span
              className={
                fieldErrors.projectBusinessProfile ? "text-destructive" : ""
              }
            >
              {fieldErrors.projectBusinessProfile ||
                "可手动填写，也可从当前项目已发布知识生成草稿。保存后用于限制标题、大纲和正文的业务范围，请人工核对后再保存。"}
            </span>
            <span>{form.projectBusinessProfile.length}/30000</span>
          </div>
        </div>

        <div className="grid min-w-0 gap-1.5">
          <Label htmlFor="server-project-notes">项目注意事项</Label>
          <Textarea
            id="server-project-notes"
            className="min-h-36 resize-y"
            value={form.projectNotes}
            maxLength={30000}
            placeholder="例如：避免提及零售价；未经证据支持不要声称认证；统一使用指定品牌术语。"
            aria-invalid={Boolean(fieldErrors.projectNotes)}
            aria-describedby="server-project-notes-help"
            disabled={!metadata || saving || loading}
            onChange={(event) => {
              setForm((current) => ({
                ...current,
                projectNotes: event.target.value,
              }));
              setFieldErrors((current) => ({
                ...current,
                projectNotes: undefined,
              }));
            }}
            onBlur={() => validateField("projectNotes")}
          />
          <div
            id="server-project-notes-help"
            className="flex flex-wrap justify-between gap-2 text-xs text-muted-foreground"
          >
            <span
              className={fieldErrors.projectNotes ? "text-destructive" : ""}
            >
              {fieldErrors.projectNotes ||
                "保存后，新建或导入的文章会捕获这份注意事项；已有文章保留自己的快照，不会被静默覆盖。"}
            </span>
            <span>{form.projectNotes.length}/30000</span>
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
            disabled={
              !metadata || !dirty || saving || loading || profileGenerating
            }
          >
            {saving ? <Loader2 className="animate-spin" /> : <Save />}
            保存项目资料
          </Button>
        </div>

        <p className="text-xs leading-5 text-muted-foreground">
          公司介绍与业务范围是生成背景，不是事实证据；自动填写只读取当前项目已发布 Knowledge 并生成待核对草稿。客户事实与产品资料仍应发布到 Knowledge，正式提示词规则仍由 Prompt Snapshot 管理。
        </p>
      </CardContent>
    </Card>
  );
}

export function ServerProjectSettings({
  projectId,
}: ServerProjectSettingsProps) {
  const [deleting, setDeleting] = useState(false);

  async function deleteProject() {
    if (deleting || !window.confirm("删除项目后，项目数据和可访问入口都会被移除，是否继续？")) {
      return;
    }
    setDeleting(true);
    try {
      await apiDelete(`/api/projects/${encodeURIComponent(projectId)}`);
      window.location.assign("/");
    } finally {
      setDeleting(false);
    }
  }

  return (
    <ServerProjectMembers
      projectId={projectId}
      pageKind="settings"
      beforeMemberCards={
        <>
          <ProjectMetadataCard projectId={projectId} />
          <ServerProjectPromptLibrary projectId={projectId} />
          <Card className="border-destructive/40">
            <CardHeader>
              <CardTitle className="flex items-center gap-2 text-destructive">
                <Trash2 className="size-4" />删除项目
              </CardTitle>
              <CardDescription>
                删除前会取消排队或运行中的项目任务，并保留必要的审计记录。此操作不可撤销。
              </CardDescription>
            </CardHeader>
            <CardContent>
              <Button
                type="button"
                variant="destructive"
                className="min-h-11"
                disabled={deleting}
                onClick={() => void deleteProject()}
              >
                {deleting ? <Loader2 className="animate-spin" /> : <Trash2 />}
                直接删除项目
              </Button>
            </CardContent>
          </Card>
        </>
      }
    />
  );
}
