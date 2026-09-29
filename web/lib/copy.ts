/** Enterprise Recovery Vault — English display copy & sanitizers. */

const BAD_PHRASES: Array<[RegExp, string]> = [
  [/传承车手/gi, "Legacy Driver Archive"],
  [/传承\s*司机/gi, "Legacy Driver Archive"],
  [/官方遗留驱动/gi, "Legacy Driver Archive"],
  [/物理直链/gi, "verified download"],
  [/鳕鱼\s*(错误|故障|代码)?/gi, "Code 43 hardware failure"],
  [/Fix\s*Cod\b/gi, "Fix Code 43"],
  [/Legacy Driver Download/gi, "Legacy Driver Archive"],
  [/Legacy Driver(?!\s+Archive)/gi, "Legacy Driver Archive"],
  [/geek driver archive/gi, "enterprise recovery archive"],
  [/link curation/gi, "enterprise recovery"],
  [/MERCHANDIZE/gi, "catalog sync"],
  [/local_storage\s*=\s*\w+/gi, ""],
];

export function sanitizeCopy(input: string): string {
  let s = (input || "").trim();
  for (const [re, rep] of BAD_PHRASES) {
    s = s.replace(re, rep);
  }
  // Overseas storefront: never surface CJK in UI copy
  s = s.replace(/[\u4e00-\u9fff]+/g, " ");
  s = s.replace(/\(\s*\)/g, "").replace(/\[\s*\]/g, "");
  s = s.replace(/\s{2,}/g, " ").replace(/\s*\|\s*$/g, "").trim();
  return s;
}

export function normalizeCodeTerms(input: string): string {
  let s = sanitizeCopy(input);
  s = s.replace(
    /出现\s*Code\s*43[^；;。]*/gi,
    "Code 43 hardware failure / device disabled",
  );
  s = s.replace(
    /Code 43 硬件中断\/?设备禁用修复/gi,
    "Code 43 hardware failure / device disabled recovery",
  );
  s = s.replace(
    /Fix Code 43\s*\/\s*Code 10 missing driver on legacy Windows for [^.】]*/gi,
    "Code 43 / Code 10 device-disabled and missing-driver recovery",
  );
  s = s.replace(/系统过旧，新驱动不兼容/g, "Legacy OS incompatibility with current OEM drivers");
  s = s.replace(
    /工控场景停机风险高，急需可用驱动/g,
    "Industrial downtime risk — verified driver required",
  );
  s = s.replace(/官方驱动难寻或已下架/g, "OEM driver discontinued or pulled from official channels");
  return s;
}

function hasCode43(text: string): boolean {
  return /code\s*43|代码\s*43/i.test(text);
}

function categoryHint(category: string, model: string): string {
  const c = category.toLowerCase();
  const m = model.toLowerCase();
  if (/cmp|矿/.test(m)) return "Discontinued Mining GPU — Legacy Driver Archive";
  if (/显卡|gpu|graphics|radeon|nvidia|intel hd/.test(c + m))
    return "Legacy GPU Driver Archive";
  if (/采集|工控|daq|vxi|rk3588|板载/.test(c + m))
    return "Industrial / Capture Card — Legacy Driver Archive";
  if (/网卡|usb|ethernet|nic/.test(c + m))
    return "Legacy NIC / USB Driver Archive";
  return "Legacy Driver Archive";
}

/** Card / detail headline: model + recovery focus. */
export function commercialTitle(
  model: string,
  category: string,
  painBlob: string,
): string {
  const hint = categoryHint(category, model);
  const code = hasCode43(painBlob) ? " — Code 43 Recovery" : "";
  return sanitizeCopy(`${model} ${hint}${code}`);
}

/** One-line applicability for cards. */
export function commercialPain(model: string, rawPains: string[]): string {
  const joined = rawPains.map(normalizeCodeTerms).join("; ");
  if (/oem|discontinued|pulled|downtime|incompatib|code\s*43|下架|停产|停机|不兼容/i.test(joined)) {
    return VALUE_APPLICABILITY;
  }
  if (joined.trim()) {
    const first = normalizeCodeTerms(rawPains[0] || "");
    if (
      first.length > 0 &&
      first.length <= 120 &&
      !/looking for archived/i.test(first) &&
      !/[\u4e00-\u9fff]/.test(first)
    ) {
      return first;
    }
  }
  return `Legacy Driver Archive for ${model} — verified instant download.`;
}

export function commercialSubtitle(model: string): string {
  return `Legacy Driver Archive · ${model}`;
}

export const VAULT_ANNOUNCEMENT =
  "DrvVault · Global search for scarce industrial & legacy hardware drivers. 100% verified, no registration, zero storage, USDT-TRC20 instant release.";

export const CTA_UNLOCK = "Download now";

export const VALUE_APPLICABILITY =
  "Built for: OEM pages discontinued, aging industrial equipment, and OS incompatibility that risks downtime.";
