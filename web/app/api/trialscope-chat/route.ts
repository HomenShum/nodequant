// Same-origin proxy avoids browser CORS differences and keeps the documented
// backend port configurable for local and deployed environments.
const BACKEND_CHAT_URL =
  process.env.TRIALSCOPE_BACKEND_URL ?? "http://localhost:8000/api/chat";
const REQUEST_TIMEOUT_MS = 90_000;

export async function POST(request: Request) {
  try {
    const body = (await request.json()) as {
      messages?: Array<Record<string, unknown>>;
      [key: string]: unknown;
    };
    const messages = body.messages?.map((message) => ({
      ...message,
      content: message.content ?? message.parts,
    }));

    const response = await fetch(BACKEND_CHAT_URL, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ...body, messages, tools: [] }),
      signal: AbortSignal.any([
        request.signal,
        AbortSignal.timeout(REQUEST_TIMEOUT_MS),
      ]),
      cache: "no-store",
    });

    return new Response(response.body, {
      status: response.status,
      headers: {
        "Content-Type":
          response.headers.get("Content-Type") ?? "text/event-stream",
        "Cache-Control": "no-cache",
        "x-vercel-ai-ui-message-stream":
          response.headers.get("x-vercel-ai-ui-message-stream") ?? "v1",
      },
    });
  } catch (error) {
    const detail = error instanceof Error ? error.message : String(error);
    return Response.json(
      { error: "TrialScope backend unavailable", detail },
      { status: 502 },
    );
  }
}
