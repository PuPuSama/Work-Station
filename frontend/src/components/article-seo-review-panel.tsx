"use client";

/* eslint-disable react-hooks/set-state-in-effect */

import {
  AlertTriangle,
  Check,
  ChevronDown,
  ClipboardCheck,
  Eye,
  History,
  Loader2,
  Save,
  ShieldAlert,
  Sparkles,
  X,
} from "lucide-react";
import { useCallback, useEffect, useMemo, useState } from "react";

import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { formatProjectDate } from "@/lib/project-date";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { Textarea } from "@/components/ui/textarea";
import { apiGet } from "@/lib/api";
import type {
  ProjectPromptLibrary,
  SeoReviewChange,
  SeoReviewPreview,
  SeoReviewRisk,
  SeoReviewRun,
  TaskRecord,
} from "@/types";

export type SeoReviewSettings = {
  primaryKeyword: string;
  longTailKeywords: string[];
  promptSelection: string;
};

export type SeoReviewChangeDecision = "pending" | "accepted" | "rejected";

function errorMessage(error: unknown) {
  return error instanceof Error ? error.message : "SEO 复检操作失败。";
}

function parseKeywords(value: string) {
  const result: string[] = [];
  const seen = new Set<string>();
  for (const item of value.split(/[\n,，、]+/)) {
    const keyword = item.replace(/\s+/g, " ").trim();
    const key = keyword.toLocaleLowerCase();
    if (!keyword || seen.has(key)) continue;
    seen.add(key);
    result.push(keyword);
  }
  return result.slice(0, 30);
}

function scoreVariant(score: number) {
  if (score >= 80) return "default" as const;
  if (score >= 70) return "secondary" as const;
  return "destructive" as const;
}

function changeOperationLabel(change: SeoReviewChange) {
  return {
    replace: "替换段落",
    insert_after: "新增段落",
    delete: "删除段落",
    structure: "结构调整组",
  }[change.operation];
}

function decisionLabel(decision: SeoReviewChangeDecision) {
  return {
    pending: "暂未处理",
    accepted: "已接受",
    rejected: "已拒绝",
  }[decision];
}

type DiffPart = { text: string; changed: boolean };

function pushDiffPart(parts: DiffPart[], text: string, changed: boolean) {
  if (!text) return;
  const previous = parts[parts.length - 1];
  if (previous?.changed === changed) {
    previous.text += text;
  } else {
    parts.push({ text, changed });
  }
}

function diffSides(before: string, after: string) {
  const tokenize = (value: string) =>
    value.split(/(\s+|[.,;:!?()[\]{}"'])/).filter(Boolean);
  const left = tokenize(before);
  const right = tokenize(after);
  const leftParts: DiffPart[] = [];
  const rightParts: DiffPart[] = [];

  if (left.length * right.length > 120_000) {
    let prefix = 0;
    while (
      prefix < left.length &&
      prefix < right.length &&
      left[prefix] === right[prefix]
    ) {
      prefix += 1;
    }
    let suffix = 0;
    while (
      suffix < left.length - prefix &&
      suffix < right.length - prefix &&
      left[left.length - 1 - suffix] === right[right.length - 1 - suffix]
    ) {
      suffix += 1;
    }
    pushDiffPart(leftParts, left.slice(0, prefix).join(""), false);
    pushDiffPart(rightParts, right.slice(0, prefix).join(""), false);
    pushDiffPart(
      leftParts,
      left.slice(prefix, left.length - suffix).join(""),
      true,
    );
    pushDiffPart(
      rightParts,
      right.slice(prefix, right.length - suffix).join(""),
      true,
    );
    if (suffix) {
      pushDiffPart(leftParts, left.slice(left.length - suffix).join(""), false);
      pushDiffPart(rightParts, right.slice(right.length - suffix).join(""), false);
    }
    return { leftParts, rightParts };
  }

  const width = right.length + 1;
  const table = new Uint32Array((left.length + 1) * width);
  for (let i = left.length - 1; i >= 0; i -= 1) {
    for (let j = right.length - 1; j >= 0; j -= 1) {
      table[i * width + j] =
        left[i] === right[j]
          ? table[(i + 1) * width + j + 1] + 1
          : Math.max(
              table[(i + 1) * width + j],
              table[i * width + j + 1],
            );
    }
  }
  let i = 0;
  let j = 0;
  while (i < left.length || j < right.length) {
    if (i < left.length && j < right.length && left[i] === right[j]) {
      pushDiffPart(leftParts, left[i], false);
      pushDiffPart(rightParts, right[j], false);
      i += 1;
      j += 1;
    } else if (
      j < right.length &&
      (i === left.length ||
        table[i * width + j + 1] >= table[(i + 1) * width + j])
    ) {
      pushDiffPart(rightParts, right[j], true);
      j += 1;
    } else {
      pushDiffPart(leftParts, left[i], true);
      i += 1;
    }
  }
  return { leftParts, rightParts };
}

function DiffText({
  parts,
  side,
}: {
  parts: DiffPart[];
  side: "left" | "right";
}) {
  return (
    <div className="min-h-28 whitespace-pre-wrap rounded-md border bg-muted/20 p-3 font-mono text-xs leading-6">
      {parts.length ? (
        parts.map((part, index) => (
          <span
            key={`${index}-${part.changed}`}
            className={
              part.changed
                ? side === "left"
                  ? "bg-red-100 text-red-900 line-through dark:bg-red-950/60 dark:text-red-200"
                  : "bg-emerald-100 text-emerald-900 dark:bg-emerald-950/60 dark:text-emerald-200"
                : ""
            }
          >
            {part.text}
          </span>
        ))
      ) : (
        <span className="text-muted-foreground">（空）</span>
      )}
    </div>
  );
}

function RiskList({ risks }: { risks: SeoReviewRisk[] }) {
  if (!risks.length) return null;
  return (
    <div className="grid gap-2">
      {risks.map((risk, index) => (
        <div
          key={`${risk.kind}-${risk.label}-${index}`}
          className="rounded-md border border-red-300 bg-red-50 p-3 text-sm dark:border-red-900 dark:bg-red-950/30"
        >
          <div className="font-medium text-red-800 dark:text-red-200">
            {risk.label}
          </div>
          <div className="mt-1 grid gap-1 text-xs">
            <div>原值：{risk.before || "无"}</div>
            <div>新值：{risk.after || "无"}</div>
            <div className="text-muted-foreground">{risk.message}</div>
          </div>
        </div>
      ))}
    </div>
  );
}

function ReviewChangeCard({
  review,
  change,
  disabled,
  onUpdate,
  onDirtyChange,
}: {
  review: SeoReviewRun;
  change: SeoReviewChange;
  disabled: boolean;
  onUpdate: (
    reviewId: string,
    changeId: string,
    reviewedText: string,
    decision: SeoReviewChangeDecision,
    confirmRisks: boolean,
    revision?: number,
  ) => Promise<TaskRecord>;
  onDirtyChange: (changeId: string, dirty: boolean) => void;
}) {
  const [reviewedText, setReviewedText] = useState(change.reviewed_text);
  const [saving, setSaving] = useState(false);
  const [localError, setLocalError] = useState("");
  const [riskDialogOpen, setRiskDialogOpen] = useState(false);
  const [riskConfirmed, setRiskConfirmed] = useState(false);
  const [pendingRiskChange, setPendingRiskChange] =
    useState<SeoReviewChange | null>(null);

  useEffect(() => {
    setReviewedText(change.reviewed_text);
    onDirtyChange(change.id, false);
  }, [change.id, change.reviewed_text, change.updated_at, onDirtyChange]);

  const diff = useMemo(
    () =>
      diffSides(
        change.operation === "insert_after" ? "" : change.target_text,
        change.operation === "delete" ? "" : reviewedText,
      ),
    [change.operation, change.target_text, reviewedText],
  );

  async function saveDecision(
    decision: SeoReviewChangeDecision,
    confirmRisks: boolean,
    revision?: number,
  ) {
    setSaving(true);
    setLocalError("");
    try {
      const saved = await onUpdate(
        review.id,
        change.id,
        reviewedText,
        decision,
        confirmRisks,
        revision,
      );
      onDirtyChange(change.id, false);
      return saved;
    } catch (error) {
      setLocalError(errorMessage(error));
      throw error;
    } finally {
      setSaving(false);
    }
  }

  async function prepareAccept() {
    try {
      const saved = await saveDecision("pending", false);
      const refreshed = saved.seo_reviews
        ?.find((item) => item.id === review.id)
        ?.changes.find((item) => item.id === change.id);
      if (!refreshed) return;
      if (refreshed.risks.length) {
        setPendingRiskChange(refreshed);
        setRiskConfirmed(false);
        setRiskDialogOpen(true);
        return;
      }
      await saveDecision("accepted", false, saved.revision);
    } catch {
      // The inline error already explains the backend validation failure.
    }
  }

  async function confirmHighRisk() {
    if (!riskConfirmed) return;
    try {
      await saveDecision("accepted", true);
      setRiskDialogOpen(false);
      setPendingRiskChange(null);
    } catch {
      // Keep the dialog open so the operator can inspect the error.
    }
  }

  const editable = !disabled && change.applicable;

  return (
    <>
      <div
        className={`grid gap-4 rounded-lg border p-4 ${
          change.risks.length ? "border-red-300 dark:border-red-900" : ""
        }`}
      >
        <div className="flex flex-wrap items-start gap-2">
          <div className="min-w-0 flex-1">
            <div className="flex flex-wrap items-center gap-2">
              <span className="font-medium">{change.title}</span>
              <Badge variant="outline">{changeOperationLabel(change)}</Badge>
              <Badge
                variant={
                  change.decision === "accepted"
                    ? "default"
                    : change.decision === "rejected"
                      ? "secondary"
                      : "outline"
                }
              >
                {decisionLabel(change.decision)}
              </Badge>
              {change.risks.length > 0 && (
                <Badge variant="destructive">
                  <ShieldAlert />
                  高风险
                </Badge>
              )}
            </div>
            {change.rationale && (
              <p className="mt-1 text-sm text-muted-foreground">
                {change.rationale}
              </p>
            )}
          </div>
        </div>

        {!change.applicable && (
          <Alert variant="destructive">
            <AlertTriangle />
            <AlertTitle>该修改块不可直接应用</AlertTitle>
            <AlertDescription>
              {change.validation_errors.join("；") ||
                "该修改块无法安全匹配源正文。完整复检报告仍然保留。"}
            </AlertDescription>
          </Alert>
        )}

        <div className="grid gap-3 lg:grid-cols-2">
          <div className="grid gap-2">
            <Label>原文</Label>
            <DiffText parts={diff.leftParts} side="left" />
          </div>
          <div className="grid gap-2">
            <Label>建议内容</Label>
            <DiffText parts={diff.rightParts} side="right" />
          </div>
        </div>

        <div className="grid gap-2">
          <Label htmlFor={`review-edit-${review.id}-${change.id}`}>
            人工审核后的建议内容
          </Label>
          <Textarea
            id={`review-edit-${review.id}-${change.id}`}
            value={reviewedText}
            readOnly={change.operation === "delete"}
            disabled={!editable || saving}
            className="min-h-32 resize-y font-mono text-sm leading-6"
            onChange={(event) => {
              const next = event.target.value;
              setReviewedText(next);
              onDirtyChange(change.id, next !== change.reviewed_text);
            }}
          />
          {reviewedText !== change.model_proposed_text && (
            <p className="text-xs text-muted-foreground">
              当前内容经过人工调整；模型原始建议仍保留在审核记录中。
            </p>
          )}
        </div>

        <RiskList risks={change.risks} />

        {localError && (
          <Alert variant="destructive">
            <AlertTitle>修改块保存失败</AlertTitle>
            <AlertDescription>{localError}</AlertDescription>
          </Alert>
        )}

        <div className="flex flex-wrap justify-end gap-2">
          <Button
            type="button"
            variant="outline"
            disabled={!editable || saving}
            onClick={() => void saveDecision("pending", false)}
          >
            暂不处理
          </Button>
          <Button
              type="button"
              variant="secondary"
              disabled={disabled || saving}
              onClick={() => void saveDecision("rejected", false)}
          >
            <X />
            拒绝
          </Button>
          <Button
            type="button"
            disabled={!editable || saving}
            onClick={() => void prepareAccept()}
          >
            {saving ? <Loader2 className="animate-spin" /> : <Check />}
            接受
          </Button>
        </div>
      </div>

      <Dialog
        open={riskDialogOpen}
        onOpenChange={(open) => {
          setRiskDialogOpen(open);
          if (!open) setRiskConfirmed(false);
        }}
      >
        <DialogContent className="max-h-[90vh] overflow-y-auto sm:max-w-2xl">
          <DialogHeader>
            <DialogTitle>确认高风险事实修改</DialogTitle>
            <DialogDescription>
              该修改涉及锁定事实。请逐项核实来源后再决定是否接受。
            </DialogDescription>
          </DialogHeader>
          <RiskList risks={pendingRiskChange?.risks || []} />
          <label className="flex items-start gap-2 rounded-md border p-3 text-sm">
            <input
              type="checkbox"
              className="mt-0.5"
              checked={riskConfirmed}
              onChange={(event) => setRiskConfirmed(event.target.checked)}
            />
            <span>我已根据客户官网或运营资料核实以上事实修改。</span>
          </label>
          <DialogFooter>
            <Button
              type="button"
              variant="outline"
              onClick={() => setRiskDialogOpen(false)}
            >
              取消
            </Button>
            <Button
              type="button"
              variant="destructive"
              disabled={!riskConfirmed || saving}
              onClick={() => void confirmHighRisk()}
            >
              仍然接受该修改
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  );
}

function ReviewHistoryItem({
  review,
  task,
  disabled,
  onUpdate,
  onPreview,
  onApply,
  onComplete,
}: {
  review: SeoReviewRun;
  task: TaskRecord;
  disabled: boolean;
  onUpdate: (
    reviewId: string,
    changeId: string,
    reviewedText: string,
    decision: SeoReviewChangeDecision,
    confirmRisks: boolean,
    revision?: number,
  ) => Promise<TaskRecord>;
  onPreview: (reviewId: string) => Promise<SeoReviewPreview>;
  onApply: (
    reviewId: string,
    previewHash: string,
    confirmPending: boolean,
  ) => Promise<TaskRecord>;
  onComplete: (
    reviewId: string,
    confirmPending: boolean,
  ) => Promise<TaskRecord>;
}) {
  const [preview, setPreview] = useState<SeoReviewPreview | null>(null);
  const [previewOpen, setPreviewOpen] = useState(false);
  const [confirmPending, setConfirmPending] = useState(false);
  const [previewBusy, setPreviewBusy] = useState(false);
  const [localError, setLocalError] = useState("");
  const [dirtyChangeIds, setDirtyChangeIds] = useState<Set<string>>(new Set());
  const updateDirtyChange = useCallback((changeId: string, dirty: boolean) => {
    setDirtyChangeIds((current) => {
      const next = new Set(current);
      if (dirty) next.add(changeId);
      else next.delete(changeId);
      return next;
    });
  }, []);

  const sourceCurrent =
    review.source_article === (task.initial_article || "").trim();
  const editable = review.status === "open" && sourceCurrent && !disabled;
  const acceptedCount = review.changes.filter(
    (change) => change.decision === "accepted",
  ).length;
  const pendingCount = review.changes.filter(
    (change) => change.decision === "pending",
  ).length;
  const invalidCount = review.changes.filter(
    (change) => !change.applicable,
  ).length;

  async function openPreview() {
    setPreviewBusy(true);
    setLocalError("");
    try {
      const result = await onPreview(review.id);
      setPreview(result);
      setConfirmPending(result.pending_count === 0);
      setPreviewOpen(true);
    } catch (error) {
      setLocalError(errorMessage(error));
    } finally {
      setPreviewBusy(false);
    }
  }

  async function applyPreview() {
    if (!preview) return;
    setPreviewBusy(true);
    setLocalError("");
    try {
      await onApply(review.id, preview.article_hash, confirmPending);
      setPreviewOpen(false);
    } catch (error) {
      setLocalError(errorMessage(error));
    } finally {
      setPreviewBusy(false);
    }
  }

  async function completeWithoutChanges() {
    if (
      pendingCount &&
      !window.confirm(
        `仍有 ${pendingCount} 个修改块未处理。完成后它们会以“未处理”状态锁定，是否继续？`,
      )
    ) {
      return;
    }
    setPreviewBusy(true);
    setLocalError("");
    try {
      await onComplete(review.id, pendingCount > 0);
    } catch (error) {
      setLocalError(errorMessage(error));
    } finally {
      setPreviewBusy(false);
    }
  }

  return (
    <>
      <details className="group rounded-lg border bg-background">
        <summary className="flex cursor-pointer list-none items-center gap-3 p-4">
          <Badge variant={scoreVariant(review.score)}>
            {review.score.toFixed(1)} 分
          </Badge>
          <div className="min-w-0 flex-1">
            <div className="truncate font-medium">
              {review.prompt_snapshot.name}
            </div>
            <div className="text-xs text-muted-foreground">
              {review.created_at
                ? formatProjectDate(review.created_at, {
                    dateStyle: "medium",
                    timeStyle: "short",
                  })
                : "时间未知"}
              {" · "}
              源正文 {review.source_article_hash.slice(0, 10)}
            </div>
          </div>
          <Badge
            variant={
              review.status === "applied"
                ? "default"
                : review.status === "completed"
                  ? "secondary"
                  : "outline"
            }
          >
            {review.status === "applied"
              ? "已应用"
              : review.status === "completed"
                ? "已完成"
                : sourceCurrent
                  ? "审核中"
                  : "源正文已变更"}
          </Badge>
          <ChevronDown className="size-4 transition-transform group-open:rotate-180" />
        </summary>

        <div className="grid gap-5 border-t p-4">
          <Alert>
            <ClipboardCheck />
            <AlertTitle>
              {review.publish_ready ? "建议发布" : "建议修改后再发布"}
            </AlertTitle>
            <AlertDescription>
              {review.publish_recommendation}
            </AlertDescription>
          </Alert>

          <div className="overflow-hidden rounded-lg border">
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>维度</TableHead>
                  <TableHead className="w-20">得分</TableHead>
                  <TableHead className="w-20">目标</TableHead>
                  <TableHead>主要问题</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {review.dimensions.map((dimension) => (
                  <TableRow key={dimension.key}>
                    <TableCell className="font-medium">
                      {dimension.name}
                    </TableCell>
                    <TableCell>{dimension.score.toFixed(1)}</TableCell>
                    <TableCell>{dimension.target_score.toFixed(1)}</TableCell>
                    <TableCell className="whitespace-normal">
                      {dimension.main_issue}
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </div>

          <div className="grid gap-2">
            <Label>完整复检报告</Label>
            <pre className="max-h-[560px] overflow-auto whitespace-pre-wrap rounded-lg border bg-muted/40 p-4 text-sm leading-6">
              {review.report}
            </pre>
          </div>

          <div className="grid gap-3">
            <div className="flex flex-wrap items-center gap-2 font-medium">
              <span>建议修改</span>
              <Badge variant="outline">{review.changes.length} 个</Badge>
              <Badge variant="outline">{acceptedCount} 个已接受</Badge>
              {invalidCount > 0 && (
                <Badge variant="destructive">{invalidCount} 个不可应用</Badge>
              )}
            </div>
            {review.changes.length ? (
              review.changes.map((change) => (
                <ReviewChangeCard
                  key={change.id}
                  review={review}
                  change={change}
                  disabled={!editable}
                  onUpdate={onUpdate}
                  onDirtyChange={updateDirtyChange}
                />
              ))
            ) : (
              <Alert>
                <Check />
                <AlertTitle>本次没有必须修改的内容</AlertTitle>
                <AlertDescription>
                  评分和完整报告已保留，可以完成审核或针对新正文再次复检。
                </AlertDescription>
              </Alert>
            )}
          </div>

          {localError && (
            <Alert variant="destructive">
              <AlertTitle>审核操作失败</AlertTitle>
              <AlertDescription>{localError}</AlertDescription>
            </Alert>
          )}

          {review.status === "open" && !sourceCurrent && (
            <Alert variant="destructive">
              <AlertTriangle />
              <AlertTitle>源正文已经变化</AlertTitle>
              <AlertDescription>
                这份 Diff 只能作为历史记录查看，不能应用到当前正文。请针对当前正文重新复检。
              </AlertDescription>
            </Alert>
          )}

          {dirtyChangeIds.size > 0 && (
            <Alert variant="destructive">
              <AlertTriangle />
              <AlertTitle>存在尚未保存的人工调整</AlertTitle>
              <AlertDescription>
                请在对应修改块选择“暂不处理”“拒绝”或“接受”保存决定后，再预览或完成审核。
              </AlertDescription>
            </Alert>
          )}

          {editable && (
            <div className="flex flex-wrap justify-end gap-2">
              <Button
                type="button"
                variant="outline"
                disabled={
                  previewBusy || acceptedCount > 0 || dirtyChangeIds.size > 0
                }
                onClick={() => void completeWithoutChanges()}
              >
                完成审核，不修改正文
              </Button>
              <Button
                type="button"
                disabled={
                  !acceptedCount || previewBusy || dirtyChangeIds.size > 0
                }
                onClick={() => void openPreview()}
              >
                {previewBusy ? <Loader2 className="animate-spin" /> : <Eye />}
                预览已接受修改
              </Button>
            </div>
          )}
        </div>
      </details>

      <Dialog open={previewOpen} onOpenChange={setPreviewOpen}>
        <DialogContent className="h-[min(900px,calc(100vh-2rem))] w-[calc(100vw-2rem)] max-w-[1200px] grid-rows-[auto_minmax(0,1fr)_auto] overflow-hidden p-0">
          <DialogHeader className="border-b px-5 py-4 pr-12">
            <DialogTitle>应用前完整正文预览</DialogTitle>
            <DialogDescription>
              已合并 {preview?.accepted_change_ids.length || 0} 个修改块，并通过
              H1、H2/H3、FAQ 和过渡段结构校验。
            </DialogDescription>
          </DialogHeader>
          <div className="min-h-0 overflow-y-auto px-5 py-4">
            <Textarea
              readOnly
              value={preview?.article || ""}
              className="h-full min-h-[520px] resize-none font-mono text-sm leading-6"
            />
            {preview && preview.pending_count > 0 && (
              <label className="mt-4 flex items-start gap-2 rounded-md border border-amber-300 bg-amber-50 p-3 text-sm dark:border-amber-900 dark:bg-amber-950/30">
                <input
                  type="checkbox"
                  className="mt-0.5"
                  checked={confirmPending}
                  onChange={(event) => setConfirmPending(event.target.checked)}
                />
                <span>
                  我确认剩余 {preview.pending_count} 个修改块将以“未处理”状态锁定。
                </span>
              </label>
            )}
          </div>
          <DialogFooter className="border-t px-5 py-4">
            <Button
              type="button"
              variant="outline"
              onClick={() => setPreviewOpen(false)}
            >
              返回继续审核
            </Button>
            <Button
              type="button"
              disabled={
                previewBusy ||
                !preview ||
                (preview.pending_count > 0 && !confirmPending)
              }
              onClick={() => void applyPreview()}
            >
              {previewBusy ? <Loader2 className="animate-spin" /> : <Save />}
              确认并生成新正文版本
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  );
}

export function ArticleSeoReviewPanel({
  task,
  articleDirty,
  busy,
  hasActiveJob,
  onSaveSettings,
  onStartReview,
  onUpdateReviewChange,
  onPreviewReview,
  onApplyReview,
  onCompleteReview,
}: {
  task: TaskRecord;
  articleDirty: boolean;
  busy: boolean;
  hasActiveJob: boolean;
  onSaveSettings: (settings: SeoReviewSettings) => void;
  onStartReview: (settings: SeoReviewSettings) => void;
  onUpdateReviewChange: (
    reviewId: string,
    changeId: string,
    reviewedText: string,
    decision: SeoReviewChangeDecision,
    confirmRisks: boolean,
    revision?: number,
  ) => Promise<TaskRecord>;
  onPreviewReview: (reviewId: string) => Promise<SeoReviewPreview>;
  onApplyReview: (
    reviewId: string,
    previewHash: string,
    confirmPending: boolean,
  ) => Promise<TaskRecord>;
  onCompleteReview: (
    reviewId: string,
    confirmPending: boolean,
  ) => Promise<TaskRecord>;
}) {
  const [primaryKeyword, setPrimaryKeyword] = useState("");
  const [longTailText, setLongTailText] = useState("");
  const [promptSelection, setPromptSelection] = useState("project_default");
  const [library, setLibrary] = useState<ProjectPromptLibrary | null>(null);
  const [libraryError, setLibraryError] = useState("");

  useEffect(() => {
    setPrimaryKeyword(task.seo_primary_keyword || "");
    setLongTailText((task.seo_long_tail_keywords || []).join("\n"));
    setPromptSelection(task.seo_review_prompt_selection || "project_default");
  }, [
    task.id,
    task.seo_primary_keyword,
    task.seo_review_prompt_selection,
    task.seo_long_tail_keywords,
  ]);

  const loadLibrary = useCallback(async () => {
    try {
      setLibraryError("");
      setLibrary(
        await apiGet<ProjectPromptLibrary>(
          `/api/projects/${encodeURIComponent(task.customer)}/prompts`,
        ),
      );
    } catch (error) {
      setLibraryError(errorMessage(error));
    }
  }, [task.customer]);

  useEffect(() => {
    void loadLibrary();
  }, [loadLibrary]);

  const longTailKeywords = useMemo(
    () => parseKeywords(longTailText),
    [longTailText],
  );
  const settings = useMemo<SeoReviewSettings>(
    () => ({
      primaryKeyword: primaryKeyword.replace(/\s+/g, " ").trim(),
      longTailKeywords,
      promptSelection,
    }),
    [longTailKeywords, primaryKeyword, promptSelection],
  );
  const settingsDirty =
    settings.primaryKeyword !== (task.seo_primary_keyword || "") ||
    settings.promptSelection !==
      (task.seo_review_prompt_selection || "project_default") ||
    settings.longTailKeywords.join("\n") !==
      (task.seo_long_tail_keywords || []).join("\n");
  const reviewPrompts = (library?.prompts || []).filter(
    (prompt) => prompt.kind === "review" && prompt.active,
  );
  const projectDefaultReviewId =
    library?.defaults.default_review_prompt_id || "";
  const projectDefaultReviewPrompt = reviewPrompts.find(
    (prompt) => prompt.id === projectDefaultReviewId,
  );
  const displayedPromptSelection =
    !projectDefaultReviewId && promptSelection === "system"
      ? "project_default"
      : promptSelection;
  const history = [...(task.seo_reviews || [])].reverse();
  const latest = history[0];
  const hasArticle = Boolean(task.initial_article?.trim());

  return (
    <details className="group rounded-lg border bg-muted/10">
      <summary className="flex cursor-pointer list-none items-center gap-3 p-4">
        <Sparkles className="size-5 text-primary" />
        <div className="min-w-0 flex-1">
          <div className="font-medium">SEO 质量复检</div>
          <div className="text-xs text-muted-foreground">
            报告与修改建议分开保存；修改按 Diff 逐块审核，不影响 ZeroGPT 流程。
          </div>
        </div>
        {latest && (
          <Badge variant={scoreVariant(latest.score)}>
            最新 {latest.score.toFixed(1)} 分
          </Badge>
        )}
        <Badge variant="outline">{history.length} 次</Badge>
        <ChevronDown className="size-4 transition-transform group-open:rotate-180" />
      </summary>

      <div className="grid gap-5 border-t p-4">
        {!hasArticle && (
          <Alert>
            <Sparkles />
            <AlertTitle>请先生成并保存第一版正文</AlertTitle>
            <AlertDescription>
              复检只绑定已保存的正文版本，不会审核编辑器中的未保存内容。
            </AlertDescription>
          </Alert>
        )}
        {articleDirty && (
          <Alert variant="destructive">
            <AlertTriangle />
            <AlertTitle>正文有未保存修改</AlertTitle>
            <AlertDescription>
              请先保存第一版，再执行 SEO 质量复检，避免报告绑定到旧版本。
            </AlertDescription>
          </Alert>
        )}
        {libraryError && (
          <Alert variant="destructive">
            <AlertTitle>提示词库加载失败</AlertTitle>
            <AlertDescription>{libraryError}</AlertDescription>
          </Alert>
        )}

        <div className="grid gap-4 rounded-lg border bg-background p-4 lg:grid-cols-2">
          <div className="grid gap-2">
            <Label htmlFor="seo-review-primary-keyword">
              本文主关键词（可选）
            </Label>
            <Input
              id="seo-review-primary-keyword"
              value={primaryKeyword}
              maxLength={240}
              disabled={busy}
              placeholder="例如：roof ladders"
              onChange={(event) => setPrimaryKeyword(event.target.value)}
            />
          </div>
          <div className="grid gap-2">
            <Label htmlFor="seo-review-prompt">本次复检提示词</Label>
            <select
              id="seo-review-prompt"
              className="h-9 rounded-md border bg-background px-3 text-sm"
              value={displayedPromptSelection}
              disabled={busy}
              onChange={(event) => setPromptSelection(event.target.value)}
            >
              <option value="project_default">
                {projectDefaultReviewPrompt
                  ? `项目默认：${projectDefaultReviewPrompt.name}`
                  : "系统默认 SEO 质量复检"}
              </option>
              {projectDefaultReviewId && (
                <option value="system">系统默认 SEO 质量复检</option>
              )}
              {reviewPrompts.map((prompt) => (
                <option key={prompt.id} value={prompt.id}>
                  {prompt.name}（v{prompt.version}）
                </option>
              ))}
            </select>
          </div>
          <div className="grid gap-2 lg:col-span-2">
            <Label htmlFor="seo-review-long-tail">
              本文长尾关键词（可选）
            </Label>
            <Textarea
              id="seo-review-long-tail"
              value={longTailText}
              disabled={busy}
              maxLength={4000}
              className="min-h-24 resize-y"
              placeholder="每行一个，也可用逗号分隔；留空时不会把话题标题当成长尾关键词。"
              onChange={(event) => setLongTailText(event.target.value)}
            />
            <p className="text-xs text-muted-foreground">
              已识别 {longTailKeywords.length} 个长尾关键词。
            </p>
          </div>
          <div className="flex flex-wrap justify-end gap-2 lg:col-span-2">
            <Button
              type="button"
              variant="outline"
              disabled={busy || !settingsDirty}
              onClick={() => onSaveSettings(settings)}
            >
              <Save />
              保存复检设置
            </Button>
            <Button
              type="button"
              disabled={busy || hasActiveJob || articleDirty || !hasArticle}
              onClick={() => onStartReview(settings)}
            >
              {busy ? <Loader2 className="animate-spin" /> : <Sparkles />}
              开始 SEO 质量复检
            </Button>
          </div>
        </div>

        {history.length > 0 ? (
          <div className="grid gap-3">
            <div className="flex items-center gap-2 font-medium">
              <History className="size-4" />
              历次复检
            </div>
            {history.map((review) => (
              <ReviewHistoryItem
                key={review.id}
                review={review}
                task={task}
                disabled={busy || hasActiveJob}
                onUpdate={onUpdateReviewChange}
                onPreview={onPreviewReview}
                onApply={onApplyReview}
                onComplete={onCompleteReview}
              />
            ))}
          </div>
        ) : (
          <p className="text-sm text-muted-foreground">还没有复检记录。</p>
        )}
      </div>
    </details>
  );
}
