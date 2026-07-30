"use client";

/* eslint-disable react-hooks/set-state-in-effect */

import { AlertCircle, ArrowLeft, Loader2, RefreshCw } from "lucide-react";
import Link from "next/link";
import { useCallback, useEffect, useState } from "react";

import { OrganizationAdminConsole } from "@/components/organization-admin-console";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { apiGet } from "@/lib/api";
import type { AuthStatus } from "@/types";

export function OrganizationAdminEntry() {
  const [organizationId, setOrganizationId] = useState<string | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);

  const resolve = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const status = await apiGet<AuthStatus>("/api/auth/status");
      if (status.data?.mode !== "server") {
        throw new Error("组织管理仅在 Server 模式开放。");
      }
      if (!status.data.authenticated || !status.data.organization_id) {
        throw new Error("当前组织身份不可用，请重新登录。");
      }
      setOrganizationId(status.data.organization_id);
    } catch (nextError) {
      setError(
        nextError instanceof Error
          ? nextError.message
          : "无法确认当前组织身份。",
      );
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void resolve();
  }, [resolve]);

  if (organizationId) {
    return <OrganizationAdminConsole organizationId={organizationId} />;
  }
  return (
    <main className="flex min-h-dvh items-center justify-center bg-background px-5">
      {loading ? (
        <div
          className="flex items-center gap-2 text-sm text-muted-foreground"
          role="status"
          aria-live="polite"
        >
          <Loader2 className="size-4 animate-spin" />
          正在确认组织管理权限…
        </div>
      ) : (
        <Alert variant="destructive" className="max-w-lg">
          <AlertCircle />
          <AlertTitle>无法打开组织管理</AlertTitle>
          <AlertDescription className="grid gap-3">
            <span>{error}</span>
            <div className="flex flex-wrap gap-2">
              <Button
                type="button"
                variant="outline"
                className="min-h-11"
                onClick={() => void resolve()}
              >
                <RefreshCw />
                重试
              </Button>
              <Button
                nativeButton={false}
                variant="outline"
                className="min-h-11"
                render={<Link href="/" />}
              >
                <ArrowLeft />
                返回项目
              </Button>
            </div>
          </AlertDescription>
        </Alert>
      )}
    </main>
  );
}
