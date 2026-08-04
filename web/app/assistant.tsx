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
import type { MarketMapSummary } from "@/components/market-map-launcher";
import { ThemeToggle } from "@/components/theme-toggle";
import { Separator } from "@/components/ui/separator";
import {
  Breadcrumb,
  BreadcrumbItem,
  BreadcrumbLink,
  BreadcrumbList,
  BreadcrumbPage,
  BreadcrumbSeparator,
} from "@/components/ui/breadcrumb";
import { MapIcon } from "lucide-react";

export const Assistant = ({
  marketMapSummary,
}: {
  marketMapSummary: MarketMapSummary;
}) => {
  const chat = useChat({
    sendAutomaticallyWhen: lastAssistantMessageIsCompleteWithToolCalls,
    transport: new AssistantChatTransport({
      api: process.env.NEXT_PUBLIC_API_URL ?? "/api/nodequant-chat",
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
      {/* Exact closed NodeQuant authority vocabulary. */}
      <NodeQuantPanels />
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
                    <BreadcrumbLink href="/">NodeQuant</BreadcrumbLink>
                  </BreadcrumbItem>
                  <BreadcrumbSeparator className="hidden md:block" />
                  <BreadcrumbItem>
                    <BreadcrumbPage>Proof Kernel v0</BreadcrumbPage>
                  </BreadcrumbItem>
                </BreadcrumbList>
              </Breadcrumb>
              <div className="ml-auto flex items-center gap-2">
                <a
                  href="/market-map"
                  aria-label="Market Map"
                  className="hover:bg-accent hover:text-accent-foreground focus-visible:ring-ring inline-flex h-9 items-center gap-2 rounded-md border px-3 text-sm font-medium outline-none focus-visible:ring-2"
                  data-testid="market-map-header-link"
                >
                  <MapIcon className="size-4" />
                  <span className="hidden sm:inline">Market Map</span>
                </a>
                <ThemeToggle />
              </div>
            </header>
            <div className="flex-1 overflow-hidden">
              <Thread
                onSend={sendMessage}
                marketMapSummary={marketMapSummary}
              />
            </div>
          </SidebarInset>
        </div>
      </SidebarProvider>
    </AssistantRuntimeProvider>
  );
};
