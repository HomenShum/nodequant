"use client";

import { AssistantRuntimeProvider } from "@assistant-ui/react";
import {
  AssistantChatTransport,
  useAISDKRuntime,
} from "@assistant-ui/react-ai-sdk";
import { useChat } from "@ai-sdk/react";
import { lastAssistantMessageIsCompleteWithToolCalls } from "ai";
import { Thread } from "@/components/thread";
import { newThreadAction } from "@/lib/new-thread";
import {
  SidebarInset,
  SidebarProvider,
  SidebarTrigger,
} from "@/components/ui/sidebar";
import { ThreadListSidebar } from "@/components/threadlist-sidebar";
import { NodeQuantPanels } from "@/components/nodequant-panels";
import { TrialScopePanels } from "@/components/trialscope-panels";
import { Separator } from "@/components/ui/separator";
import {
  Breadcrumb,
  BreadcrumbItem,
  BreadcrumbLink,
  BreadcrumbList,
  BreadcrumbPage,
  BreadcrumbSeparator,
} from "@/components/ui/breadcrumb";

export const Assistant = () => {
  const chat = useChat({
    sendAutomaticallyWhen: lastAssistantMessageIsCompleteWithToolCalls,
    transport: new AssistantChatTransport({
      api: process.env.NEXT_PUBLIC_API_URL ?? "/api/trialscope-chat",
    }),
  });
  const runtime = useAISDKRuntime(chat);

  // The sidebar button cannot reach `chat`, and every runtime-level path was
  // measured not to work (see lib/new-thread.ts). This component owns the
  // messages, so it registers how to clear them.
  newThreadAction.run = () => {
    (globalThis as any).__newThreadFired = ((globalThis as any).__newThreadFired ?? 0) + 1;
    // Clear BOTH: chat.messages is the AI SDK's copy, and the runtime keeps its
    // own repository which is what actually renders. Clearing one leaves the
    // other on screen.
    chat.setMessages([]);
    // The runtime keeps its own message repository, which is what renders;
    // clearing chat.messages alone leaves it on screen. `threads` is the
    // current API surface -- AssistantRuntime has no switchToNewThread.
    try { runtime.threads.switchToNewThread(); } catch { /* not thread-list capable */ }
  };

  const sendMessage = (text: string) =>
    runtime.thread.append({
      role: "user",
      content: [{ type: "text", text }],
    });

  return (
    <AssistantRuntimeProvider runtime={runtime}>
      {/* Registers the validated panel vocabulary the backend composes from.
          Both sets are mounted during the port: NodeQuant's panels are the
          destination, TrialScope's are still what the current chat route emits.
          The TrialScope set comes out with the domain cut, not before, so the
          app keeps running while the vocabulary is replaced one panel at a
          time. */}
      <NodeQuantPanels />
      <TrialScopePanels />
      <SidebarProvider>
        <div className="flex h-dvh w-full pr-0.5">
          <ThreadListSidebar />
          <SidebarInset>
            <header className="flex h-16 shrink-0 items-center gap-2 border-b px-4">
              <SidebarTrigger />
              <Separator orientation="vertical" className="mr-2 h-4" />
              <Breadcrumb>
                <BreadcrumbList>
                  <BreadcrumbItem className="hidden md:block">
                    <BreadcrumbLink
                      href="https://clinicaltrials.gov/data-api/api"
                      target="_blank"
                      rel="noopener noreferrer"
                    >
                      ClinicalTrials.gov API
                    </BreadcrumbLink>
                  </BreadcrumbItem>
                  <BreadcrumbSeparator className="hidden md:block" />
                  <BreadcrumbItem>
                    <BreadcrumbPage>TrialScope</BreadcrumbPage>
                  </BreadcrumbItem>
                </BreadcrumbList>
              </Breadcrumb>
            </header>
            <div className="flex-1 overflow-hidden">
              <Thread onSend={sendMessage} />
            </div>
          </SidebarInset>
        </div>
      </SidebarProvider>
    </AssistantRuntimeProvider>
  );
};
