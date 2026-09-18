// 純計算層：清單只提供 productId / quantity，商品資料由 Repository 取得。
function calculateDailyNutrition(items, repository) {
  if (!Array.isArray(items) || items.length === 0) return {};
  const totals = Object.fromEntries(Object.entries(NUTRITION_CONFIG).map(([key, config]) =>
    [key, { value: 0, unit: config.dailyReference?.unit ?? "g", complete: true }],
  ));

  items.forEach((item) => {
    const validQuantity = Number.isFinite(item?.quantity) && item.quantity > 0;
    const product = validQuantity ? repository.getProductById(item.productId) : null;
    Object.entries(totals).forEach(([key, total]) => {
      const nutrient = product?.nutrition?.[key];
      if (!validQuantity || !Number.isFinite(nutrient?.value) || nutrient.value < 0 ||
          nutrient.unit !== total.unit) {
        total.complete = false;
        return;
      }
      const contribution = nutrient.value * item.quantity;
      const nextValue = total.value + contribution;
      if (!Number.isFinite(contribution) || !Number.isFinite(nextValue)) {
        total.complete = false;
        return;
      }
      total.value = nextValue;
    });
  });

  // 不把缺漏資料當成 0；該項目由既有營養渲染顯示「目前尚無資料」。
  return Object.fromEntries(Object.entries(totals)
    .filter(([, total]) => total.complete)
    .map(([key, total]) => [key, {
      // 消除二進位浮點運算尾差，例如 2.1 × 3 顯示為 6.3。
      value: Number(total.value.toPrecision(15)),
      unit: total.unit,
    }]));
}
