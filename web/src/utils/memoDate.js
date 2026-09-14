// Memo scheduling uses the reminder store's UTC+8 clock, independent of browser timezone.
export function memoDateParts(value) {
  if (!value) return { date: "", time: "" };
  if (/^\d{4}-\d{2}-\d{2}$/.test(value)) return { date: value, time: "" };
  const zoned = /(?:Z|[+-]\d{2}:?\d{2})$/i.test(value);
  const stamp = new Date(zoned ? value : `${value}+08:00`);
  const local = new Date(stamp.getTime() + 8 * 3600000).toISOString();
  return { date: local.slice(0, 10), time: local.slice(11, 16) };
}

export function changeMemoDate(value, part, next) {
  const parts = { ...memoDateParts(value), [part]: next };
  if (!parts.date) return "";
  return parts.time ? `${parts.date}T${parts.time}:00+08:00` : parts.date;
}
