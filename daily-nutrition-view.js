function applyDailyReferenceStatuses(section, nutrition) {
  Object.entries(NUTRITION_CONFIG).forEach(([key, config]) => {
    if (!config.dailyReference) return;
    const percentage = getDailyReferencePercentage(nutrition[key], config.dailyReference);
    const state = getDailyReferenceStatus(percentage);
    if (!state) return;
    const row = section.querySelector(`[data-nutrient="${key}"]`);
    const progress = row.querySelector("progress");
    if (progress) progress.dataset.referenceState = state.key;
    if (state.message) {
      const label = document.createElement("span");
      label.className = "daily-reference-status";
      label.dataset.referenceState = state.key;
      label.textContent = state.message;
      row.querySelector("dd").append(label);
    }
  });
}

function renderDailyNutrition(container, items, repository) {
  container.replaceChildren();
  container.hidden = items.length === 0;
  if (items.length === 0) return;
  const totals = calculateDailyNutrition(items, repository);
  const section = createNutritionSection(totals, {
    title: "營養累加總計",
    note: "依累加清單數量計算；缺資料的營養項目顯示「目前尚無資料」。",
  });
  applyDailyReferenceStatuses(section, totals);
  container.append(section);
}
