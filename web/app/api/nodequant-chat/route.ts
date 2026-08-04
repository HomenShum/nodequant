// Same-origin NodeQuant review proxy. The backend owns every authoritative
// number; this route only normalises AI SDK message bodies and streams bytes.
const BACKEND_REVIEW_URL = (
  process.env.TRIALSCOPE_BACKEND_URL ?? "http://localhost:8000/api/chat"
).replace(/\/api\/chat$/, "/api/kernel/review");
const REQUEST_TIMEOUT_MS = 120_000;
const MAX_REQUEST_BYTES = 1_048_576;

function validatedBackendUrl(): string {
  const url = new URL(BACKEND_REVIEW_URL);
  const allowed = new Set(
    (process.env.NODEQUANT_BACKEND_HOSTS ?? "localhost,127.0.0.1")
      .split(",").map((host) => host.trim()).filter(Boolean),
  );
  if (!["http:", "https:"].includes(url.protocol) || url.username || url.password || !allowed.has(url.hostname)) {
    throw new Error("NodeQuant backend URL is not in the configured host allowlist");
  }
  return url.toString();
}

async function readBoundedJson(request: Request): Promise<unknown> {
  const declared = Number(request.headers.get("content-length") ?? 0);
  if (declared > MAX_REQUEST_BYTES) throw new RangeError("request body exceeds 1 MiB");
  if (!request.body) return {};
  const reader = request.body.getReader();
  const chunks: Uint8Array[] = [];
  let size = 0;
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    size += value.byteLength;
    if (size > MAX_REQUEST_BYTES) {
      await reader.cancel();
      throw new RangeError("request body exceeds 1 MiB");
    }
    chunks.push(value);
  }
  const raw = new Uint8Array(size);
  let offset = 0;
  for (const chunk of chunks) { raw.set(chunk, offset); offset += chunk.byteLength; }
  const parsed: unknown = JSON.parse(new TextDecoder().decode(raw));
  if (parsed === null || typeof parsed !== "object" || Array.isArray(parsed)) {
    throw new SyntaxError("request JSON must be an object");
  }
  return parsed;
}

export async function POST(request: Request) {
  try {
    const body = await readBoundedJson(request);
    const response = await fetch(validatedBackendUrl(), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
      signal: AbortSignal.any([request.signal, AbortSignal.timeout(REQUEST_TIMEOUT_MS)]),
      cache: "no-store",
    });
    return new Response(response.body, {
      status: response.status,
      headers: {
        "Content-Type": response.headers.get("Content-Type") ?? "text/event-stream",
        "Cache-Control": "no-cache",
        "x-vercel-ai-ui-message-stream": response.headers.get("x-vercel-ai-ui-message-stream") ?? "v1",
      },
    });
  } catch (error) {
    if (error instanceof RangeError) {
      return Response.json({ error: "NodeQuant request too large", detail: error.message }, { status: 413 });
    }
    if (error instanceof SyntaxError) {
      return Response.json({ error: "Invalid NodeQuant request JSON", detail: error.message }, { status: 400 });
    }
    const detail = error instanceof Error ? error.message : String(error);
    return Response.json({ error: "NodeQuant backend unavailable", detail }, { status: 502 });
  }
}
