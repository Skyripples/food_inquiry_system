const DAILY_REFERENCE_STATUS_CONFIG = Object.freeze({
  near: Object.freeze({ minimumPercentage: 80, message: "接近每日參考值" }),
  reached: Object.freeze({ minimumPercentage: 100, message: "已達或超過每日參考值" }),
});

// 使用未四捨五入的計算百分比判斷；顯示仍沿用一位小數規則。
function getDailyReferenceStatus(percentage) {
  if (!Number.isFinite(percentage) || percentage < 0) return null;
  for (const key of ["reached", "near"]) {
    const config = DAILY_REFERENCE_STATUS_CONFIG[key];
    if (percentage >= config.minimumPercentage) return { key, message: config.message };
  }
  return { key: "normal", message: "" };
}
