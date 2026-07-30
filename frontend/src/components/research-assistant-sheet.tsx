"use client";

import {
  AlertCircle,
  ExternalLink,
  Loader2,
  MessageSquareText,
  Send,
  ShieldCheck,
  X,
} from "lucide-react";
import { useState } from "react";

import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Sheet,
  SheetClose,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
  SheetTrigger,
} from "@/components/ui/sheet";
import { Textarea } from "@/components/ui/textarea";
import { apiPost } from "@/lib/api";
import type { ResearchConversation } from "@/types";

type ResearchAssistantSheetProps = {
  customer: string;
  articleId?: string;
};

function errorMessage(error: unknown) {
  return error instanceof Error ? error.message : "研究助手暂时不可用";
}

export function ResearchAssistantSheet({
  customer,
  articleId,
}: ResearchAssistantSheetProps) {
  const [conversation, setConversation] =
    useState<ResearchConversation | null>(null);
  const [question, setQuestion] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  async function ask() {
    const value = question.trim();
    if (!value || busy) return;
    setBusy(true);
    setError("");
    try {
      const requestId =
        typeof crypto !== "undefined" && "randomUUID" in crypto
          ? crypto.randomUUID()
          : `${Date.now()}-${Math.random().toString(16).slice(2)}`;
      const next = await apiPost<ResearchConversation>(
        `/api/knowledge/${encodeURIComponent(customer)}/research-assistant/messages`,
        {
          request_id: requestId,
          question: value,
          conversation_id: conversation?.conversation_id,
          article_id: articleId,
          limit: 8,
        },
      );
      setConversation(next);
      setQuestion("");
    } catch (askError) {
      setError(errorMessage(askError));
    } finally {
      setBusy(false);
    }
  }

  return (
    <Sheet>
      <SheetTrigger
        render={
          <Button type="button" variant="outline" className="min-h-11" />
        }
      >
        <MessageSquareText />
        询问研究资料
      </SheetTrigger>
      <SheetContent
        className="w-full gap-0 sm:max-w-xl"
        showCloseButton={false}
      >
        <SheetHeader className="border-b pr-14">
          <SheetTitle>只读研究助手</SheetTitle>
          <SheetDescription>
            回答仅依据当前项目已发布、当前快照中的 Chunk；不会改文章、产品或知识库状态。
          </SheetDescription>
        </SheetHeader>
        <SheetClose
          render={
            <Button
              type="button"
              variant="ghost"
              size="icon"
              className="absolute right-2 top-2 size-11"
              aria-label="关闭研究助手"
            />
          }
        >
          <X />
        </SheetClose>

        <div className="flex min-h-0 flex-1 flex-col">
          <div className="min-h-0 flex-1 overflow-y-auto p-4">
            <Alert className="mb-4">
              <ShieldCheck />
              <AlertTitle>引用受服务端校验</AlertTitle>
              <AlertDescription>
                模型只能引用本次实际检索到的 Chunk ID；公开链接由服务端来源记录补回。
              </AlertDescription>
            </Alert>

            {error ? (
              <Alert variant="destructive" className="mb-4">
                <AlertCircle />
                <AlertTitle>回答失败</AlertTitle>
                <AlertDescription>{error}</AlertDescription>
              </Alert>
            ) : null}

            <div className="grid gap-4" aria-live="polite">
              {conversation?.messages.map((message) => (
                <article
                  key={message.message_id}
                  className={
                    message.role === "user"
                      ? "ml-8 rounded-xl bg-primary px-4 py-3 text-primary-foreground"
                      : "mr-4 rounded-xl border bg-card px-4 py-3"
                  }
                >
                  <p className="whitespace-pre-wrap text-sm leading-6">
                    {message.content}
                  </p>
                  {message.citations.length ? (
                    <div className="mt-3 grid gap-2 border-t pt-3">
                      {message.citations.map((citation) => (
                        <div
                          key={citation.chunk_id}
                          className="rounded-lg bg-muted/40 p-2 text-foreground"
                        >
                          <div className="flex items-center gap-2">
                            <Badge variant="outline">[{citation.ordinal}]</Badge>
                            <span className="min-w-0 flex-1 truncate text-xs font-medium">
                              {citation.display_name}
                            </span>
                            {citation.canonical_url ? (
                              <a
                                href={citation.canonical_url}
                                target="_blank"
                                rel="noreferrer"
                                className="inline-flex min-h-8 items-center gap-1 text-xs text-muted-foreground hover:text-foreground"
                              >
                                来源
                                <ExternalLink className="size-3" />
                              </a>
                            ) : (
                              <span className="text-xs text-muted-foreground">
                                私有资料
                              </span>
                            )}
                          </div>
                          <p className="mt-1 line-clamp-3 text-xs leading-5 text-muted-foreground">
                            {citation.text}
                          </p>
                        </div>
                      ))}
                    </div>
                  ) : null}
                </article>
              ))}
              {!conversation?.messages.length ? (
                <div className="rounded-xl border border-dashed p-6 text-center text-sm text-muted-foreground">
                  可以询问产品事实、资料差异或某一章节有哪些已发布证据。
                </div>
              ) : null}
            </div>
          </div>

          <div className="border-t bg-background p-4">
            <label htmlFor="research-question" className="text-sm font-medium">
              问题
            </label>
            <Textarea
              id="research-question"
              value={question}
              onChange={(event) => setQuestion(event.target.value)}
              placeholder="例如：已发布资料对这个产品的材质和适用场景有哪些明确说明？"
              className="mt-2 min-h-24 resize-y"
              maxLength={4000}
              onKeyDown={(event) => {
                if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) {
                  event.preventDefault();
                  void ask();
                }
              }}
            />
            <div className="mt-2 flex items-center justify-between gap-3">
              <span className="text-xs text-muted-foreground">
                Ctrl/⌘ + Enter 发送 · 记录保留 30 天
              </span>
              <Button
                type="button"
                className="min-h-11"
                disabled={!question.trim() || busy}
                onClick={() => void ask()}
              >
                {busy ? <Loader2 className="animate-spin" /> : <Send />}
                {busy ? "检索并回答…" : "发送"}
              </Button>
            </div>
          </div>
        </div>
      </SheetContent>
    </Sheet>
  );
}
