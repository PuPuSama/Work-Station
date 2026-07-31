"use client";

import { BookOpenText, FileSearch } from "lucide-react";
import { useRouter, useSearchParams } from "next/navigation";

import { ServerKnowledgeInbox } from "@/components/server-knowledge-inbox";
import { ServerResearchWorkspace } from "@/components/server-research-workspace";
import {
  Tabs,
  TabsContent,
  TabsList,
  TabsTrigger,
} from "@/components/ui/tabs";

export function ServerKnowledgeWorkspace({ customer }: { customer: string }) {
  const router = useRouter();
  const searchParams = useSearchParams();
  const selected =
    searchParams.get("tab") === "research" ? "research" : "inbox";

  function selectTab(value: string) {
    const next = new URLSearchParams(searchParams.toString());
    next.set("tab", value);
    if (value !== "research") next.delete("thread");
    router.replace(`?${next.toString()}`, { scroll: false });
  }

  return (
    <Tabs value={selected} onValueChange={selectTab} className="gap-0">
      <div className="mx-auto w-full max-w-[1480px] px-5 pt-5">
        <TabsList className="h-auto min-h-11 max-w-md">
          <TabsTrigger value="inbox" className="min-h-10">
            <BookOpenText />
            来源 Inbox
          </TabsTrigger>
          <TabsTrigger value="research" className="min-h-10">
            <FileSearch />
            资料研究
          </TabsTrigger>
        </TabsList>
      </div>
      <TabsContent value="inbox">
        <ServerKnowledgeInbox customer={customer} />
      </TabsContent>
      <TabsContent value="research">
        <ServerResearchWorkspace customer={customer} />
      </TabsContent>
    </Tabs>
  );
}
