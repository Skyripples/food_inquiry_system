// 嚴格解析日曆日期；不讓 Date 自動把無效日期轉成下一個月。
function parseVerificationDate(value) {
  if (typeof value !== "string" || !/^\d{4}-\d{2}-\d{2}$/.test(value)) return null;
  const [year, month, day] = value.split("-").map(Number);
  const date = new Date(0);
  date.setUTCFullYear(year, month - 1, day);
  date.setUTCHours(0, 0, 0, 0);
  if (date.getUTCFullYear() !== year || date.getUTCMonth() !== month - 1 ||
      date.getUTCDate() !== day) return null;
  return date.getTime();
}

function getDataFreshness(verifiedAt, today = new Date()) {
  const verifiedTime = parseVerificationDate(verifiedAt);
  if (verifiedTime === null || !(today instanceof Date) || !Number.isFinite(today.getTime())) return null;
  // 以使用者本地的今天計算日曆天數，排除時分秒與夏令時間的影響。
  const calendarToday = new Date(0);
  calendarToday.setUTCFullYear(today.getFullYear(), today.getMonth(), today.getDate());
  calendarToday.setUTCHours(0, 0, 0, 0);
  const elapsedDays = Math.round((calendarToday.getTime() - verifiedTime) / 86400000);
  // 未來的日期不應被當成已確認資料的經過天數。
  if (elapsedDays < 0) return null;
  return { elapsedDays, isStale: elapsedDays > DATA_FRESHNESS_CONFIG.staleAfterDays };
}
