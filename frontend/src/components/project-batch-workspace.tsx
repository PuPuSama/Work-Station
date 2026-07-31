"use client";

import { Loader2 } from "lucide-react";
import { useEffect, useState } from "react";

import { ProjectBatchDetail } from "@/components/project-batch-detail";
import { ServerProjectBatchDetail } from "@/components/server-project-batch-detail";
import { apiGet } from "@/lib/api";
import type { AuthStatus } from "@/types";

export function ProjectBatchWorkspace({
  customer,
  batchId,
}: {
  customer: string;
  batchId: string;
}) {
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
    return <ServerProjectBatchDetail customer={customer} batchId={batchId} />;
  }
  if (mode === "local") {
    return <ProjectBatchDetail customer={customer} batchId={batchId} />;
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
