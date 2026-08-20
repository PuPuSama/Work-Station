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
import { apiGet, apiPatch } from "@/lib/api";
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
  const [error, setError] = useState("");

  async function openProfile() {
    setOpen(true);
    setPending(true);
    setError("");
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
              这里的显示名会用于组织成员列表、项目协作和审计记录。登录身份本身不会被修改。
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
          <DialogFooter>
            <DialogClose
              render={
                <Button type="button" variant="outline" disabled={pending} />
              }
            >
              取消
            </DialogClose>
            <Button
              type="button"
              disabled={pending || !profile || !displayName.trim()}
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
