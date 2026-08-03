/**
 * Money formatting, with one invariant:
 *
 *   A non-zero authoritative monetary value may never render as zero.
 *
 * Raw micros stay authoritative; formatting is a deterministic projection of
 * them. The failure this prevents is subtle and was live: fees of 3,400 micros
 * rendered as "$0.00", so the panel stated there were no fees when there were.
 * The arithmetic was correct and the user-facing authority layer was false,
 * which is the worst combination available — every check passes and the screen
 * lies.
 *
 * Raising the fee made that instance visible but did not fix the class. A
 * per-share regulatory fee, an exchange rebate, a fractional accrual, or a borrow
 * charge can all be legitimately sub-cent. The invariant, not the magnitude, is
 * the fix.
 *
 * Run: node lib/money.mjs
 */

const MICROS = 1_000_000;

export function formatUsd(micros) {
  const value = micros / MICROS;
  // Below half a cent, two decimals would round to zero. Widen precision until
  // the value survives rather than letting it vanish.
  const digits = micros !== 0 && Math.abs(value) < 0.005 ? 6 : 2;
  return value.toLocaleString("en-US", {
    style: "currency",
    currency: "USD",
    minimumFractionDigits: 2,
    maximumFractionDigits: digits,
  });
}

function demo() {
  const assert = (cond, msg) => {
    if (!cond) throw new Error(msg);
  };

  // The exact case that shipped broken. Trailing zeros are trimmed because
  // minimumFractionDigits is 2, so this is "$0.0034" rather than "$0.003400".
  assert(formatUsd(3400) === "$0.0034", `3400 micros -> ${formatUsd(3400)}`);

  // The invariant itself, swept across every magnitude that could round away,
  // both signs, so a future formatter change cannot reintroduce the class.
  for (const m of [1, -1, 9, 499, 999, 3400, -3400, 4999, -4999, 5000, -5000, 1, 12]) {
    const out = formatUsd(m);
    assert(out !== "$0.00" && out !== "-$0.00", `${m} micros rendered as ${out}`);
  }

  // Zero is allowed to render as zero. Only non-zero may not.
  assert(formatUsd(0) === "$0.00", `0 micros -> ${formatUsd(0)}`);

  // Ordinary amounts keep two decimals.
  assert(formatUsd(34 * MICROS) === "$34.00", formatUsd(34 * MICROS));
  assert(formatUsd(-550_875_300) === "-$550.88", formatUsd(-550_875_300));

  console.log("money: a non-zero value never renders as zero");
}

if (import.meta.url === `file://${process.argv[1]?.replace(/\\/g, "/")}`) demo();
