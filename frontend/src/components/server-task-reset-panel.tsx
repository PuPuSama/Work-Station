"use client";

import { AlertTriangle, Loader2, RotateCcw } from "lucide-react";
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
  DialogTrigger,
} from "@/components/ui/dialog";
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
  const [open, setOpen] = useState(false);
  const label = "完全重写任务";

  function closeDialog() {
    if (pending) return;
    setOpen(false);
    setConfirmed(false);
  }

  return (
    <Dialog
      open={open}
      onOpenChange={(nextOpen) => {
        if (nextOpen) {
          setOpen(true);
          return;
        }
        closeDialog();
      }}
    >
      <DialogTrigger
        render={
          <Button
            type="button"
            variant="outline"
            className="min-h-11 text-destructive hover:text-destructive"
            disabled={Boolean(pending) || !editAllowed || !resetAllowed}
            title={
              !editAllowed || !resetAllowed
                ? "当前账号无权执行完全重写"
                : undefined
            }
          />
        }
      >
        <RotateCcw />
        重写
      </DialogTrigger>
      <DialogContent className="sm:max-w-lg" showCloseButton={false}>
        <DialogHeader>
          <DialogTitle>完全重写任务</DialogTitle>
          <DialogDescription>
            把 Task 回退到标题起点；这是确定性状态重置，不调用模型，也不删除服务器历史对象。
          </DialogDescription>
        </DialogHeader>
        <Alert variant="destructive">
          <AlertTriangle />
          <AlertTitle>会清空当前生产链状态</AlertTitle>
          <AlertDescription>
            当前标题选择、产品、大纲、正文、AI 检查、链接、图片和交付引用都会失效。知识库来源、
            产品目录、不可变审计和对象存储历史不会被浏览器删除。
          </AlertDescription>
        </Alert>
        <label
          htmlFor="server-task-reset-confirm"
          className="flex min-h-11 cursor-pointer items-center gap-3 rounded-md border px-3 text-sm"
        >
          <input
            id="server-task-reset-confirm"
            type="checkbox"
            checked={confirmed}
            disabled={Boolean(pending) || !editAllowed || !resetAllowed}
            onChange={(event) => setConfirmed(event.target.checked)}
          />
          我确认从标题阶段重新开始，并接受当前下游产物失效
        </label>
        <DialogFooter>
          <DialogClose
            render={
              <Button
                type="button"
                variant="outline"
                className="min-h-11"
                disabled={Boolean(pending)}
              />
            }
          >
            取消
          </DialogClose>
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
                setOpen(false);
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
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
