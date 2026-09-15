const ZONE_SUFFIX = /(?:Z|[+-]\d{2}:?\d{2})$/iu;
const DATE_TIME_PREFIX = /^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}/u;

export function parseSourceTime(value) {
  const raw = String(value || "").trim();
  if (!raw) return null;
  const normalized = !ZONE_SUFFIX.test(raw) && DATE_TIME_PREFIX.test(raw)
    ? `${raw.replace(" ", "T")}Z`
    : raw;
  const parsed = new Date(normalized);
  return Number.isNaN(parsed.getTime()) ? null : parsed;
}

export function formatSourceTime(value, { includeYear = false } = {}) {
  const parsed = parseSourceTime(value);
  if (!parsed) return String(value || "").trim() || "时间未记录";
  return new Intl.DateTimeFormat("zh-CN", {
    timeZone: "Asia/Shanghai",
    ...(includeYear ? { year: "numeric" } : {}),
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }).format(parsed).replaceAll("/", "-");
}
