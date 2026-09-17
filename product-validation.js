// 商品使用 serving、nutrition，並以 nutritionSource 引用 sources 中的來源。
// id、name 不合法時排除整筆；選填欄位不合法時移除該欄位。
function isProductRecord(value) {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

function isProductText(value) {
  return typeof value === "string" && value.trim() !== "" &&
    !["undefined", "null", "nan"].includes(value.trim().toLowerCase());
}

function validateProductText(value, field, warn) {
  if (isProductText(value)) return value.trim();
  warn(`${field} 缺少資料或不是有效字串`);
  return undefined;
}

function validateProductQuantity(value, field, warn, expectedUnit, valueKey = "amount") {
  if (!isProductRecord(value) || !Number.isFinite(value[valueKey]) || value[valueKey] < 0 ||
      !isProductText(value.unit) || (expectedUnit && value.unit !== expectedUnit)) {
    warn(`${field} 必須有有效的非負數值與正確單位`);
    return undefined;
  }
  return { [valueKey]: value[valueKey], unit: value.unit.trim() };
}

function validateProductNutrition(nutrition, warn) {
  const validated = {};
  if (!isProductRecord(nutrition)) {
    warn("nutrition 缺少資料或不是物件");
    return validated;
  }
  Object.entries(NUTRITION_CONFIG).forEach(([key, config]) => {
    const quantity = validateProductQuantity(
      nutrition[key], `nutrition.${key}`, warn,
      config.dailyReference?.unit ?? "g", "value",
    );
    if (quantity) validated[key] = quantity;
  });
  return validated;
}

function validateProductSource(source, warn) {
  if (!isProductRecord(source)) {
    warn("來源不是物件");
    return undefined;
  }
  const id = validateProductText(source.id, "id", warn);
  const name = validateProductText(source.name, "name", warn);
  const url = validateProductText(source.url, "url", warn);
  if (!id || !name || !url) return undefined;
  try {
    if (["http:", "https:"].includes(new URL(url).protocol)) {
      let verifiedAt = source.verified_at;
      if (parseVerificationDate(verifiedAt) === null) {
        warn("verified_at 缺失或不是有效的 YYYY-MM-DD 日期");
        verifiedAt = undefined;
      }
      return { id, name, url, verified_at: verifiedAt };
    }
  } catch {
    // 無效 URL 僅警告，不拋出至載入流程。
  }
  warn("url 必須是有效的 HTTP 或 HTTPS 網址");
  return undefined;
}

function validateProductSources(sources, warn) {
  if (!Array.isArray(sources)) {
    warn("sources 缺少資料或不是陣列");
    return [];
  }
  const validated = [];
  const seenIds = new Set();
  sources.forEach((source, index) => {
    const sourceWarn = (reason) => warn(`sources[${index}]：${reason}`);
    if (isProductText(source?.id)) {
      const id = source.id.trim();
      if (seenIds.has(id)) {
        sourceWarn(`id「${id}」重複，已忽略；保留第一筆`);
        return;
      }
      seenIds.add(id);
    }
    const value = validateProductSource(source, sourceWarn);
    if (value) validated.push(value);
  });
  return validated;
}

function validateNutritionSource(sourceId, sources, warn) {
  const id = validateProductText(sourceId, "nutritionSource", warn);
  if (!id) return undefined;
  if (!resolveProductSource({ sources }, id)) {
    warn(`nutritionSource「${id}」找不到對應來源`);
    return undefined;
  }
  return id;
}

function validateProducts(records) {
  const validated = [];
  const seenIds = new Set();
  if (!Array.isArray(records)) {
    console.warn("[食品資料驗證] products 必須是陣列");
    return validated;
  }
  records.forEach((product, index) => {
    const warn = (reason) => console.warn(`[食品資料驗證] 第 ${index + 1} 筆：${reason}`);
    if (!isProductRecord(product)) {
      warn("商品不是物件，已忽略");
      return;
    }
    const id = validateProductText(product.id, "必要欄位 id", warn);
    const name = validateProductText(product.name, "必要欄位 name", warn);
    if (id) {
      if (seenIds.has(id)) {
        warn(`id「${id}」重複，已忽略；保留第一筆`);
        return;
      }
      seenIds.add(id);
    }
    if (!id || !name) return;

    const brand = validateProductText(product.brand, "brand", warn);
    let barcode = validateProductText(product.barcode, "barcode", warn);
    if (barcode && !/^\d+$/.test(barcode)) {
      warn("barcode 必須是純數字字串");
      barcode = undefined;
    }
    const sources = validateProductSources(product.sources, warn);
    validated.push({
      ...product,
      id,
      name,
      brand,
      barcode,
      serving: validateProductQuantity(product.serving, "serving", warn),
      nutrition: validateProductNutrition(product.nutrition, warn),
      sources,
      nutritionSource: validateNutritionSource(product.nutritionSource, sources, warn),
    });
  });
  return validated;
}
