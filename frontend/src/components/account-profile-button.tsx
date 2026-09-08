"use client";

import { Loader2, Save, UserRound } from "lucide-react";
import { useState } from "react";

import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
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
import { apiGet, apiPatch, apiPost } from "@/lib/api";
import type { AccountProfile } from "@/types";

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : "账户资料加载失败。";
}

export function AccountProfileButton({
  iconOnly = false,
}: {
  iconOnly?: boolean;
}) {
  const [open, setOpen] = useState(false);
  const [profile, setProfile] = useState<AccountProfile | null>(null);
  const [displayName, setDisplayName] = useState("");
  const [pending, setPending] = useState(false);
  const [passwordPending, setPasswordPending] = useState(false);
  const [currentPassword, setCurrentPassword] = useState("");
  const [newPassword, setNewPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [passwordMessage, setPasswordMessage] = useState("");
  const [error, setError] = useState("");

  async function openProfile() {
    setOpen(true);
    setPending(true);
    setError("");
    setPasswordMessage("");
    setCurrentPassword("");
    setNewPassword("");
    setConfirmPassword("");
    try {
      const next = await apiGet<AccountProfile>("/api/account/profile");
      setProfile(next);
      setDisplayName(next.display_name);
    } catch (nextError) {
      setError(errorMessage(nextError));
    } finally {
      setPending(false);
    }
  }

  async function save() {
    if (!displayName.trim()) return;
    setPending(true);
    setError("");
    try {
      const next = await apiPatch<AccountProfile>("/api/account/profile", {
        display_name: displayName,
      });
      setProfile(next);
      setDisplayName(next.display_name);
      setOpen(false);
    } catch (nextError) {
      setError(errorMessage(nextError));
    } finally {
      setPending(false);
    }
  }

  async function savePassword() {
    if (!profile || !currentPassword || !newPassword) return;
    if (newPassword !== confirmPassword) {
      setError("两次输入的新密码不一致。");
      return;
    }
    if (newPassword.length < 8) {
      setError("新密码至少需要 8 个字符。");
      return;
    }
    setPasswordPending(true);
    setError("");
    setPasswordMessage("");
    try {
      await apiPost<{ message: string }>("/api/account/password", {
        current_password: currentPassword,
        new_password: newPassword,
      });
      setCurrentPassword("");
      setNewPassword("");
      setConfirmPassword("");
      setPasswordMessage("密码已更新。");
    } catch (nextError) {
      setError(errorMessage(nextError));
    } finally {
      setPasswordPending(false);
    }
  }

  return (
    <>
      <Button
        type="button"
        size={iconOnly ? "icon-sm" : "default"}
        variant="outline"
        aria-label={iconOnly ? "账户资料" : undefined}
        onClick={() => void openProfile()}
      >
        <UserRound />
        {!iconOnly && "账户资料"}
      </Button>
      <Dialog open={open} onOpenChange={setOpen}>
        <DialogContent className="sm:max-w-md">
          <DialogHeader>
            <DialogTitle>账户资料</DialogTitle>
            <DialogDescription>
              这里可以更新显示名和当前账号密码。密码修改后，其他登录会话会失效。
            </DialogDescription>
          </DialogHeader>
          {error && (
            <Alert variant="destructive">
              <AlertTitle>操作失败</AlertTitle>
              <AlertDescription>{error}</AlertDescription>
            </Alert>
          )}
          <div className="grid gap-2">
            <Label htmlFor="account-profile-display-name">显示名</Label>
            <Input
              id="account-profile-display-name"
              value={displayName}
              maxLength={200}
              disabled={pending || !profile}
              placeholder="例如：周怡"
              onChange={(event) => setDisplayName(event.target.value)}
            />
            {profile && (
              <p className="text-xs text-muted-foreground">
                User ID：{profile.user_id}
              </p>
            )}
          </div>
          <div className="grid gap-3 border-t pt-4">
            <div>
              <h3 className="text-sm font-medium">修改密码</h3>
              <p className="mt-1 text-xs text-muted-foreground">
                新密码至少 8 个字符；修改后当前登录会话会自动续期。
              </p>
            </div>
            <div className="grid gap-2">
              <Label htmlFor="account-current-password">当前密码</Label>
              <Input
                id="account-current-password"
                type="password"
                autoComplete="current-password"
                value={currentPassword}
                disabled={pending || passwordPending || !profile}
                onChange={(event) => setCurrentPassword(event.target.value)}
              />
            </div>
            <div className="grid gap-2">
              <Label htmlFor="account-new-password">新密码</Label>
              <Input
                id="account-new-password"
                type="password"
                autoComplete="new-password"
                value={newPassword}
                disabled={pending || passwordPending || !profile}
                onChange={(event) => setNewPassword(event.target.value)}
              />
            </div>
            <div className="grid gap-2">
              <Label htmlFor="account-confirm-password">确认新密码</Label>
              <Input
                id="account-confirm-password"
                type="password"
                autoComplete="new-password"
                value={confirmPassword}
                disabled={pending || passwordPending || !profile}
                onChange={(event) => setConfirmPassword(event.target.value)}
              />
            </div>
            {passwordMessage && (
              <p className="text-sm text-emerald-600" role="status">
                {passwordMessage}
              </p>
            )}
            <Button
              type="button"
              variant="secondary"
              className="w-fit"
              disabled={
                pending ||
                passwordPending ||
                !profile ||
                !currentPassword ||
                !newPassword ||
                !confirmPassword
              }
              onClick={() => void savePassword()}
            >
              {passwordPending && <Loader2 className="animate-spin" />}
              保存新密码
            </Button>
          </div>
          <DialogFooter>
            <DialogClose
              render={
                <Button
                  type="button"
                  variant="outline"
                  disabled={pending || passwordPending}
                />
              }
            >
              取消
            </DialogClose>
            <Button
              type="button"
              disabled={
                pending || passwordPending || !profile || !displayName.trim()
              }
              onClick={() => void save()}
            >
              {pending ? <Loader2 className="animate-spin" /> : <Save />}
              保存显示名
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  );
}
