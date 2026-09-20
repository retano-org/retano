// supabase/functions/send-campaign-sms/index.ts
//
// Manually-invoked Edge Function. Sends SMS for eligible rows in
// `trigger_results` via sms.ir's Bulk send method, then verifies
// acceptance from the sms.ir response itself (per spec: acceptance,
// not delivery confirmation, is enough to mark a message "sent").
//
// Invoke with an empty body (or {}) to process all tenants, or
// { "tenant_id": 123 } to restrict to one tenant.
//
// Required secrets (set with `supabase secrets set` or via the
// self-hosted stack's env):
//   SUPABASE_URL
//   SUPABASE_SERVICE_ROLE_KEY
//   SMSIR_API_KEY
//   SMSIR_CAMPAIGN_LINE_NUMBER

import { createClient, SupabaseClient } from "jsr:@supabase/supabase-js@2";

// ---------------------------------------------------------------------------
// Config
// ---------------------------------------------------------------------------

const MAX_RETRIES = 3; // total attempts per row per invocation
const RETRY_DELAYS_MS = [1000, 2000, 4000]; // delay *before* attempt 2, 3, 4...
const SMSIR_BULK_URL = "https://api.sms.ir/v1/send/bulk";

const SMSIR_API_KEY = Deno.env.get("SMSIR_API_KEY");
const SMSIR_LINE_NUMBER = Deno.env.get("SMSIR_CAMPAIGN_LINE_NUMBER");
const SUPABASE_URL = Deno.env.get("SUPABASE_URL");
const SUPABASE_SERVICE_ROLE_KEY = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY");

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

interface TriggerResultRow {
  id: number;
  tenant_id: number;
  user_id: string;
  rule_id: number;
  final_message: string | null;
  phone_number: string | null;
  retry_count: number;
  status: string | null;
}

interface SmsIrBulkResponse {
  status: number;
  message: string;
  data?: {
    packId: string;
    messageIds: (number | null)[];
    cost: number;
  };
}

interface RowResult {
  id: number;
  outcome: "sent" | "failed_permanent" | "failed_will_retry_never" | "error";
  attempts: number;
  message_id?: number | null;
  error?: string;
}

// sms.ir numeric status codes -> human-readable Persian/English message.
// (from https://sms.ir/rest-api/)
const SMSIR_STATUS_MESSAGES: Record<number, string> = {
  0: "مشکلی در سامانه رخ داده است (sms.ir internal error)",
  10: "کلید وب سرویس نامعتبر است (invalid API key)",
  11: "کلید وب سرویس غیرفعال است (API key disabled)",
  12: "کلید وب سرویس محدود به IPهای تعریف شده می‌باشد (IP not whitelisted)",
  13: "حساب کاربری غیرفعال است (account disabled)",
  14: "حساب کاربری در حالت تعلیق قرار دارد (account suspended)",
  20: "تعداد درخواست بیشتر از حد مجاز است (rate limit exceeded)",
  101: "شماره خط نامعتبر می‌باشد (invalid line number)",
  102: "اعتبار کافی نمی‌باشد (insufficient credit)",
  103: "درخواست شما دارای متن(های) خالی است (empty message text)",
  104: "درخواست شما دارای موبایل(های) نادرست است (invalid mobile number)",
  105: "تعداد موبایل‌ها بیشتر از حد مجاز است (too many recipients)",
  106: "تعداد متن‌ها بیشتر از حد مجاز است (too many texts)",
  107: "لیست موبایل‌ها خالی می‌باشد (empty mobile list)",
  108: "لیست متن‌ها خالی می‌باشد (empty text list)",
  109: "زمان ارسال نامعتبر می‌باشد (invalid send time)",
  110: "تعداد شماره موبایل‌ها و تعداد متن‌ها برابر نیستند (mobile/text count mismatch)",
  115: "شماره موبایل(ها) در لیست سیاه سامانه می‌باشند (number blacklisted)",
  117: "متن ارسال شده مورد تایید نمی‌باشد (message text rejected)",
  118: "تعداد پیام‌ها بیش از حد مجاز می‌باشد (too many messages)",
};

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

/**
 * Normalizes a stored phone number (format: +989373212313) to the local
 * format sms.ir's examples use (09373212313). Kept as a small pure
 * function so it's easy to adjust if sms.ir turns out to accept the
 * +98 form directly.
 */
function normalizePhoneNumber(raw: string): string {
  let n = raw.trim();
  if (n.startsWith("+98")) {
    n = "0" + n.slice(3);
  } else if (n.startsWith("0098")) {
    n = "0" + n.slice(4);
  } else if (n.startsWith("98") && n.length === 12) {
    n = "0" + n.slice(2);
  }
  return n;
}

/** Returns current Tehran-local date (YYYY-MM-DD) and time (HH:MM:SS). */
function getTehranNow(): { date: string; time: string } {
  const now = new Date();
  const fmt = new Intl.DateTimeFormat("en-CA", {
    timeZone: "Asia/Tehran",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  });
  const parts = fmt.formatToParts(now);
  const get = (t: string) => parts.find((p) => p.type === t)?.value ?? "00";
  const date = `${get("year")}-${get("month")}-${get("day")}`;
  const time = `${get("hour")}:${get("minute")}:${get("second")}`;
  return { date, time };
}

/**
 * One attempt to send via sms.ir Bulk endpoint. Returns either the
 * accepted numeric message id, or an error string (either a network/
 * HTTP-level failure, or an sms.ir-reported failure: top-level status
 * != 1, or a null/0 entry in messageIds meaning invalid number / blacklist).
 */
async function attemptSend(
  phoneNumber: string,
  messageText: string,
): Promise<{ ok: true; messageId: number } | { ok: false; error: string }> {
  let resp: Response;
  try {
    resp = await fetch(SMSIR_BULK_URL, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Accept: "application/json",
        "X-API-KEY": SMSIR_API_KEY!,
      },
      body: JSON.stringify({
        lineNumber: Number(SMSIR_LINE_NUMBER),
        messageText: messageText,
        mobiles: [phoneNumber],
        sendDateTime: null, // send immediately — our own scheduling already gated eligibility
      }),
    });
  } catch (networkErr) {
    return {
      ok: false,
      error: `Network error calling sms.ir: ${
        networkErr instanceof Error ? networkErr.message : String(networkErr)
      }`,
    };
  }

  let body: SmsIrBulkResponse;
  try {
    body = await resp.json();
  } catch {
    return {
      ok: false,
      error: `sms.ir returned non-JSON response (HTTP ${resp.status})`,
    };
  }

  if (body.status !== 1) {
    const known = SMSIR_STATUS_MESSAGES[body.status];
    return {
      ok: false,
      error: `sms.ir error (status ${body.status}): ${
        known ?? body.message ?? "unknown error"
      }`,
    };
  }

  const messageId = body.data?.messageIds?.[0];
  if (messageId === null || messageId === undefined || messageId === 0) {
    return {
      ok: false,
      error: messageId === 0
        ? "sms.ir rejected the number: blacklisted (messageIds[0] = 0)"
        : "sms.ir rejected the number: invalid number or text too long (messageIds[0] = null)",
    };
  }

  return { ok: true, messageId };
}

/**
 * Atomically claims a single row for processing: flips it to status
 * 'pending' (recording that an invocation is now working on it) only if
 * it is still in an eligible, unclaimed state at the moment of the
 * UPDATE. This is the idempotency guard — if two invocations raced on
 * the same row, only one UPDATE would find a matching row and return it.
 */
async function claimRow(
  supabase: SupabaseClient,
  row: TriggerResultRow,
): Promise<boolean> {
  const { data, error } = await supabase
    .from("trigger_results")
    .update({ status: "pending" })
    .eq("id", row.id)
    .eq("processed", false)
    .in("status", ["pending", "failed"])
    .lt("retry_count", MAX_RETRIES)
    .select("id");

  if (error) {
    console.error(`Failed to claim row ${row.id}:`, error.message);
    return false;
  }
  return (data?.length ?? 0) === 1;
}

async function markSent(
  supabase: SupabaseClient,
  id: number,
  messageId: number,
  attemptsFailedBeforeSuccess: number,
  nowIso: string,
): Promise<void> {
  await supabase
    .from("trigger_results")
    .update({
      status: "sent",
      processed: true,
      retry_count: attemptsFailedBeforeSuccess,
      error_message: null,
      sms_message_id: String(messageId),
      sent_at: nowIso,
      delivered_at: nowIso, // per spec: sent_at and delivered_at are identical for now
    })
    .eq("id", id);
}

async function markFailed(
  supabase: SupabaseClient,
  id: number,
  finalRetryCount: number,
  lastError: string,
): Promise<void> {
  await supabase
    .from("trigger_results")
    .update({
      status: "failed",
      processed: true,
      retry_count: finalRetryCount,
      error_message: lastError,
    })
    .eq("id", id);
}

/**
 * Runs up to MAX_RETRIES send attempts for one row, with delay between
 * attempts, and persists the final outcome.
 */
async function processRow(
  supabase: SupabaseClient,
  row: TriggerResultRow,
): Promise<RowResult> {
  if (!row.final_message || row.final_message.trim() === "") {
    await markFailed(supabase, row.id, row.retry_count, "final_message is empty");
    return { id: row.id, outcome: "error", attempts: 0, error: "final_message is empty" };
  }
  if (!row.phone_number || row.phone_number.trim() === "") {
    await markFailed(supabase, row.id, row.retry_count, "phone_number is empty");
    return { id: row.id, outcome: "error", attempts: 0, error: "phone_number is empty" };
  }

  const phone = normalizePhoneNumber(row.phone_number);
  let failedAttempts = 0;
  let lastError = "";

  for (let attempt = 0; attempt < MAX_RETRIES; attempt++) {
    if (attempt > 0) {
      await sleep(RETRY_DELAYS_MS[attempt - 1] ?? 4000);
    }

    const result = await attemptSend(phone, row.final_message);

    if (result.ok) {
      const nowIso = new Date().toISOString();
      await markSent(supabase, row.id, result.messageId, failedAttempts, nowIso);
      return {
        id: row.id,
        outcome: "sent",
        attempts: attempt + 1,
        message_id: result.messageId,
      };
    }

    failedAttempts++;
    lastError = result.error;
  }

  // Exhausted all attempts in this invocation.
  await markFailed(supabase, row.id, failedAttempts, lastError);
  return {
    id: row.id,
    outcome: "failed_permanent",
    attempts: MAX_RETRIES,
    error: lastError,
  };
}

// ---------------------------------------------------------------------------
// Handler
// ---------------------------------------------------------------------------

Deno.serve(async (req: Request) => {
  if (!SMSIR_API_KEY || !SMSIR_LINE_NUMBER) {
    return Response.json(
      { error: "Missing SMSIR_API_KEY or SMSIR_CAMPAIGN_LINE_NUMBER secret" },
      { status: 500 },
    );
  }
  if (!SUPABASE_URL || !SUPABASE_SERVICE_ROLE_KEY) {
    return Response.json(
      { error: "Missing SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY secret" },
      { status: 500 },
    );
  }

  let tenantId: number | null = null;
  try {
    const bodyText = await req.text();
    if (bodyText) {
      const parsed = JSON.parse(bodyText);
      if (parsed && parsed.tenant_id !== undefined && parsed.tenant_id !== null) {
        tenantId = Number(parsed.tenant_id);
        if (!Number.isFinite(tenantId)) {
          return Response.json({ error: "tenant_id must be a number" }, { status: 400 });
        }
      }
    }
  } catch {
    return Response.json({ error: "Invalid JSON body" }, { status: 400 });
  }

  const supabase = createClient(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY);

  const { date: tehranDate, time: tehranTime } = getTehranNow();

  // Eligibility: not yet fully processed, status pending/failed, has
  // retries left, scheduled date has arrived, and the campaign's
  // send_sms_time (Tehran clock) has arrived. core_campaign is joined
  // via rule_id -> core_campaign.id.
  let query = supabase
    .from("trigger_results")
    .select(
      "id, tenant_id, user_id, rule_id, final_message, phone_number, retry_count, status, send_sms_date, core_campaign!inner(send_sms_time)",
    )
    .eq("processed", false)
    .in("status", ["pending", "failed"])
    .lt("retry_count", MAX_RETRIES)
    .lte("send_sms_date", tehranDate)
    .lte("core_campaign.send_sms_time", tehranTime);

  if (tenantId !== null) {
    query = query.eq("tenant_id", tenantId);
  }

  const { data: candidateRows, error: fetchError } = await query;

  if (fetchError) {
    return Response.json(
      { error: `Failed to fetch eligible rows: ${fetchError.message}` },
      { status: 500 },
    );
  }

  const results: RowResult[] = [];

  for (const raw of candidateRows ?? []) {
    const row = raw as unknown as TriggerResultRow;

    // Idempotency / double-send guard: atomically claim the row before
    // doing any work. If another concurrent invocation already claimed
    // it, this returns false and we skip it.
    const claimed = await claimRow(supabase, row);
    if (!claimed) {
      continue;
    }

    const result = await processRow(supabase, row);
    results.push(result);
  }

  const summary = {
    tenant_id: tenantId,
    checked_at_tehran: `${tehranDate} ${tehranTime}`,
    candidates_found: candidateRows?.length ?? 0,
    processed: results.length,
    sent: results.filter((r) => r.outcome === "sent").length,
    failed: results.filter((r) => r.outcome === "failed_permanent" || r.outcome === "error").length,
    results,
  };

  return Response.json(summary);
});
