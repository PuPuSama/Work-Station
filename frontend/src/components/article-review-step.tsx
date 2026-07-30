"use client";

import {
  Clipboard,
  Link2,
  Save,
  ShieldCheck,
  WandSparkles,
} from "lucide-react";

import {
  AiScreenshotInput,
  WorkbenchField,
  WorkflowStep,
} from "@/components/article-workbench-ui";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import { ScrollArea } from "@/components/ui/scroll-area";
import { Textarea } from "@/components/ui/textarea";
import type { PublicConfig, TaskRecord } from "@/types";

export function ArticleReviewStep({
  task,
  config,
  initialArticle,
  initialAiScore,
  initialAiReport,
  finalAiScore,
  finalAiReport,
  humanizedText,
  humanizedWords,
  humanizedDirty,
  humanizedEditRollsBack,
  busy,
  hasActiveJob,
  canAction,
  onInitialAiScoreChange,
  onInitialAiReportChange,
  onFinalAiScoreChange,
  onFinalAiReportChange,
  onHumanizedTextChange,
  onCopy,
  onUploadScreenshot,
  onConfirmInitial,
  onHumanize,
  onSaveHumanized,
  onConfirmFinal,
  onRestoreLinks,
}: {
  task: TaskRecord;
  config: PublicConfig | null;
  initialArticle: string;
  initialAiScore: string;
  initialAiReport: string;
  finalAiScore: string;
  finalAiReport: string;
  humanizedText: string;
  humanizedWords: number;
  humanizedDirty: boolean;
  humanizedEditRollsBack: boolean;
  busy: boolean;
  hasActiveJob: boolean;
  canAction: (action: string) => boolean;
  onInitialAiScoreChange: (value: string) => void;
  onInitialAiReportChange: (value: string) => void;
  onFinalAiScoreChange: (value: string) => void;
  onFinalAiReportChange: (value: string) => void;
  onHumanizedTextChange: (value: string) => void;
  onCopy: (content: string, label: string) => void;
  onUploadScreenshot: (stage: "initial" | "final", file: File) => void;
  onConfirmInitial: () => void;
  onHumanize: () => void;
  onSaveHumanized: () => void;
  onConfirmFinal: () => void;
  onRestoreLinks: () => void;
}) {
  return (
    <ScrollArea className="h-[570px] pr-3">
      <div className="grid gap-4">
        <WorkflowStep
          number="1"
          title="ZeroGPT 初检（人工）"
          description="复制第一版正文到 ZeroGPT，检测后把分数或备注保存回来。系统不会自动访问 ZeroGPT。"
          done={Boolean(task.initial_ai_check?.confirmed)}
        >
          <div className="flex flex-wrap gap-2">
            <Button variant="outline" onClick={() => onCopy(initialArticle, "第一版正文已复制")}>
              <Clipboard />
              复制第一版正文
            </Button>
          </div>
          {task.initial_article_issues?.length ? (
            <Alert className="border-amber-500/40 bg-amber-50">
              <AlertTitle>第一版尚未满足检测条件</AlertTitle>
              <AlertDescription>{task.initial_article_issues.join(" ")}</AlertDescription>
            </Alert>
          ) : null}
          <div className="grid gap-3 md:grid-cols-[150px_1fr]">
            <WorkbenchField
              label="AI 率（可选）"
              value={initialAiScore}
              onChange={onInitialAiScoreChange}
              inputType="number"
            />
            <div className="grid gap-2">
              <Label>初检报告或备注</Label>
              <Textarea
                value={initialAiReport}
                onChange={(event) => onInitialAiReportChange(event.target.value)}
                placeholder="粘贴 ZeroGPT 结果，或记录已在公司工作台完成初检"
                className="h-[92px] resize-none"
              />
            </div>
          </div>
          <AiScreenshotInput
            label="初检 AI 率截图"
            path={task.initial_ai_check?.screenshot_path || ""}
            disabled={busy}
            onImage={(file) => onUploadScreenshot("initial", file)}
          />
          <Button
            onClick={onConfirmInitial}
            disabled={
              busy ||
              !canAction("confirm_initial_ai_check") ||
              task.initial_article_ready === false ||
              (!initialAiScore &&
                !initialAiReport.trim() &&
                !task.initial_ai_check?.screenshot_path)
            }
          >
            <ShieldCheck />
            确认初检完成
          </Button>
        </WorkflowStep>

        <WorkflowStep
          number="2"
          title="生成或粘贴降 AI 稿"
          description={`可在完成初检后调用本地提示词，也可以直接粘贴外部已经降 AI 的正文并保存。提示词：${config?.prompts?.humanize ?? "D:\\article\\降ai提示词-未测试效果版.txt"}`}
          done={Boolean(task.humanized_article)}
        >
          {task.humanization_skipped && (
            <Alert className="border-emerald-600/30 bg-emerald-50">
              <ShieldCheck />
              <AlertTitle>首次正文已经达标</AlertTitle>
              <AlertDescription>
                初检 AI 率低于项目阈值，系统已沿用第一版正文，并自动跳过降 AI 改写和第二次检测。
              </AlertDescription>
            </Alert>
          )}
          <div className="text-xs text-muted-foreground">
            当前候选稿：{humanizedWords} 词；不设最大词数，不会自动压缩。
          </div>
          {humanizedEditRollsBack && (
            <Alert className="border-amber-500/40 bg-amber-50">
              <AlertTitle>保存修改会回退后续步骤</AlertTitle>
              <AlertDescription>
                将回退到“待 ZeroGPT 复检”，并清除旧正文对应的复检确认、链接恢复、图片准备、Word、TDK 和交付包记录。
              </AlertDescription>
            </Alert>
          )}
          <Textarea
            value={humanizedText}
            onChange={(event) => onHumanizedTextChange(event.target.value)}
            placeholder="可直接粘贴外部已经降 AI 的完整正文，或完成初检后点击下方模型改写"
            className="h-[220px] resize-none overflow-y-auto font-mono text-sm leading-6"
          />
          <div className="grid gap-2 md:grid-cols-2">
            <Button
              onClick={onHumanize}
              disabled={busy || hasActiveJob || !canAction("humanize_article")}
            >
              <WandSparkles />
              执行内置 AI 改写
            </Button>
            <Button
              variant="outline"
              onClick={onSaveHumanized}
              disabled={
                busy ||
                !humanizedText.trim() ||
                !humanizedDirty ||
                !canAction("update_humanized_article")
              }
            >
              <Save />
              {humanizedEditRollsBack
                ? "保存修改并回退后续步骤"
                : "保存粘贴的降 AI 稿"}
            </Button>
          </div>
        </WorkflowStep>

        <WorkflowStep
          number="3"
          title="ZeroGPT 复检（人工）"
          description={`复制降 AI 版本再次检测。默认参考线 < ${config?.article.ai_pass_threshold ?? 30}%，但最终由你确认。`}
          done={Boolean(task.final_ai_check?.confirmed)}
        >
          <Button
            variant="outline"
            onClick={() => onCopy(task.humanized_article || humanizedText, "降 AI 正文已复制")}
            disabled={!humanizedText.trim()}
          >
            <Clipboard />
            复制降 AI 正文
          </Button>
          <div className="grid gap-3 md:grid-cols-[150px_1fr]">
            <WorkbenchField
              label="复检 AI 率（可选）"
              value={finalAiScore}
              onChange={onFinalAiScoreChange}
              inputType="number"
            />
            <div className="grid gap-2">
              <Label>复检报告或备注</Label>
              <Textarea
                value={finalAiReport}
                onChange={(event) => onFinalAiReportChange(event.target.value)}
                placeholder="粘贴第二次 ZeroGPT 结果"
                className="h-[92px] resize-none"
              />
            </div>
          </div>
          <AiScreenshotInput
            label="复检 AI 率截图"
            path={task.final_ai_check?.screenshot_path || ""}
            disabled={busy}
            onImage={(file) => onUploadScreenshot("final", file)}
          />
          <Button
            onClick={onConfirmFinal}
            disabled={
              busy ||
              !canAction("confirm_final_ai_check") ||
              !humanizedText.trim() ||
              (!finalAiScore &&
                !finalAiReport.trim() &&
                !task.final_ai_check?.screenshot_path)
            }
          >
            <ShieldCheck />
            确认复检完成
          </Button>
        </WorkflowStep>

        <WorkflowStep
          number="4"
          title="恢复并校验超链接"
          description="以第一版链接清单为基准；URL 缺失或锚文本被降 AI 改名时，由模型恢复第一版的原始链接名称和 URL。"
          done={
            task.status === "links_verified" ||
            task.status === "images_ready" ||
            task.status === "docx_exported"
          }
        >
          <div className="flex flex-wrap items-center gap-2 text-sm">
            <Badge variant="outline">第一版链接 {task.source_links?.length ?? 0}</Badge>
            {task.workflow_error && (
              <span className="text-destructive">{task.workflow_error.message}</span>
            )}
          </div>
          <Button
            onClick={onRestoreLinks}
            disabled={busy || hasActiveJob || !canAction("restore_links")}
          >
            <Link2 />
            校验并回填链接
          </Button>
        </WorkflowStep>
      </div>
    </ScrollArea>
  );
}
