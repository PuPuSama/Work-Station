"use client";

import { LineDiffView } from "@/components/line-diff-view";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";

export function RevisionConflictDialog({
  open,
  message,
  localValue,
  serverValue,
  onAdoptServer,
  onKeepLocal,
}: {
  open: boolean;
  message: string;
  localValue: string;
  serverValue: string;
  onAdoptServer: () => void;
  onKeepLocal: () => void;
}) {
  return (
    <Dialog open={open}>
      <DialogContent className="sm:max-w-5xl" showCloseButton={false}>
        <DialogHeader>
          <DialogTitle>服务器内容已更新，请选择保留哪一版</DialogTitle>
          <DialogDescription>
            保存期间检测到修订号冲突。本地草稿没有被覆盖；请对照服务器最新版后再决定。
          </DialogDescription>
        </DialogHeader>
        <div className="rounded-lg border border-amber-500/40 bg-amber-50 p-3 text-xs text-amber-900">
          {message}
        </div>
        <LineDiffView localValue={localValue} serverValue={serverValue} />
        <div className="flex flex-col-reverse gap-2 border-t pt-3 sm:flex-row sm:justify-end">
          <Button variant="outline" onClick={onAdoptServer}>
            采用服务器版本
          </Button>
          <Button onClick={onKeepLocal}>
            保留本地修改，稍后重新保存
          </Button>
        </div>
      </DialogContent>
    </Dialog>
  );
}
