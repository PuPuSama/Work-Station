"use client";

/* eslint-disable react-hooks/set-state-in-effect */

import {
  AlertCircle,
  CheckCircle2,
  Fingerprint,
  Link2,
  Loader2,
  RefreshCw,
  ShieldOff,
} from "lucide-react";
import { useCallback, useEffect, useMemo, useState } from "react";

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
import {
  Dialog,
  DialogClose,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Skeleton } from "@/components/ui/skeleton";
import { apiDelete, apiGet, apiPost } from "@/lib/api";
import type {
  ExternalIdentityMapping,
  ExternalIdentityMappingPage,
  WorkspaceUser,
} from "@/types";

const selectClass =
  "h-11 w-full cursor-pointer rounded-lg border border-input bg-background px-3 text-sm outline-none focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/50 disabled:cursor-not-allowed disabled:opacity-50";

type Feedback = { kind: "success" | "error"; message: string } | null;

function errorMessage(error: unknown, fallback: string) {
  return error instanceof Error && error.message ? error.message : fallback;
}

function mergeMappings(
  current: ExternalIdentityMapping[],
  next: ExternalIdentityMapping[],
) {
  const merged = new Map(
    current.map((mapping) => [mapping.mapping_id, mapping]),
  );
  next.forEach((mapping) => merged.set(mapping.mapping_id, mapping));
  return Array.from(merged.values()).sort((a, b) =>
    a.mapping_id.localeCompare(b.mapping_id),
  );
}

export function OrganizationExternalIdentities({
  organizationId,
  users,
}: {
  organizationId: string;
  users: WorkspaceUser[];
}) {
  const encodedOrg = encodeURIComponent(organizationId);
  const [mappings, setMappings] = useState<ExternalIdentityMapping[]>([]);
  const [cursor, setCursor] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [pending, setPending] = useState("");
  const [feedback, setFeedback] = useState<Feedback>(null);
  const [revokeTarget, setRevokeTarget] =
    useState<ExternalIdentityMapping | null>(null);
  const [form, setForm] = useState({
    issuer: "",
    subject: "",
    user_id: "",
  });

  const activeUsers = useMemo(
    () =>
      users
        .filter((user) => user.status === "active")
        .sort((a, b) => a.display_name.localeCompare(b.display_name)),
    [users],
  );

  const loadMappings = useCallback(async () => {
    setLoading(true);
    setFeedback(null);
    try {
      const page = await apiGet<ExternalIdentityMappingPage>(
        `/api/organizations/${encodedOrg}/external-identities?limit=50`,
      );
      setMappings(page.items);
      setCursor(page.next_after_mapping_id);
    } catch (error) {
      setFeedback({
        kind: "error",
        message: errorMessage(error, "外部身份目录加载失败。"),
      });
    } finally {
      setLoading(false);
    }
  }, [encodedOrg]);

  useEffect(() => {
    void loadMappings();
  }, [loadMappings]);

  async function loadMore() {
    if (!cursor) return;
    setPending("load-more");
    try {
      const page = await apiGet<ExternalIdentityMappingPage>(
        `/api/organizations/${encodedOrg}/external-identities?limit=50&after_mapping_id=${encodeURIComponent(cursor)}`,
      );
      setMappings((current) => mergeMappings(current, page.items));
      setCursor(page.next_after_mapping_id);
    } catch (error) {
      setFeedback({
        kind: "error",
        message: errorMessage(error, "更多身份映射加载失败。"),
      });
    } finally {
      setPending("");
    }
  }

  async function linkIdentity() {
    if (!form.issuer.trim() || !form.subject.trim() || !form.user_id) return;
    setPending("link");
    setFeedback(null);
    try {
      await apiPost(
        `/api/organizations/${encodedOrg}/external-identities`,
        form,
      );
      setForm({ issuer: "", subject: "", user_id: "" });
      await loadMappings();
      setFeedback({
        kind: "success",
        message: "外部身份已关联；原始 Subject 未写入页面目录。",
      });
    } catch (error) {
      setFeedback({
        kind: "error",
        message: errorMessage(error, "外部身份关联失败。"),
      });
    } finally {
      setPending("");
    }
  }

  async function revokeIdentity() {
    if (!revokeTarget) return;
    setPending(`revoke-${revokeTarget.mapping_id}`);
    setFeedback(null);
    try {
      await apiDelete(
        `/api/organizations/${encodedOrg}/external-identities/${encodeURIComponent(revokeTarget.mapping_id)}`,
      );
      setRevokeTarget(null);
      await loadMappings();
      setFeedback({
        kind: "success",
        message: "外部身份映射已撤销，后续登录不会再解析到该账号。",
      });
    } catch (error) {
      setFeedback({
        kind: "error",
        message: errorMessage(error, "外部身份撤销失败。"),
      });
    } finally {
      setPending("");
    }
  }

  const disabled = Boolean(pending);

  return (
    <div className="grid gap-4">
      {feedback && (
        <Alert
          variant={feedback.kind === "error" ? "destructive" : "default"}
          aria-live="polite"
        >
          {feedback.kind === "error" ? <AlertCircle /> : <CheckCircle2 />}
          <AlertTitle>
            {feedback.kind === "error" ? "身份操作失败" : "身份操作完成"}
          </AlertTitle>
          <AlertDescription>{feedback.message}</AlertDescription>
        </Alert>
      )}

      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <Link2 className="size-4" />
            关联外部登录身份
          </CardTitle>
          <CardDescription>
            输入身份提供方的 Issuer 与 Subject，并关联到一个 Active
            本地账号。同一用户在同一 Issuer 下只能关联一个身份。
          </CardDescription>
        </CardHeader>
        <CardContent className="grid gap-4">
          <div className="grid gap-4 lg:grid-cols-3">
            <div className="grid gap-2">
              <Label htmlFor="identity-issuer">Issuer URL</Label>
              <Input
                id="identity-issuer"
                type="url"
                autoComplete="off"
                placeholder="https://id.example.com"
                value={form.issuer}
                disabled={disabled}
                onChange={(event) =>
                  setForm((current) => ({
                    ...current,
                    issuer: event.target.value,
                  }))
                }
              />
            </div>
            <div className="grid gap-2">
              <Label htmlFor="identity-subject">Subject</Label>
              <Input
                id="identity-subject"
                type="password"
                autoComplete="off"
                placeholder="仅在本次绑定时提交"
                value={form.subject}
                disabled={disabled}
                onChange={(event) =>
                  setForm((current) => ({
                    ...current,
                    subject: event.target.value,
                  }))
                }
              />
            </div>
            <div className="grid gap-2">
              <Label htmlFor="identity-user">本地账号</Label>
              <select
                id="identity-user"
                className={selectClass}
                value={form.user_id}
                disabled={disabled}
                onChange={(event) =>
                  setForm((current) => ({
                    ...current,
                    user_id: event.target.value,
                  }))
                }
              >
                <option value="">选择 Active 账号</option>
                {activeUsers.map((user) => (
                  <option key={user.user_id} value={user.user_id}>
                    {user.display_name} · {user.user_id}
                  </option>
                ))}
              </select>
            </div>
          </div>
          <div className="flex flex-col gap-3 border-t pt-4 sm:flex-row sm:items-center sm:justify-between">
            <p className="max-w-3xl text-xs leading-5 text-muted-foreground">
              Subject 属于私密身份标识：服务端仅在绑定时接收，目录、撤销请求和审计记录均不返回它。
            </p>
            <Button
              type="button"
              className="min-h-11 shrink-0"
              disabled={
                disabled ||
                !form.issuer.trim() ||
                !form.subject.trim() ||
                !form.user_id
              }
              onClick={() => void linkIdentity()}
            >
              {pending === "link" ? (
                <Loader2 className="animate-spin" />
              ) : (
                <Fingerprint />
              )}
              关联身份
            </Button>
          </div>
        </CardContent>
      </Card>

      <Card>
        <CardHeader className="gap-3 sm:flex-row sm:items-start sm:justify-between">
          <div>
            <CardTitle>身份映射目录</CardTitle>
            <CardDescription>
              显示 Active 与 Revoked 映射，便于追踪历史状态；目录只展示不可逆映射 ID。
            </CardDescription>
          </div>
          <Button
            type="button"
            variant="outline"
            className="min-h-11 shrink-0"
            disabled={disabled || loading}
            onClick={() => void loadMappings()}
          >
            {loading ? (
              <Loader2 className="animate-spin" />
            ) : (
              <RefreshCw />
            )}
            刷新身份
          </Button>
        </CardHeader>
        <CardContent className="grid gap-3">
          {loading && !mappings.length ? (
            <div className="grid gap-3" role="status" aria-live="polite">
              <span className="sr-only">正在读取外部身份映射…</span>
              <Skeleton className="h-24 w-full" />
              <Skeleton className="h-24 w-full" />
            </div>
          ) : mappings.length ? (
            mappings.map((mapping) => (
              <article
                key={mapping.mapping_id}
                className="grid gap-4 rounded-xl border p-4 lg:grid-cols-[minmax(0,1.4fr)_minmax(0,1fr)_auto] lg:items-center"
              >
                <div className="min-w-0">
                  <div className="flex flex-wrap items-center gap-2">
                    <p className="font-medium">
                      {mapping.user_display_name}
                    </p>
                    <Badge
                      variant={
                        mapping.status === "active"
                          ? "secondary"
                          : "outline"
                      }
                    >
                      {mapping.status === "active" ? "Active" : "Revoked"}
                    </Badge>
                    {mapping.user_status === "disabled" && (
                      <Badge variant="destructive">账号已停用</Badge>
                    )}
                  </div>
                  <p className="mt-1 truncate text-sm text-muted-foreground">
                    {mapping.issuer}
                  </p>
                  <p className="mt-1 truncate font-mono text-xs text-muted-foreground">
                    {mapping.user_id}
                  </p>
                </div>
                <div className="min-w-0">
                  <p className="text-xs font-medium text-muted-foreground">
                    Mapping ID
                  </p>
                  <p
                    className="mt-1 truncate font-mono text-xs"
                    title={mapping.mapping_id}
                  >
                    {mapping.mapping_id}
                  </p>
                </div>
                {mapping.status === "active" ? (
                  <Button
                    type="button"
                    variant="outline"
                    className="min-h-11"
                    disabled={disabled}
                    onClick={() => setRevokeTarget(mapping)}
                  >
                    <ShieldOff />
                    撤销映射
                  </Button>
                ) : (
                  <span className="text-xs text-muted-foreground">
                    已保留历史记录
                  </span>
                )}
              </article>
            ))
          ) : (
            <div className="rounded-xl border border-dashed px-4 py-10 text-center">
              <Fingerprint className="mx-auto size-5 text-muted-foreground" />
              <p className="mt-3 text-sm font-medium">尚无外部身份映射</p>
              <p className="mt-1 text-xs text-muted-foreground">
                使用上方表单关联第一个登录身份。
              </p>
            </div>
          )}
          {cursor && (
            <Button
              type="button"
              variant="outline"
              className="min-h-11 w-fit"
              disabled={disabled}
              onClick={() => void loadMore()}
            >
              {pending === "load-more" && (
                <Loader2 className="animate-spin" />
              )}
              加载更多映射
            </Button>
          )}
        </CardContent>
      </Card>

      <Dialog
        open={revokeTarget !== null}
        onOpenChange={(open) => !open && !disabled && setRevokeTarget(null)}
      >
        <DialogContent showCloseButton={false}>
          <DialogHeader>
            <DialogTitle>撤销这个外部身份映射？</DialogTitle>
            <DialogDescription>
              {revokeTarget?.user_display_name} 将不能再通过该映射登录。此操作只使用
              Mapping ID，不会读取或显示原始 Subject。
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <DialogClose
              render={
                <Button
                  type="button"
                  variant="outline"
                  className="min-h-11"
                  disabled={disabled}
                />
              }
            >
              取消
            </DialogClose>
            <Button
              type="button"
              variant="destructive"
              className="min-h-11"
              disabled={disabled}
              onClick={() => void revokeIdentity()}
            >
              {pending.startsWith("revoke-") ? (
                <Loader2 className="animate-spin" />
              ) : (
                <ShieldOff />
              )}
              确认撤销
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}
