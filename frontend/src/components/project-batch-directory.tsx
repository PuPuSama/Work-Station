"use client";

import { Loader2 } from "lucide-react";
import { useEffect, useState } from "react";

import { ProjectBatchCenter } from "@/components/project-batch-center";
import { ServerProjectBatchCenter } from "@/components/server-project-batch-center";
import { apiGet } from "@/lib/api";
import type { AuthStatus } from "@/types";

export function ProjectBatchDirectory({ customer }: { customer: string }) {
  const [mode, setMode] = useState<"loading" | "local" | "server">("loading");
  const [error, setError] = useState("");

  useEffect(() => {
    let active = true;
    void apiGet<AuthStatus>("/api/auth/status")
      .then((status) => {
        if (!active) return;
        const isServer = status.data?.mode === "server";
        const isLocal =
          status.data?.mode === undefined &&
          typeof status.data?.enabled === "boolean";
        if (!isServer && !isLocal) {
          throw new Error("无法识别当前工作区模式。");
        }
        setMode(isServer ? "server" : "local");
      })
      .catch((reason: unknown) => {
        if (!active) return;
        setError(
          reason instanceof Error ? reason.message : "无法确认工作区模式。",
        );
      });
    return () => {
      active = false;
    };
  }, []);

  if (mode === "server") {
    return <ServerProjectBatchCenter customer={customer} />;
  }
  if (mode === "local") {
    return <ProjectBatchCenter customer={customer} />;
  }
  return (
    <main className="flex min-h-[50dvh] items-center justify-center px-5">
      <div
        className="flex max-w-md items-center gap-3 rounded-xl border bg-card px-5 py-4 text-sm text-muted-foreground"
        role={error ? "alert" : "status"}
        aria-live="polite"
      >
        {!error && <Loader2 className="size-4 animate-spin" />}
        {error || "正在确认 Local / Server 批次准源…"}
      </div>
    </main>
  );
}
