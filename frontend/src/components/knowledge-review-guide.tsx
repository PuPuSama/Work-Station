import { AlertCircle, CheckCircle2, Info, ShieldCheck } from "lucide-react";

import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";

export function KnowledgeReviewGuide() {
  return (
    <Alert className="border-sky-200 bg-sky-50/70 dark:border-sky-900 dark:bg-sky-950/20">
      <Info className="text-sky-700 dark:text-sky-300" />
      <AlertTitle>运营审核标准</AlertTitle>
      <AlertDescription className="grid gap-3 text-sm leading-6">
        <p>审核时只看三件事：来源可信、内容能用、产品和图片对应。</p>
        <div className="grid gap-2 md:grid-cols-3">
          <div className="rounded-lg border bg-background/70 p-3">
            <div className="flex items-center gap-2 font-medium"><ShieldCheck className="size-4 text-emerald-600" />1. 来源对吗？</div>
            <p className="mt-1 text-xs text-muted-foreground">是客户官网、客户上传的资料，或可以追溯的公开来源。</p>
          </div>
          <div className="rounded-lg border bg-background/70 p-3">
            <div className="flex items-center gap-2 font-medium"><CheckCircle2 className="size-4 text-emerald-600" />2. 内容能用吗？</div>
            <p className="mt-1 text-xs text-muted-foreground">能支撑文章事实；明显广告、乱码、重复或无关内容不要通过。</p>
          </div>
          <div className="rounded-lg border bg-background/70 p-3">
            <div className="flex items-center gap-2 font-medium"><AlertCircle className="size-4 text-amber-600" />3. 产品对应吗？</div>
            <p className="mt-1 text-xs text-muted-foreground">产品名称、规格和图片属于同一产品；不确定就交给负责人复核。</p>
          </div>
        </div>
        <details className="rounded-lg border bg-background/70 px-3 py-2 text-xs">
          <summary className="cursor-pointer font-medium">信任层级怎么选</summary>
          <div className="mt-2 grid gap-1 text-muted-foreground">
            <p><strong className="text-foreground">硬事实：</strong>型号、尺寸、材质、认证等可直接核对的信息。</p>
            <p><strong className="text-foreground">参考资料：</strong>博客、指南和背景说明，可参考但不要当成硬事实。</p>
            <p><strong className="text-foreground">写作指令：</strong>语气、格式和禁用表达，不是产品事实。</p>
          </div>
        </details>
      </AlertDescription>
    </Alert>
  );
}
