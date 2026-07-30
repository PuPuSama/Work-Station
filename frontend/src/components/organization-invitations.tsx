"use client";

/* eslint-disable react-hooks/set-state-in-effect */

import {
  AlertCircle,
  CheckCircle2,
  Clipboard,
  Clock3,
  KeyRound,
  Loader2,
  MailPlus,
  RefreshCw,
  ShieldX,
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
  IssuedWorkspaceInvitation,
  WorkspaceInvitation,
  WorkspaceInvitationPage,
  WorkspaceUser,
} from "@/types";

const selectClass =
  "h-11 w-full cursor-pointer rounded-lg border border-input bg-background px-3 text-sm outline-none focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/50 disabled:cursor-not-allowed disabled:opacity-50";

type Feedback = { kind: "success" | "error"; message: string } | null;

function errorMessage(error: unknown, fallback: string) {
  return error instanceof Error && error.message ? error.message : fallback;
}

function formatDate(value: string) {
  const parsed = new Date(value);
  return Number.isNaN(parsed.valueOf())
    ? value
    : new Intl.DateTimeFormat("zh-CN", {
        dateStyle: "medium",
        timeStyle: "short",
      }).format(parsed);
}

function mergeInvitations(
  current: WorkspaceInvitation[],
  next: WorkspaceInvitation[],
) {
  const merged = new Map(
    current.map((invitation) => [
      invitation.invitation_id,
      invitation,
    ]),
  );
  next.forEach((invitation) =>
    merged.set(invitation.invitation_id, invitation),
  );
  return Array.from(merged.values()).sort((a, b) =>
    a.invitation_id.localeCompare(b.invitation_id),
  );
}

export function OrganizationInvitations({
  organizationId,
  users,
}: {
  organizationId: string;
  users: WorkspaceUser[];
}) {
  const encodedOrg = encodeURIComponent(organizationId);
  const [invitations, setInvitations] = useState<WorkspaceInvitation[]>([]);
  const [cursor, setCursor] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [pending, setPending] = useState("");
  const [feedback, setFeedback] = useState<Feedback>(null);
  const [issued, setIssued] = useState<IssuedWorkspaceInvitation | null>(
    null,
  );
  const [revokeTarget, setRevokeTarget] =
    useState<WorkspaceInvitation | null>(null);
  const [form, setForm] = useState({
    user_id: "",
    issuer: "",
    expires_in_hours: "24",
  });

  const activeUsers = useMemo(
    () =>
      users
        .filter((user) => user.status === "active" && !user.login_linked)
        .sort((a, b) => a.display_name.localeCompare(b.display_name)),
    [users],
  );

  const loadInvitations = useCallback(async () => {
    setLoading(true);
    try {
      const page = await apiGet<WorkspaceInvitationPage>(
        `/api/organizations/${encodedOrg}/invitations?limit=50`,
      );
      setInvitations(page.items);
      setCursor(page.next_after_invitation_id);
    } catch (error) {
      setFeedback({
        kind: "error",
        message: errorMessage(error, "邀请目录加载失败。"),
      });
    } finally {
      setLoading(false);
    }
  }, [encodedOrg]);

  useEffect(() => {
    void loadInvitations();
  }, [loadInvitations]);

  async function issueInvitation() {
    if (!form.user_id || !form.issuer.trim()) return;
    setPending("issue");
    setFeedback(null);
    setIssued(null);
    try {
      const result = await apiPost<IssuedWorkspaceInvitation>(
        `/api/organizations/${encodedOrg}/invitations`,
        {
          user_id: form.user_id,
          issuer: form.issuer,
          expires_in_hours: Number(form.expires_in_hours),
        },
      );
      setIssued(result);
      setForm((current) => ({ ...current, user_id: "" }));
      await loadInvitations();
      setFeedback({
        kind: "success",
        message: "邀请已签发。请现在复制一次性 Token。",
      });
    } catch (error) {
      setFeedback({
        kind: "error",
        message: errorMessage(error, "邀请签发失败。"),
      });
    } finally {
      setPending("");
    }
  }

  async function copyToken() {
    if (!issued) return;
    try {
      await navigator.clipboard.writeText(issued.invitation_token);
      setFeedback({
        kind: "success",
        message: "一次性 Token 已复制到剪贴板。",
      });
    } catch {
      setFeedback({
        kind: "error",
        message: "浏览器未允许写入剪贴板，请手动选择并复制 Token。",
      });
    }
  }

  async function revokeInvitation() {
    if (!revokeTarget) return;
    setPending(`revoke-${revokeTarget.invitation_id}`);
    setFeedback(null);
    try {
      await apiDelete(
        `/api/organizations/${encodedOrg}/invitations/${encodeURIComponent(revokeTarget.invitation_id)}`,
      );
      setRevokeTarget(null);
      await loadInvitations();
      setFeedback({
        kind: "success",
        message: "邀请已撤销；对应 Token 不能再兑换。",
      });
    } catch (error) {
      setFeedback({
        kind: "error",
        message: errorMessage(error, "邀请撤销失败。"),
      });
    } finally {
      setPending("");
    }
  }

  async function loadMore() {
    if (!cursor) return;
    setPending("load-more");
    try {
      const page = await apiGet<WorkspaceInvitationPage>(
        `/api/organizations/${encodedOrg}/invitations?limit=50&after_invitation_id=${encodeURIComponent(cursor)}`,
      );
      setInvitations((current) =>
        mergeInvitations(current, page.items),
      );
      setCursor(page.next_after_invitation_id);
    } catch (error) {
      setFeedback({
        kind: "error",
        message: errorMessage(error, "更多邀请加载失败。"),
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
            {feedback.kind === "error" ? "邀请操作失败" : "邀请操作完成"}
          </AlertTitle>
          <AlertDescription>{feedback.message}</AlertDescription>
        </Alert>
      )}

      {issued && (
        <Alert aria-live="polite">
          <KeyRound />
          <AlertTitle>一次性邀请 Token</AlertTitle>
          <AlertDescription className="grid gap-3">
            <p>
              这是服务端唯一一次返回原始 Token。关闭或刷新页面后无法再次读取；请通过受信渠道交给
              {issued.user_display_name}。
            </p>
            <div className="flex flex-col gap-2 sm:flex-row">
              <Input
                readOnly
                aria-label="一次性邀请 Token"
                className="min-w-0 font-mono text-xs"
                value={issued.invitation_token}
                onFocus={(event) => event.currentTarget.select()}
              />
              <Button
                type="button"
                variant="outline"
                className="min-h-11 shrink-0"
                onClick={() => void copyToken()}
              >
                <Clipboard />
                复制 Token
              </Button>
              <Button
                type="button"
                variant="ghost"
                className="min-h-11 shrink-0"
                onClick={() => setIssued(null)}
              >
                已安全保存
              </Button>
            </div>
            <p className="text-xs text-muted-foreground">
              领取人可在 <span className="font-mono">/accept-invite</span>
              粘贴 Token；受控分享链接应把 Token 放在 URL Fragment，而不是查询参数。
            </p>
          </AlertDescription>
        </Alert>
      )}

      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <MailPlus className="size-4" />
            签发工作区邀请
          </CardTitle>
          <CardDescription>
            邀请绑定现有 Active 本地账号与预期 OIDC Issuer；系统不使用或信任外部 Email、Group、Role。
          </CardDescription>
        </CardHeader>
        <CardContent className="grid gap-4">
          <div className="grid gap-4 lg:grid-cols-[1fr_1.4fr_0.6fr]">
            <div className="grid gap-2">
              <Label htmlFor="invitation-user">目标本地账号</Label>
              <select
                id="invitation-user"
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
                <option value="">选择未关联登录的 Active 账号</option>
                {activeUsers.map((user) => (
                  <option key={user.user_id} value={user.user_id}>
                    {user.display_name} · {user.user_id}
                  </option>
                ))}
              </select>
              <p className="text-xs leading-5 text-muted-foreground">
                若账号未出现在这里，请先加载完整账号目录或检查其状态与登录关联。
              </p>
            </div>
            <div className="grid gap-2">
              <Label htmlFor="invitation-issuer">预期 Issuer URL</Label>
              <Input
                id="invitation-issuer"
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
              <Label htmlFor="invitation-hours">有效时间</Label>
              <select
                id="invitation-hours"
                className={selectClass}
                value={form.expires_in_hours}
                disabled={disabled}
                onChange={(event) =>
                  setForm((current) => ({
                    ...current,
                    expires_in_hours: event.target.value,
                  }))
                }
              >
                <option value="1">1 小时</option>
                <option value="24">24 小时</option>
                <option value="72">3 天</option>
                <option value="168">7 天</option>
              </select>
            </div>
          </div>
          <div className="flex justify-end border-t pt-4">
            <Button
              type="button"
              className="min-h-11"
              disabled={
                disabled || !form.user_id || !form.issuer.trim()
              }
              onClick={() => void issueInvitation()}
            >
              {pending === "issue" ? (
                <Loader2 className="animate-spin" />
              ) : (
                <MailPlus />
              )}
              签发邀请
            </Button>
          </div>
        </CardContent>
      </Card>

      <Card>
        <CardHeader className="gap-3 sm:flex-row sm:items-start sm:justify-between">
          <div>
            <CardTitle>邀请目录</CardTitle>
            <CardDescription>
              目录保留 Pending、Expired、Accepted 与 Revoked 状态，但永不返回 Token 或 Token Hash。
            </CardDescription>
          </div>
          <Button
            type="button"
            variant="outline"
            className="min-h-11 shrink-0"
            disabled={disabled || loading}
            onClick={() => void loadInvitations()}
          >
            {loading ? <Loader2 className="animate-spin" /> : <RefreshCw />}
            刷新邀请
          </Button>
        </CardHeader>
        <CardContent className="grid gap-3">
          {loading && !invitations.length ? (
            <div className="grid gap-3" role="status" aria-live="polite">
              <span className="sr-only">正在读取邀请目录…</span>
              <Skeleton className="h-24 w-full" />
              <Skeleton className="h-24 w-full" />
            </div>
          ) : invitations.length ? (
            invitations.map((invitation) => {
              const revocable =
                invitation.status === "pending" ||
                invitation.status === "expired";
              return (
                <article
                  key={invitation.invitation_id}
                  className="grid gap-4 rounded-xl border p-4 lg:grid-cols-[minmax(0,1.3fr)_minmax(0,1fr)_auto] lg:items-center"
                >
                  <div className="min-w-0">
                    <div className="flex flex-wrap items-center gap-2">
                      <p className="font-medium">
                        {invitation.user_display_name}
                      </p>
                      <Badge
                        variant={
                          invitation.status === "pending"
                            ? "secondary"
                            : invitation.status === "expired"
                              ? "destructive"
                              : "outline"
                        }
                      >
                        {invitation.status}
                      </Badge>
                    </div>
                    <p className="mt-1 truncate text-sm text-muted-foreground">
                      {invitation.issuer}
                    </p>
                    <p className="mt-1 truncate font-mono text-xs text-muted-foreground">
                      {invitation.user_id}
                    </p>
                  </div>
                  <div className="min-w-0">
                    <p className="flex items-center gap-1 text-xs font-medium text-muted-foreground">
                      <Clock3 className="size-3" />
                      到期时间
                    </p>
                    <p className="mt-1 text-sm">
                      {formatDate(invitation.expires_at)}
                    </p>
                    <p
                      className="mt-1 truncate font-mono text-xs text-muted-foreground"
                      title={invitation.invitation_id}
                    >
                      {invitation.invitation_id}
                    </p>
                  </div>
                  {revocable ? (
                    <Button
                      type="button"
                      variant="outline"
                      className="min-h-11"
                      disabled={disabled}
                      onClick={() => setRevokeTarget(invitation)}
                    >
                      <ShieldX />
                      撤销邀请
                    </Button>
                  ) : (
                    <span className="text-xs text-muted-foreground">
                      已保留历史记录
                    </span>
                  )}
                </article>
              );
            })
          ) : (
            <div className="rounded-xl border border-dashed px-4 py-10 text-center">
              <MailPlus className="mx-auto size-5 text-muted-foreground" />
              <p className="mt-3 text-sm font-medium">尚无工作区邀请</p>
              <p className="mt-1 text-xs text-muted-foreground">
                使用上方表单签发第一条一次性邀请。
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
              加载更多邀请
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
            <DialogTitle>撤销这个邀请？</DialogTitle>
            <DialogDescription>
              {revokeTarget?.user_display_name} 的对应 Token
              将立即失效，已开始但尚未完成的 OIDC 登录也不能兑换。
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
              onClick={() => void revokeInvitation()}
            >
              {pending.startsWith("revoke-") ? (
                <Loader2 className="animate-spin" />
              ) : (
                <ShieldX />
              )}
              确认撤销
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}
