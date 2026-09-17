const MISSING_DATA = "目前尚無資料";

function hasDisplayValue(value) {
  if (typeof value === "number") return Number.isFinite(value);
  return typeof value === "string" && value.trim() !== "" &&
    !["undefined", "null", "nan"].includes(value.trim().toLowerCase());
}

function displayValue(value) {
  return hasDisplayValue(value) ? String(value) : MISSING_DATA;
}

function hasQuantity(quantity, valueKey = "value") {
  return Number.isFinite(quantity?.[valueKey]) && quantity[valueKey] >= 0 &&
    typeof quantity.unit === "string" && hasDisplayValue(quantity.unit);
}

function formatQuantity(quantity, separator = " ", valueKey = "value") {
  return hasQuantity(quantity, valueKey)
    ? `${quantity[valueKey]}${separator}${quantity.unit}`
    : MISSING_DATA;
}

function createDetail(label, value) {
  const wrapper = document.createElement("div");
  const term = document.createElement("dt");
  const description = document.createElement("dd");
  term.textContent = label;
  description.textContent = displayValue(value);
  wrapper.append(term, description);
  return wrapper;
}

function createSourceDetail(source) {
  const detail = createDetail("資料來源", MISSING_DATA);
  if (!hasDisplayValue(source?.name) || !hasDisplayValue(source?.url)) return detail;
  try {
    const url = new URL(source.url);
    if (!["https:", "http:"].includes(url.protocol)) return detail;
    const link = document.createElement("a");
    link.className = "source-link";
    link.href = url.href;
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    link.textContent = displayValue(source.name);
    detail.querySelector("dd").replaceChildren(link);
  } catch {
    // 無效或不完整的來源不建立連結。
  }
  return detail;
}

function createSourceTimingDetail(source) {
  const freshness = getDataFreshness(source?.verified_at);
  const detail = createDetail("最後更新日期", freshness ? source.verified_at : "資料更新時間未知");
  if (!freshness) return detail;

  const age = document.createElement("span");
  age.className = "source-age";
  age.textContent = `距資料確認已過 ${freshness.elapsedDays} 天`;
  const description = detail.querySelector("dd");
  description.append(age);
  if (freshness.isStale) {
    const warning = document.createElement("span");
    warning.className = "source-warning";
    warning.textContent = DATA_FRESHNESS_CONFIG.staleMessage;
    description.append(warning);
  }
  return detail;
}

function createProductCard(product = {}) {
  const nutritionSource = resolveProductSource(product, product.nutritionSource);
  const card = document.createElement("article");
  const title = document.createElement("h3");
  const details = document.createElement("dl");
  card.className = "product-card";
  title.textContent = displayValue(product.name);
  details.className = "product-details";
  details.append(
    createDetail("品牌", product.brand),
    createDetail("條碼", product.barcode),
    createDetail("每份規格", formatQuantity(product.serving, "", "amount")),
    createSourceDetail(nutritionSource),
    createSourceTimingDetail(nutritionSource),
  );
  card.append(title, details, createNutritionSection(product.nutrition));
  return card;
}

function renderProductDetail(container, product) {
  container.replaceChildren(createProductCard(product));
}
