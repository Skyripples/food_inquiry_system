function getDailyReferencePercentage(nutrient, reference) {
  if (!hasQuantity(nutrient) || !reference || nutrient.unit !== reference.unit ||
      !Number.isFinite(reference.amount) || reference.amount <= 0) return null;
  const percentage = (nutrient.value / reference.amount) * 100;
  return Number.isFinite(percentage) ? percentage : null;
}

function appendDailyReference(row, label, percentage) {
  const formattedPercentage = `${Number(percentage.toFixed(1))}%`;
  const referenceText = document.createElement("span");
  const progress = document.createElement("progress");
  referenceText.className = "daily-reference";
  referenceText.textContent = `每日參考值 ${formattedPercentage}`;
  progress.className = "nutrition-progress";
  progress.max = 100;
  progress.value = Math.min(100, Math.max(0, percentage));
  progress.setAttribute("aria-label", `${label}每日參考值`);
  progress.setAttribute("aria-valuetext", formattedPercentage);
  progress.textContent = formattedPercentage;
  row.querySelector("dd").append(referenceText, progress);
}

function createNutritionSection(nutrition, {
  title = "營養成分",
  note: noteText = "每份含量與每日參考值百分比",
} = {}) {
  const section = document.createElement("section");
  const heading = document.createElement("h4");
  const note = document.createElement("p");
  const list = document.createElement("dl");
  section.className = "nutrition-section";
  heading.textContent = title;
  note.className = "nutrition-note";
  note.textContent = noteText;
  list.className = "nutrition-list";
  Object.entries(NUTRITION_CONFIG).forEach(([key, config]) => {
    const nutrient = nutrition?.[key];
    const row = createDetail(config.label, formatQuantity(nutrient));
    row.className = "nutrition-row";
    row.dataset.nutrient = key;
    const percentage = getDailyReferencePercentage(nutrient, config.dailyReference);
    if (percentage !== null) appendDailyReference(row, config.label, percentage);
    list.append(row);
  });
  section.append(heading, note, list);
  return section;
}
