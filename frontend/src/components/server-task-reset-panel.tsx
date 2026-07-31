"use client";

import { AlertTriangle, Loader2, RotateCcw } from "lucide-react";
import { useState } from "react";

import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { apiPost } from "@/lib/api";
import type { TaskRecord } from "@/types";

type ServerTaskResetPanelProps = {
  task: TaskRecord;
  taskApi: string;
  pending: string;
  editAllowed: boolean;
  resetAllowed: boolean;
  runAction: (
    label: string,
    action: () => Promise<unknown>,
    successMessage?: string,
  ) => Promise<boolean>;
  onCompleted: () => void;
};

export function ServerTaskResetPanel({
  task,
  taskApi,
  pending,
  editAllowed,
  resetAllowed,
  runAction,
  onCompleted,
}: ServerTaskResetPanelProps) {
  const [confirmed, setConfirmed] = useState(false);
  const label = "完全重写任务";

  return (
    <Card>
      <CardHeader className="border-b">
        <CardTitle>完全重写</CardTitle>
        <CardDescription>
          把 Task 回退到标题起点；这是确定性状态重置，不调用模型，也不删除服务器历史对象。
        </CardDescription>
      </CardHeader>
      <CardContent className="grid gap-4">
        <Alert variant="destructive">
          <AlertTriangle />
          <AlertTitle>会清空当前生产链状态</AlertTitle>
          <AlertDescription>
            当前标题选择、产品、大纲、正文、AI 检查、链接、图片和交付引用都会失效。知识库来源、
            产品目录、不可变审计和对象存储历史不会被浏览器删除。
          </AlertDescription>
        </Alert>
        <label className="flex min-h-11 cursor-pointer items-center gap-3 rounded-md border px-3 text-sm">
          <input
            type="checkbox"
            checked={confirmed}
            disabled={Boolean(pending) || !editAllowed || !resetAllowed}
            onChange={(event) => setConfirmed(event.target.checked)}
          />
          我确认从标题阶段重新开始，并接受当前下游产物失效
        </label>
        <Button
          type="button"
          variant="destructive"
          className="min-h-11"
          disabled={
            Boolean(pending) ||
            !editAllowed ||
            !resetAllowed ||
            !confirmed
          }
          onClick={() =>
            void runAction(
              label,
              () =>
                apiPost<TaskRecord>(`${taskApi}/rewrite-from-scratch`, {
                  revision: task.revision ?? 0,
                }),
              "Task 已回退到标题阶段；服务器已读取最新 Revision。",
            ).then((succeeded) => {
              if (!succeeded) return;
              setConfirmed(false);
              onCompleted();
            })
          }
        >
          {pending === label ? (
            <Loader2 className="animate-spin" />
          ) : (
            <RotateCcw />
          )}
          确认完全重写
        </Button>
      </CardContent>
    </Card>
  );
}
