export function localCalendarDate(now = new Date()) {
  return `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, "0")}-${String(now.getDate()).padStart(2, "0")}`;
}

export function togetherDays(meetingDate, today = localCalendarDate()) {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(meetingDate)) return null;
  const start = Date.parse(`${meetingDate}T00:00:00Z`);
  const end = Date.parse(`${today}T00:00:00Z`);
  if (!Number.isFinite(start) || !Number.isFinite(end) || start > end
    || new Date(start).toISOString().slice(0, 10) !== meetingDate) return null;
  return Math.floor((end - start) / 86400000) + 1;
}

export function togetherCaption(meetingDate, today) {
  const days = togetherDays(meetingDate, today);
  return days === null ? "从这里开始" : `在一起的 ${days} 天`;
}
